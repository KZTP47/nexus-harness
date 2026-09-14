from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from our_harness import goal_context_progress as progress, long_horizon, provider_wait
from our_harness.search_results import usable
from tests import test_harness_tools as harness_fixture


class CatalogTests(unittest.TestCase):
    def test_catalog_matches_task_schema_in_both_modes(self):
        for direct in (False, True):
            for task in ({"kind": "work"}, {"kind": "review", "review_of": "other"}):
                catalog = long_horizon._advertised_tools(task, direct=direct)
                names = [one["name"] for one in catalog]
                variants = long_horizon._agent_action_format(task).schema["properties"]["tool_calls"]["items"]["anyOf"]
                self.assertEqual(names, [one["properties"]["name"]["enum"][0] for one in variants])
                self.assertEqual(len(names), len(set(names)))
                self.assertTrue({"read_file", "fetch_url", "list_tree", "read_local_skill"} <= set(names))
                self.assertEqual("read_proposed_change" in names, task["kind"] == "review")
                reader = next(one for one in catalog if one["name"] == "read_file")
                self.assertIn("path", reader["input_schema"]["properties"])


class SearchAndReaderTests(unittest.TestCase):
    setUp = harness_fixture.HarnessToolTests.setUp
    def test_wrong_skill_reader_guidance_precedes_any_file_access(self):
        with patch.object(self.session, "_stable_regular_bytes") as read:
            for name in ("src/sample.py", "arbitrary-missing-file", "different-placeholder"):
                with self.assertRaisesRegex(Exception, r"\[wrong-skill-reader\].*read_file"):
                    self.tools.execute("read_local_skill", {"path": name})
            read.assert_not_called()

    def test_irrelevant_html_triggers_rss_fallback(self):
        with patch("our_harness.harness_tools.fetch_public", side_effect=[
            {"url": "https://html.duckduckgo.com/", "data": b'<a class="result__a" href="https://radio.example/live">Radio live</a>'},
            {"url": "https://www.bing.com/", "data": b'<rss><channel><item><title>Widget testing</title><link>https://tools.example/testing</link></item></channel></rss>'},
        ]) as fetch:
            result = self.tools.execute("web_search", {"query": "widget testing browser pipeline fixtures automation"})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["results"][0]["url"], "https://tools.example/testing")

    def test_both_unusable_sources_return_recovery_instead_of_success(self):
        with patch("our_harness.harness_tools.fetch_public", return_value={"url": "https://search.example", "data": b'<rss><channel><item><title>Radio live</title><link>https://radio.example/live</link></item></channel></rss>'}):
            with self.assertRaisesRegex(Exception, r"\[search-no-usable-results\].*fetch_url"):
                self.tools.execute("web_search", {"query": "widget testing browser pipeline fixtures automation"})

    def test_site_constraints_and_non_english_queries(self):
        results = [{"title": "Guide", "url": url} for url in (
            "https://docs.example.org/a", "https://example.org.attacker.test/a", "https://other.test/b")]
        self.assertEqual([r["url"] for r in usable("site:example.org guide", results)], [results[0]["url"]])
        self.assertEqual(len(usable("site:example.org OR site:other.test guide", results)), 2)
        self.assertEqual(len(usable("資料 探索", results)), 3)
        synonyms = [{"title": "GitHub Actions documentation", "url": "https://docs.github.com/en/actions"}]
        self.assertEqual(usable("continuous integration", synonyms), synonyms)


class RecoveryProgressTests(unittest.TestCase):
    def step(self, number, *, failed=True, name="read_local_skill"):
        call = {"call_id": str(number), "name": name, "arguments": {"path": f"missing-{number}"}}
        body = {"error": "[wrong-skill-reader] Use read_file"} if failed else {"content": "# Skill"}
        return {"step_id": f"step-{number}", "state": "complete", "calls": [call],
                "results": [{"call_id": str(number), "name": name,
                    "result": {"status": "error" if failed else "ok", "content": json.dumps(body)}}]}

    def observe(self, held, step, binding=None):
        return progress.observe(held, step, binding=binding or {"route": "arbitrary-route-v1"},
            speaker_id="a", messages=[], normalize=long_horizon._semantic_tool_result)

    def test_variant_errors_pause_after_restart_and_changed_route_resets(self):
        held = {}
        for n in range(5):
            held = self.observe(json.loads(json.dumps(held)), self.step(n))
        self.assertEqual(held["state"], "paused")
        self.assertIn("read_file", held["reason"])
        self.assertEqual(self.observe(held, self.step(4)), held)
        self.assertEqual(self.observe(held, self.step(5), {"route": "different"})["state"], "tracking")

    def test_valid_read_resets_typed_failure_and_mixed_new_evidence_does_not_pause(self):
        held = {}
        for n in range(4):
            held = self.observe(held, self.step(n))
        mixed = self.step(4)
        other = self.step(99, failed=False, name="read_file")
        mixed["calls"] += other["calls"]
        mixed["results"] += other["results"]
        self.assertEqual(self.observe(held, mixed)["state"], "tracking")
        held = self.observe(held, self.step(5, failed=False, name="read_file"))
        self.assertEqual(held["recoverable_failures"], {})
        self.assertEqual(self.observe(held, self.step(6))["state"], "tracking")


class WaitObservationTests(unittest.TestCase):
    def test_effect_and_route_fingerprints_and_optional_sink(self):
        task = {"provider_effect_id": "new-effect"}
        observation = {"started_ms": 1000, "timeout_seconds": 10}
        first = provider_wait.record(task, {"route": "alpha"}, observation)
        second = provider_wait.record(task, {"route": "beta"}, observation)
        self.assertNotEqual(first["contract_fingerprint_sha256"], second["contract_fingerprint_sha256"])
        self.assertEqual(first["effect_id"], "new-effect")
        task.update(state="running", provider_effect_state="dispatched", provider_wait=first)
        self.assertTrue(provider_wait.current(task, {"route": "alpha"}))
        self.assertFalse(provider_wait.current(task, {"route": "beta"}))
        task["provider_effect_state"] = "outcome_unknown"
        self.assertFalse(provider_wait.current(task, {"route": "alpha"}))
        provider_wait.notify(lambda _: (_ for _ in ()).throw(RuntimeError("sink failed")))
