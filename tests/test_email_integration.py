"""Opt-in real bundled-JVM integration; provider is explicitly a deterministic fixture."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_studio import EmailStudio
from our_harness.server import HarnessHTTPServer


@unittest.skipUnless(os.environ.get("NEXUS_TEST_KESTRA") == "1", "real bundled Kestra integration is opt-in")
class RealEmailWorkflow(unittest.TestCase):
    def test_review_restart_resume_learning_and_account_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="mail-arbitrary-") as temporary:
            root = Path(temporary)
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            prompts = []
            def provider(route, task, context):
                prompts.append(context)
                if task.startswith("Extract"):
                    return "Use the sign-off Kind regards."
                return "Hello,\nThank you for the update.\n" + (
                    "Kind regards" if context.get("approved_preferences") else "Cheers")
            def start():
                panel = HarnessHTTPServer(("127.0.0.1", 0), config)
                panel.email._studio = EmailStudio(config, provider_call=provider)
                threading.Thread(target=panel.serve_forever, daemon=True).start()
                return panel
            panel = start()
            def api(action, payload):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{panel.server_port}/api/email/{action}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "X-Harness-Token": panel.token})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read())
            def wait_for(check, label, seconds=90):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    value = check()
                    if value:
                        return value
                    failures = [j for j in panel.email._jobs.values() if j["state"] == "failed"]
                    if failures:
                        self.fail(f"{label}: {failures}")
                    time.sleep(.3)
                self.fail(f"Timed out waiting for {label}")
            def draft(identity):
                return next(d for d in panel.email.studio.snapshot()["drafts"] if d["id"] == identity)
            try:
                a = api("account_save", {"name": "A", "email": "a@example.test", "kind": "import", "provider_route": "arbitrary-route"})["account"]["id"]
                b = api("account_save", {"name": "B", "email": "b@example.test", "kind": "import", "provider_route": "arbitrary-route"})["account"]["id"]
                incoming = {"account_id": a, "sender": "colleague@example.test", "subject": "PRIVATE SENTINEL", "body": "Can you acknowledge the schedule?"}
                message = api("import", incoming)["message"]
                self.assertEqual(api("import", incoming)["message"]["id"], message["id"])
                identity = api("create_draft", {"account_id": a, "message_id": message["id"]})["draft"]["id"]
                wait_for(lambda: draft(identity)["status"] == "review" and draft(identity)["execution_id"], "real draft generation")
                execution_id = draft(identity)["execution_id"]
                wait_for(lambda: panel.email.engine.execution_status(execution_id)["state"]["current"] == "PAUSED", "Kestra pause")
                execution = panel.email.engine.execution_status(execution_id)
                self.assertNotIn("PRIVATE SENTINEL", json.dumps(execution))
                saved = api("save_draft", {"account_id": a, "draft_id": identity, "revision": draft(identity)["revision"], "text": "Thank you.\nKind regards"})["draft"]
                panel.shutdown()
                panel.server_close()
                panel = start()
                self.assertEqual(draft(identity)["edited"], saved["edited"])
                self.assertEqual(draft(identity)["original"], saved["original"])
                api("approve_draft", {"account_id": a, "draft_id": identity, "revision": saved["revision"], "text": saved["edited"], "learn": True})
                wait_for(lambda: draft(identity)["status"] == "exported" and draft(identity).get("learning_complete"), "resume/export/reflection")
                self.assertTrue(Path(draft(identity)["export_path"]).is_file())
                exported = api("export_email", {"account_id": a, "draft_id": identity})
                self.assertIn("Kind regards", exported["content"])
                self.assertEqual(len(panel.email.studio.snapshot()["memories"]), 1)
                next_message = api("import", {**incoming, "subject": "Second request"})["message"]
                next_id = api("create_draft", {"account_id": a, "message_id": next_message["id"]})["draft"]["id"]
                wait_for(lambda: draft(next_id)["status"] == "review", "second draft")
                self.assertIn("Kind regards", draft(next_id)["original"])
                self.assertEqual(prompts[-1]["approved_preferences"], ["Use the sign-off Kind regards."])
                isolated = api("import", {**incoming, "account_id": b})["message"]
                isolated_id = api("create_draft", {"account_id": b, "message_id": isolated["id"]})["draft"]["id"]
                wait_for(lambda: draft(isolated_id)["status"] == "review", "isolated account draft")
                self.assertEqual(prompts[-1]["approved_preferences"], [])
                self.assertEqual(prompts[-1]["previous_received"], [])
            finally:
                panel.shutdown()
                panel.server_close()
