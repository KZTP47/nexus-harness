"""Agents can see the web pages they build, and are told when a rewrite lost work.

Regression for a goal where two agents "looped five times" on a browser game
without ever opening it: index.html loaded its game as a module script, which
browsers refuse from a page opened from disk, so the user saw a blank page.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from our_harness import facilitator, goal_verification, harness_tools, long_horizon, swarm_work, web_preview
from our_harness.models import HarnessError
from our_harness.playwright_runtime import discover_bundled_playwright_runtime

DRAWING = """
const c = document.getElementById('scene'); const g = c.getContext('2d');
for (let i = 0; i < 40; i++) { g.fillStyle = `hsl(${i * 9}, 70%, ${30 + i}%)`; g.fillRect(i * 20, i * 10, 200, 120); }
"""
PAGE = """<!doctype html><html><head><title>Portable page</title></head>
<body style="margin:0;background:#123"><canvas id="scene" width="1280" height="720"></canvas>
<div style="position:absolute;top:8px;left:8px;color:white">HUD 100</div>{script}</body></html>"""
RUNTIME = discover_bundled_playwright_runtime()


def project(script: str, files: dict[str, str] | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="portable-web-preview-"))
    (root / "index.html").write_text(PAGE.replace("{script}", script), encoding="utf-8")
    for name, text in (files or {}).items():
        (root / name).write_text(text, encoding="utf-8")
    return root


@unittest.skipIf(RUNTIME is None, "Nexus's bundled browser is not installed here")
class WebPreviewTests(unittest.TestCase):
    def test_module_page_that_only_works_from_a_server_is_caught(self):
        root = project('<script type="module" src="app.js"></script>', {"app.js": DRAWING})
        result = web_preview.preview(root)
        disk, served = (next(one for one in result["pages"] if one["mode"] == mode) for mode in ("file", "http"))
        self.assertTrue(disk["looks_blank"], disk)
        self.assertTrue(disk["main_canvas"]["looks_blank"], "HUD text over the canvas must not count as drawing")
        self.assertTrue(any("CORS" in one or "file://" in one for one in disk["errors"] + disk["failed_requests"]), disk)
        self.assertFalse(served["looks_blank"], served)
        self.assertEqual(served["error_count"], 0, served)
        self.assertTrue(any("opened from disk" in one for one in result["advice"]), result["advice"])
        for one in result["pages"]:
            self.assertTrue((root / one["screenshot"]).is_file())
            self.assertTrue(one["screenshot"].startswith(".harness/previews/"))

    def test_working_page_and_script_errors(self):
        good = web_preview.preview(project("<script>" + DRAWING + "</script>"))
        self.assertTrue(all(not one["looks_blank"] and one["error_count"] == 0 for one in good["pages"]), good)
        self.assertEqual(good["advice"], [])
        broken = web_preview.preview(project("<script>" + DRAWING + "missingFunction();</script>"))
        self.assertTrue(all(one["error_count"] for one in broken["pages"]))
        self.assertTrue(any("missingFunction" in " ".join(one["errors"]) for one in broken["pages"]))
        self.assertTrue(any("script errors" in one for one in broken["advice"]))

    def test_the_agent_tool_and_goal_verification_use_it(self):
        root = project('<script type="module" src="app.js"></script>', {"app.js": DRAWING})
        summary = goal_verification.page_preview(root)
        self.assertEqual(summary["page"], "index.html")
        words = goal_verification._page_preview_words(summary)
        self.assertRegex(words, r"opened from disk: \d+ script errors, looks blank")
        self.assertIn("served locally: draws without script errors", words)


class WebPreviewBoundaryTests(unittest.TestCase):
    def test_only_pages_inside_the_project(self):
        root = project("")
        (root / "notes.txt").write_text("x", encoding="utf-8")
        for page in ("../index.html", "missing.html", "notes.txt", str(root / "index.html")):
            with self.subTest(page=page), self.assertRaises(HarnessError):
                web_preview.preview(root, page, runtime=object())

    def test_long_path_prefix_is_removed_for_chromium(self):
        self.assertEqual(web_preview._plain_path("\\\\?\\C:\\tools\\chrome.exe"), "C:\\tools\\chrome.exe")
        self.assertEqual(web_preview._plain_path("/opt/tools/chrome"), "/opt/tools/chrome")

    def test_no_page_means_no_preview(self):
        self.assertIsNone(goal_verification.page_preview(Path(tempfile.mkdtemp(prefix="portable-no-page-"))))
        self.assertEqual(goal_verification._page_preview_words(None), "")

    def test_every_agent_schema_offers_the_tool(self):
        self.assertIn("preview_web_page", harness_tools.TOOL_NAMES)
        for schema in (swarm_work.WORK_FORMAT.schema, long_horizon.AGENT_ACTION_FORMAT.schema):
            names = [one["properties"]["name"]["enum"][0] for one in schema["properties"]["tool_calls"]["items"]["anyOf"]]
            self.assertIn("preview_web_page", names)
        self.assertEqual(harness_tools.validate("preview_web_page", {}), {})
        with self.assertRaises(HarnessError):
            harness_tools.validate("preview_web_page", {"wait_ms": 999999})


class ShrunkFileNoticeTests(unittest.TestCase):
    def test_a_cut_off_rewrite_is_reported_until_the_file_recovers(self):
        root = Path(tempfile.mkdtemp(prefix="portable-shrunk-"))
        (root / "game.js").write_text("x" * 7_700, encoding="utf-8")
        (root / "small.js").write_text("y", encoding="utf-8")
        goal = {"artifacts": [
            {"kind": "file_transaction", "transaction_id": "tx-1", "changes": [
                {"path": "game.js", "backup_bytes": 83_000}, {"path": "small.js", "backup_bytes": 900}]},
        ]}
        notices = facilitator.shrunk_files(goal, root)
        self.assertEqual([one["path"] for one in notices], ["game.js"])
        self.assertEqual(notices[0]["earlier_version"], ".harness/backups/tx-1/files/game.js")
        self.assertIn("83000 to 7700 bytes", notices[0]["notice"])
        # Restored: the notice goes away without anyone clearing it.
        (root / "game.js").write_text("x" * 80_000, encoding="utf-8")
        self.assertEqual(facilitator.shrunk_files(goal, root), [])
        # Only the latest change to a file counts: a later normal edit supersedes it.
        (root / "game.js").write_text("x" * 7_700, encoding="utf-8")
        goal["artifacts"].append({"kind": "file_transaction", "transaction_id": "tx-2",
                                  "changes": [{"path": "game.js", "backup_bytes": 9_000}]})
        self.assertEqual(facilitator.shrunk_files(goal, root), [])
        self.assertEqual(facilitator.shrunk_files({}, root), [])


class NativeHabitTests(unittest.TestCase):
    def test_writable_agents_are_told_to_edit_parts_and_look_at_their_work(self):
        from our_harness.providers import native_execution
        text = native_execution._NATIVE_WORK_HABITS
        self.assertIn("never resend a large existing file in full", text)
        self.assertIn("page preview command", text)
        self.assertIn("never call them as one", text)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(RUNTIME is None, "Nexus's bundled browser is not installed here")
class CompletionCheckTests(unittest.TestCase):
    from tests import test_long_horizon_dialogue as _fixtures
    setUp = _fixtures.LongHorizonDialogueTests.setUp
    create = _fixtures.LongHorizonDialogueTests.create
    provider = _fixtures.LongHorizonDialogueTests.provider
    run_replies = _fixtures.LongHorizonDialogueTests.run_replies

    def test_a_done_claim_carries_what_the_page_really_shows(self):
        # Evidence beside the claim, never a gate: the goal still completes.
        (self.project / "index.html").write_text(PAGE.replace("{script}", '<script type="module" src="app.js"></script>'),
                                                 encoding="utf-8")
        (self.project / "app.js").write_text(DRAWING, encoding="utf-8")
        goal = self.create("page-claim", facilitator_mode=True)
        claim = self._fixtures.reply(summary="The game is finished and looks great.",
                                     summary_delivery={"kind": "user", "agent_id": ""})
        result, _seen = self.run_replies(goal, [claim, claim])
        self.assertEqual(result["status"], "complete", result.get("note"))
        checks = [one.get("nexus_check") for one in result["dialogue"]["messages"] if one.get("nexus_check")]
        self.assertTrue(checks, result["dialogue"]["messages"])
        self.assertIn("Opened from disk (how the user opens it): PROBLEM", checks[0]["verdict"])
        self.assertTrue(Path(checks[0]["screenshot"]).is_file())
        # The next agent turn sees it in its context.
        task = next(one for one in result["tasks"])
        self.assertIn("nexus_check", facilitator.context(result, task, self.project, [], {}, "", []))
