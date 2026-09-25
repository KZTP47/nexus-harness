"""Per-agent model choice, the Opus default, longer native turns, limit resume and usage.

The user picks which model each board agent uses; it must reach every place an
agent runs (chats, goals, Work together, Live team) from its next message.
"""
from __future__ import annotations

import copy
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from our_harness import chat, limit_resume, session_usage, swarm
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import ProviderResponse
from our_harness.providers.catalog import model_choices


def config_with_routes(root: Path) -> LoadedConfig:
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
    config.data["providers"] = {"claude": {"kind": "claude-cli", "model": "claude-opus-5"},
                                "codex": {"kind": "codex-cli", "model": "gpt-5.5"}}
    return config


class BoardAgentModelTests(unittest.TestCase):
    def test_model_round_trips_imports_and_is_bounded(self):
        board = swarm.read_it({"agents": [{"id": "a1", "name": "Builder", "who": "claude", "model": " claude-sonnet-5 "},
                                          {"id": "a2", "name": "Reviewer", "who": "codex"}], "projects": []})
        self.assertEqual([one.model for one in board.agents], ["claude-sonnet-5", ""])
        self.assertEqual(board.to_dict()["agents"][0]["model"], "claude-sonnet-5")
        swarm._check_import_shape({"agents": [{"id": "a1", "name": "Builder", "who": "claude", "model": "claude-opus-5"}],
                                   "projects": [], "works_on": [], "talks_to": []})
        with self.assertRaises(swarm.SwarmError):
            swarm.read_it({"agents": [{"id": "a1", "name": "B", "who": "claude", "model": "x" * 200}], "projects": []})

    def test_live_lookup_needs_the_same_agent_on_the_same_route(self):
        board = swarm.read_it({"agents": [{"id": "a1", "name": "Builder", "who": "claude", "model": "claude-sonnet-5"}],
                               "projects": []})
        with mock.patch.object(swarm, "load", return_value=board):
            self.assertEqual(swarm.agent_model("a1", "claude"), "claude-sonnet-5")
            self.assertEqual(swarm.agent_model("a1", "codex"), "")
            self.assertEqual(swarm.agent_model("other", "claude"), "")
        with mock.patch.object(swarm, "load", side_effect=OSError("gone")):
            self.assertEqual(swarm.agent_model("a1", "claude"), "")

    def test_picker_choices_follow_the_route_kind(self):
        claude = [one["id"] for one in model_choices("claude-cli", "claude-opus-5")]
        self.assertIn("claude-opus-5", claude)
        self.assertIn("claude-sonnet-5", claude)
        self.assertEqual(model_choices("codex-cli", "gpt-5.5")[0]["id"], "gpt-5.5")  # current kept first when unlisted
        self.assertEqual(model_choices("ollama", ""), [])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="portable-agent-models-")).resolve()
        (self.root / ".harness").mkdir()
        self.config = config_with_routes(self.root)
        self.seen = []
        test = self

        class Provider:
            structured_retry_is_safe = True

            def complete(self, request):
                test.seen.append(request)
                return ProviderResponse("ready", input_tokens=120, output_tokens=7)

        self.provider = Provider()

    def test_agent_model_overrides_the_route_default_and_is_reported(self):
        with mock.patch.object(chat, "create_provider", return_value=self.provider):
            result = chat.ask_once(self.config, "claude", "hello", model="claude-sonnet-5")
            default = chat.ask_once(self.config, "claude", "hello")
        self.assertEqual([one.model for one in self.seen], ["claude-sonnet-5", "claude-opus-5"])
        self.assertEqual((result["model"], default["model"]), ("claude-sonnet-5", "claude-opus-5"))

    def test_native_work_turns_get_an_hour_and_answers_keep_ten_minutes(self):
        (self.root / "work").mkdir()
        with mock.patch.object(chat, "create_provider", return_value=self.provider):
            chat.ask_once(self.config, "claude", "answer")
            chat.ask_once(self.config, "claude", "work", native_execution="work",
                          working_directory=str(self.root / "work"))
        self.assertEqual([one.timeout_seconds for one in self.seen],
                         [chat.LONGEST_WAIT_SECONDS, chat.NATIVE_WORK_WAIT_SECONDS])
        self.assertGreaterEqual(DEFAULT_CONFIG["provider"]["timeout_seconds"], chat.NATIVE_WORK_WAIT_SECONDS)


class OpusDefaultTests(unittest.TestCase):
    def test_new_claude_routes_default_to_opus_5(self):
        from our_harness import autosetup, seats
        self.assertEqual(seats._default_model(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path("."), [], {}), "claude-cli"),
                         "claude-opus-5")
        self.assertIn('model="claude-opus-5"', Path(autosetup.__file__).read_text(encoding="utf-8"))


class LimitResumeTests(unittest.TestCase):
    def goal(self, error, *, note_prefix=limit_resume.PROVIDER_PAUSE, paused=None):
        paused = paused or time.time()
        return {"status": "paused", "note": f"{note_prefix}. Review the failure: {error}", "updated_ms": int(paused * 1000),
                "tasks": [{"last_error": error}]}

    def test_reset_times_are_read_from_provider_words(self):
        now = datetime(2026, 9, 25, 10, 0).astimezone().timestamp()
        self.assertAlmostEqual(limit_resume.reset_after("Usage limit reached. Try again in 2h 13m.", now),
                               now + 2 * 3600 + 13 * 60 + limit_resume.MARGIN_SECONDS)
        self.assertAlmostEqual(limit_resume.reset_after("429 Too Many Requests. Retry-After: 90", now),
                               now + 90 + limit_resume.MARGIN_SECONDS)
        five = limit_resume.reset_after("You've hit your limit · resets 5pm", now)
        self.assertEqual(datetime.fromtimestamp(five - limit_resume.MARGIN_SECONDS).hour, 17)
        early = limit_resume.reset_after("You've hit your limit · resets 9am", now)
        self.assertGreater(early, now + 20 * 3600)  # already past today: tomorrow
        stamp = (datetime.fromtimestamp(now) + timedelta(hours=3)).astimezone().isoformat()
        self.assertAlmostEqual(limit_resume.reset_after(f"rate limit until {stamp}", now), now + 3 * 3600 + limit_resume.MARGIN_SECONDS, delta=1)
        self.assertEqual(limit_resume.reset_after("usage limit reached", now), now + limit_resume.FALLBACK_SECONDS)

    def test_only_provider_limit_pauses_are_planned(self):
        self.assertIsNotNone(limit_resume.plan(self.goal("Claude usage limit reached; resets 5pm")))
        self.assertIsNone(limit_resume.plan(self.goal("codex timed out")))
        self.assertIsNone(limit_resume.plan(self.goal("usage limit", note_prefix="Paused by you")))
        self.assertIsNone(limit_resume.plan({**self.goal("usage limit"), "status": "running"}))
        self.assertTrue(limit_resume.plan(self.goal("usage limit"), attempt=limit_resume.MAX_AUTOMATIC_RESUMES)["gives_up"])

    def test_server_resumes_once_after_the_reset_and_never_early(self):
        from our_harness.server import HarnessHTTPServer
        paused = time.time() - 3600
        goal = {**self.goal("usage limit reached. Try again in 10 minutes", paused=paused), "goal_id": "g1"}
        server = mock.Mock(spec=["long_horizon", "_limit_attempts", "_limit_seen", "_resume_paused_goal"])
        server.long_horizon.store.list.return_value = [goal]
        server._limit_attempts, server._limit_seen = {}, {}
        server._resume_paused_goal.return_value = True
        self.assertEqual(HarnessHTTPServer.resume_limited_goals(server, now=paused + 60), [])
        self.assertEqual(HarnessHTTPServer.resume_limited_goals(server, now=paused + 3600), ["g1"])
        self.assertEqual(HarnessHTTPServer.resume_limited_goals(server, now=paused + 3700), [])  # same pause only once
        self.assertEqual(server._resume_paused_goal.call_count, 1)


class CompletionCheckDisplayTests(unittest.TestCase):
    def test_saved_chat_turns_keep_the_nexus_check(self):
        kept = chat._said_correlation({"schema_version": 1, "nexus_check_verdict": "Opened from disk: PROBLEM - blank.",
                                       "nexus_check_page": "index.html", "nexus_check_screenshot": "C:/p/.harness/previews/i.png"})
        self.assertEqual(kept["nexus_check_verdict"], "Opened from disk: PROBLEM - blank.")
        self.assertEqual(kept["nexus_check_page"], "index.html")


class SessionUsageTests(unittest.TestCase):
    def test_each_call_counts_once_and_unknown_counts_are_kept_apart(self):
        where = Path(tempfile.mkdtemp(prefix="portable-usage-")) / "session-usage.sqlite3"
        with mock.patch.object(session_usage, "_where", return_value=where):
            session_usage.record("goal:g1", route="claude", model="claude-opus-5",
                                 response=ProviderResponse("a", input_tokens=100, output_tokens=10), call_id="c1")
            session_usage.record("goal:g1", route="claude", model="claude-opus-5",
                                 response=ProviderResponse("a", input_tokens=100, output_tokens=10), call_id="c1")
            session_usage.record("goal:g1", route="codex", model="gpt-5.5", response=ProviderResponse("b"), call_id="c2")
            session_usage.record("", route="claude", model="x", response=None)
            total = session_usage.summary("goal:g1")
        self.assertEqual((total["calls"], total["input_tokens"], total["output_tokens"], total["calls_without_token_counts"]),
                         (2, 100, 10, 1))
        self.assertEqual([one["model"] for one in total["by_model"]], ["claude-opus-5", "gpt-5.5"])

    def test_live_team_usage_adds_claude_turns_and_tracks_codex_totals(self):
        from our_harness.agent_runtime.team import TeamRun
        team = TeamRun.__new__(TeamRun)
        team.usage = {}
        import threading
        team._lock = threading.RLock()
        team._count_usage("claude", {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5})
        team._count_usage("claude", {"input_tokens": 1, "output_tokens": 1})
        team._count_usage("codex", {"total": {"inputTokens": 500, "outputTokens": 50}})
        team._count_usage("codex", {"total": {"inputTokens": 800, "outputTokens": 70}})
        self.assertEqual((team.usage["claude"]["input_tokens"], team.usage["claude"]["output_tokens"]), (101, 6))
        self.assertEqual((team.usage["codex"]["input_tokens"], team.usage["codex"]["output_tokens"]), (800, 70))


if __name__ == "__main__":
    unittest.main()
