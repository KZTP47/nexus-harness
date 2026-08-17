"""What the panel is told while a run is going.

The live feed is not a second copy of the run folder. It is usually the first
place a check's output exists at all: it arrives while the run is going, before
anything has been written down. It goes onto a screen that gets shared, and it
stays in the page afterwards. So it is cleaned.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.server import EventBus

SECRETS = ("sk-live-abcdefghijklmno", "hunter2hunter2", "eyJhbGciOiJIUzI1NiJ9abcdef")


def leaky_result() -> dict:
    return {
        "run_id": "20260101-000001",
        "counts": {"passed": 0, "failed": 1, "flaky": 0, "skipped": 0, "total": 1},
        "cases": [{
            "id": "sign-in",
            "title": "Signing in works",
            "status": "failed",
            "duration_ms": 12,
            "reasons": ["connecting with api_key=sk-live-abcdefghijklmno gave 401"],
            "attempts": [{
                "number": 1,
                "passed": False,
                "evidence": (
                    '{"password": "hunter2hunter2"}\n'
                    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef"
                ),
            }],
        }],
    }


class LiveFeedTests(unittest.TestCase):
    def said(self, bus: EventBus) -> str:
        return json.dumps(bus.after(0))

    def test_a_run_result_on_the_wire_carries_no_credential(self) -> None:
        bus = EventBus()
        bus.add({"kind": "qa_result", "node": "checks", "payload": leaky_result()})
        said = self.said(bus)
        for secret in SECRETS:
            with self.subTest(secret=secret[:16]):
                self.assertNotIn(secret, said)
        self.assertIn("[REDACTED]", said)

    def test_it_still_says_which_check_failed_and_why(self) -> None:
        bus = EventBus()
        bus.add({"kind": "qa_result", "node": "checks", "payload": leaky_result()})
        said = self.said(bus)
        self.assertIn("sign-in", said)
        self.assertIn("401", said)
        self.assertIn('"failed": 1', said)

    def test_an_error_message_is_cleaned_too(self) -> None:
        bus = EventBus()
        bus.add({"kind": "qa_error", "node": "checks",
                 "payload": {"error": "token=abcdefghijklmnop was refused"}})
        self.assertNotIn("abcdefghijklmnop", self.said(bus))

    def test_a_bus_made_without_a_remover_still_hides_things(self) -> None:
        # Forgetting must not be the thing that turns this off.
        bus = EventBus(redactor=None)
        bus.add({"kind": "qa_result", "node": "checks", "payload": leaky_result()})
        self.assertNotIn("hunter2hunter2", self.said(bus))

    def test_the_kind_and_the_node_are_left_alone(self) -> None:
        bus = EventBus()
        bus.add({"kind": "qa_result", "node": "checks", "payload": {}})
        event = bus.after(0)[0]
        self.assertEqual(event["kind"], "qa_result")
        self.assertEqual(event["node"], "checks")
        self.assertIn("sequence", event)
        self.assertIn("time", event)

    def test_ordinary_words_go_through_untouched(self) -> None:
        bus = EventBus()
        bus.add({"kind": "qa_result", "node": "checks", "payload": {
            "cases": [{"id": "sign-in", "reasons": ["the button moved, so nothing was clicked"]}],
        }})
        said = self.said(bus)
        self.assertIn("the button moved, so nothing was clicked", said)
        self.assertNotIn("[REDACTED]", said)

    def test_counts_of_tokens_are_numbers_worth_keeping(self) -> None:
        bus = EventBus()
        bus.add({"kind": "usage", "node": "planner",
                 "payload": {"input_tokens": 120, "output_tokens": 45}})
        self.assertEqual(bus.after(0)[0]["payload"], {"input_tokens": 120, "output_tokens": 45})

    def test_the_size_cap_still_works_on_a_cleaned_event(self) -> None:
        bus = EventBus(max_bytes=500)
        bus.add({"kind": "qa_result", "node": "checks", "payload": {"big": "x" * 5000}})
        event = bus.after(0)[0]
        self.assertEqual(event["kind"], "event_omitted")


class KeptResultTests(unittest.TestCase):
    """The copy the panel asks for later is the cleaned one as well."""

    def test_the_kept_result_is_clean(self) -> None:
        import http.client
        import tempfile
        import threading

        from our_harness.server import HarnessHTTPServer

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
            server = HarnessHTTPServer(("127.0.0.1", 0), LoadedConfig(data, root, [], {}))
            threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            ).start()
            try:
                server.qa_result = server.events.redactor.value(leaky_result())
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                connection.request(
                    "GET", "/api/qa/result", None,
                    {"Host": f"127.0.0.1:{server.server_port}", "X-Harness-Token": server.token},
                )
                answer = connection.getresponse()
                body = answer.read().decode("utf-8")
                connection.close()
                self.assertEqual(answer.status, 200, body)
                for secret in SECRETS:
                    with self.subTest(secret=secret[:16]):
                        self.assertNotIn(secret, body)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
