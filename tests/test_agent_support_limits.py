"""Nexus supports agents: generous machine limits, no harness-side vetoes.

Covers the "Agents lead; Nexus only supports" policy for command/write limits,
project-hash cost, proposal size, saved denials, closeout feedback and repeated
tool results. Genuine protections (confinement, explicit denials, read-only)
are asserted alongside each widened limit.
"""
from __future__ import annotations

import copy
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import facilitator, goal_access, goal_tools, long_horizon, project_operations as ops, swarm_work
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def _config(root: Path) -> LoadedConfig:
    return LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})


class CommandAndWriteLimits(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = _config(self.root)

    def test_advertised_limits_are_generous(self):
        for definitions in (goal_tools.DEFINITIONS, goal_tools.FACILITATOR_DEFINITIONS):
            command, write = definitions
            self.assertGreaterEqual(command["input_schema"]["properties"]["timeout_seconds"]["maximum"], 3600)
            self.assertGreaterEqual(write["input_schema"]["properties"]["content"]["maxLength"], 10_000_000)
        self.assertGreaterEqual(goal_tools.DEFAULT_COMMAND_TIMEOUT_SECONDS, 600)
        # What the description promises is what is stored and shown.
        self.assertEqual(goal_tools.MAX_COMMAND_OUTPUT_BYTES, 200_000)
        self.assertGreaterEqual(long_horizon._tool_result_string_limit("run_command", shown=True) * 2,
                                goal_tools.MAX_COMMAND_OUTPUT_BYTES)

    def test_timeout_default_maximum_and_invalid_values(self):
        self.assertEqual(goal_tools.command_timeout({}), goal_tools.DEFAULT_COMMAND_TIMEOUT_SECONDS)
        self.assertEqual(goal_tools.command_timeout({"timeout_seconds": 3600}), 3600)
        for bad in (0, 3601, 12.5, "60", True):
            with self.subTest(bad=bad), self.assertRaises(HarnessError):
                goal_tools.command_timeout({"timeout_seconds": bad})

    def test_requested_timeout_lifts_the_smaller_runner_default(self):
        self.assertEqual(self.config.get("execution.timeout_seconds"), 180)
        rebound = goal_tools.command_config(self.config, self.root, 2400)
        self.assertEqual(rebound.get("execution.timeout_seconds"), 2400)
        self.assertGreaterEqual(rebound.get("execution.max_output_bytes"), goal_tools.COMMAND_CAPTURE_BYTES)
        # The caller's configuration is never mutated.
        self.assertEqual(self.config.get("execution.timeout_seconds"), 180)
        self.assertEqual(goal_tools.command_config(self.config, self.root, 30).get("execution.timeout_seconds"), 180)

    def test_large_output_returns_its_beginning_and_end_with_a_marker(self):
        script = ("import sys; sys.stdout.write('HEAD-MARK' + 'x' * 3_000_000 + 'TAIL-MARK');"
                  "sys.stderr.write('small error text')")
        result = goal_tools.execute(self.config, self.root, "run_command",
                                    {"argv": [sys.executable, "-c", script], "timeout_seconds": 120})["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["stdout"].startswith("HEAD-MARK"))
        self.assertTrue(result["stdout"].endswith("TAIL-MARK"))
        self.assertIn("Nexus omitted", result["stdout"])
        self.assertLessEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()),
                             goal_tools.MAX_COMMAND_OUTPUT_BYTES + 1_000)
        self.assertEqual(result["stderr"], "small error text")
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["output_bytes"]["stdout"], 3_000_018)

    def test_small_output_is_returned_whole(self):
        result = goal_tools.bounded_output({"stdout": "all", "stderr": "", "output_truncated": False})
        self.assertEqual(result, {"stdout": "all", "stderr": "", "output_truncated": False})

    def test_multi_megabyte_write_file_and_the_machine_bound(self):
        content = "line\n" * 400_000  # 2 MB, twenty times the old limit
        receipt = goal_tools.execute(self.config, self.root, "write_file", {"path": "big.txt", "content": content})
        self.assertEqual(receipt["applied_to"], "private_agent_copy")
        self.assertEqual((self.root / "big.txt").read_text(), content)
        with mock.patch.object(goal_tools, "MAX_WRITE_FILE_CHARS", 10), \
                self.assertRaisesRegex(HarnessError, "machine limit"):
            goal_tools.execute(self.config, self.root, "write_file", {"path": "over.txt", "content": "x" * 11})
        # Confinement is a genuine protection and stays.
        for path in ("../escape.txt", ".git/config"):
            with self.subTest(path=path), self.assertRaises(HarnessError):
                goal_tools.execute(self.config, self.root, "write_file", {"path": path, "content": "no"})
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_facilitator_digest_accepts_long_timeouts_and_keeps_existing_identity(self):
        from our_harness import facilitator_commands
        arguments = {"argv": [sys.executable, "-c", "print(1)"]}
        omitted = facilitator_commands.tool_command_digest(self.root, arguments, self.config)
        # An omitted timeout keeps the digest existing approvals were saved under.
        self.assertEqual(omitted, facilitator_commands.tool_command_digest(
            self.root, {**arguments, "timeout_seconds": 30}, self.config))
        facilitator_commands.tool_command_digest(self.root, {**arguments, "timeout_seconds": 3600}, self.config)
        with self.assertRaises(HarnessError):
            facilitator_commands.tool_command_digest(self.root, {**arguments, "timeout_seconds": 3601}, self.config)


class ProjectHashing(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for relative in ["src/app.py", "node_modules/pkg/index.js", ".venv/lib/site.py", "src/__pycache__/app.pyc",
                         "target/debug/app", ".next/cache/page", "env/pyvenv.cfg", "env/lib/tool.py", "stray.pyc"]:
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_text(relative)
        long_horizon._BASELINE_HASH_CACHE.clear()

    def test_generated_and_dependency_trees_are_not_hashed(self):
        self.assertEqual(sorted(long_horizon._project_baseline_manifest(self.root)), ["src/app.py"])

    def test_unchanged_files_are_hashed_once_and_changes_are_seen(self):
        source = self.root / "src" / "app.py"
        old = 1_000_000_000_000_000_000
        import os
        os.utime(source, ns=(old, old))
        real = swarm_work.file_sha256
        with mock.patch.object(swarm_work, "file_sha256", side_effect=real) as hashed:
            first = long_horizon._project_baseline_manifest(self.root, reuse_hashes=True)
            second = long_horizon._project_baseline_manifest(self.root, reuse_hashes=True)
        self.assertEqual(first, second)
        self.assertEqual(hashed.call_count, 1)
        source.write_text("changed content of a different size")
        third = long_horizon._project_baseline_manifest(self.root)
        self.assertNotEqual(first["src/app.py"], third["src/app.py"])

    def test_same_size_in_place_copy_with_the_source_mtime_is_seen_after_an_effect(self):
        # Copy-Item -Force / copy /Y keep the file ID and carry the source's
        # mtime, so only an uncached scan can see the new content.
        import os
        source = self.root / "src" / "app.py"
        source.write_text("AAAA", encoding="utf-8")
        old = 1_000_000_000_000_000_000
        os.utime(source, ns=(old, old))
        before = long_horizon._project_effect_manifest(self.root, reuse_hashes=True)
        with source.open("r+", encoding="utf-8") as stream:
            stream.write("BBBB")
        os.utime(source, ns=(old, old))
        self.assertEqual(long_horizon._project_effect_manifest(self.root, reuse_hashes=True), before)  # the stale case
        after = long_horizon._project_effect_manifest(self.root)
        self.assertEqual([one["path"] for one in ops.observed_changes(before, after)], ["src/app.py"])
        self.assertEqual(long_horizon._project_baseline_manifest(self.root)["src/app.py"],
                         long_horizon._path_baseline_marker(self.root, "src/app.py"))

    def test_skipped_locations_resolve_targets_and_record_effects(self):
        # An existing file under a build output is never mistaken for missing,
        # and edits there appear in the change record.
        manifest = long_horizon._project_baseline_manifest(self.root)
        self.assertNotIn("target/debug/app", manifest)
        self.assertEqual(long_horizon._observed_baseline(self.root, manifest, "target/debug/app"),
                         long_horizon._path_baseline_marker(self.root, "target/debug/app"))
        self.assertEqual(long_horizon._observed_baseline(self.root, manifest, "env/lib/tool.py"),
                         long_horizon._path_baseline_marker(self.root, "env/lib/tool.py"))
        self.assertEqual(long_horizon._observed_baseline(self.root, manifest, "src/new.py"), "missing")
        before = long_horizon._project_effect_manifest(self.root, reuse_hashes=True)
        (self.root / "target" / "debug" / "app").write_text("rebuilt")
        (self.root / ".next" / "server.js").write_text("built page")
        observed = ops.observed_changes(before, long_horizon._project_effect_manifest(self.root))
        self.assertEqual(sorted(one["path"] for one in observed), [".next/server.js", "target/debug/app"])


class ProposalAndDenialScope(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "arbitrary-project"
        self.root.mkdir()

    def test_a_large_proposal_applies_in_one_transaction(self):
        self.assertGreaterEqual(ops.MAX_PROPOSAL_FILES, 500)
        changes = [{"path": f"generated/file-{number:03}.txt", "content": str(number), "reason": "Requested"}
                   for number in range(40)]
        store = mock.Mock(root=self.base / "runtime")
        store.uncoordinated_writers.return_value = []
        runtime = mock.Mock(store=store, config=_config(self.root), external_project_conflicts=None)
        goal = {"goal_id": "portable", "project": {"path": str(self.root)}}
        action = {"changes": changes, "_nexus_baselines": {one["path"]: "missing" for one in changes}}
        artifact = ops.apply_proposal(runtime, goal, {"id": "task"}, action)
        self.assertEqual(len(artifact["changes"]), 40)
        self.assertEqual((self.root / "generated" / "file-039.txt").read_text(), "39")

    def test_a_saved_deny_applies_only_to_the_denied_command(self):
        goal = {"goal_id": "g", "project": {"id": "p", "path": str(self.root)}, "agents": []}
        goal["agent_access"] = {"schema_version": 1, "binding": goal_access.binding(goal), "mode": "full",
                                "grants": {"d" * 64: {"decision": "deny", "remaining": 0, "commands": [["rm", "-rf", "dist"]]}}}
        self.assertEqual(facilitator.native_profile(goal, True), "work")
        self.assertEqual(facilitator.denied_commands(goal), [{"approval_digest": "d" * 64, "commands": [["rm", "-rf", "dist"]]}])
        # Explicit user restrictions still hold.
        self.assertEqual(facilitator.native_profile(goal, False), "inspect")
        goal["agent_access"]["mode"] = "read_only"
        self.assertEqual(facilitator.native_profile(goal, True), "inspect")


class TestsOnlyWhenRequested(unittest.TestCase):
    """No configured checks completes unless the user explicitly asked for tests."""

    def setUp(self):
        from tests import test_long_horizon_verification_policy as policy
        self.policy = policy
        policy.LongHorizonVerificationPolicyTests.setUp(self)

    def solo(self, request, objective):
        goal = self.runtime.store.create(self.board, "tiny-game", [objective], request, lead_id="creator")
        self.assertFalse(goal.get("require_all_participants"))
        return goal

    def finish_and_verify(self, goal):
        cases = self.policy.LongHorizonVerificationPolicyTests
        cases.finish_tasks(self, goal)
        return cases.verify(self, goal)

    def test_non_shared_goal_without_checks_completes_and_says_no_tests_ran(self):
        finished = self.finish_and_verify(self.solo("solo-no-tests", "Make the footer say 2026"))
        self.assertEqual(finished["status"], "complete", finished["note"])
        self.assertIn("no tests ran", finished["note"])
        self.assertEqual(finished["verification"]["status"], "not_configured")
        self.assertEqual(finished["verification"]["basis"], "no_selected_checks")
        # The completed record survives restart unchanged.
        self.assertEqual(long_horizon.GoalStore(self.config).get(finished["goal_id"])["status"], "complete")

    def test_explicit_test_request_still_needs_checks(self):
        finished = self.finish_and_verify(self.solo("solo-tests-requested",
                                                    "Add unit tests for the footer and make sure the tests pass"))
        # An explicit request for tests is the user's requirement and stays one.
        self.assertNotEqual(finished["status"], "complete")
        self.assertIn(finished["verification"]["status"], {"failed", "unavailable"}, finished["verification"])
        self.assertTrue(long_horizon.requested_runtime_verification(finished["objective"]))
        self.assertFalse(long_horizon._unconfigured_checks_acceptable(finished))

    def test_prompt_says_tests_only_when_requested(self):
        goal = self.solo("solo-prompt", "Make the footer say 2026")
        task = goal["tasks"][0]
        context = self.runtime._agent_context(goal, task)
        self.assertIn("Tests are required only when the user explicitly asked for them", context)
        self.assertIn("A reasoned no-change result is valid", context)
        self.assertNotIn("need real execution evidence even when the user did not explicitly request tests", context)
        self.assertNotIn("Nexus still requires deterministic project verification", context)


class BudgetWording(unittest.TestCase):
    def test_messages_name_who_set_the_limit(self):
        from our_harness import goal_budget_policy
        source = Path(long_horizon.__file__).read_text(encoding="utf-8")
        self.assertNotIn("The explicit provider-call budget", source)
        self.assertNotIn("The explicit context-tool call budget", source)
        self.assertIn("goal_budget_policy.exhausted_message", source)
        explicit = goal_budget_policy.create_budget({"max_provider_calls": 1}, shared=False)
        self.assertIn("you set", goal_budget_policy.exhausted_message(explicit, "provider_calls"))
        self.assertIn("saved with this older goal", goal_budget_policy.exhausted_message(
            {"max_provider_calls": 7, "provider_calls": 7}, "provider_calls"))


class DenialEnforcement(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_denied_command_matches_any_timeout_or_spelling_in_full_mode(self):
        denied = [["C:/Tools/Python/python.exe", "./scripts/deploy.py", "dist/"]]
        access = {"grants": {"d" * 64: {"decision": "deny", "remaining": 0, "commands": denied}}}
        normal = goal_access.denied_argv(access)
        for spelling in (["python", "scripts/deploy.py", "dist"], ["python.exe", ".\\scripts\\deploy.py", "./dist"]):
            with self.subTest(spelling=spelling):
                self.assertIn(goal_access.normalized_argv(spelling), normal)
        self.assertNotIn(goal_access.normalized_argv(["python", "scripts/build.py"]), normal)

        class Store(goal_access.AccessStoreMixin):
            def __init__(self, document):
                self.document = document
            def _mutate(self, _goal_id, change):
                result = change(self.document, None)
                return self.document, result
            def _event(self, *args, **kwargs):
                pass
        goal = {"goal_id": "g", "project": {"id": "p", "path": str(self.root)}, "agents": [], "revision": 1}
        goal["agent_access"] = {"schema_version": 1, "binding": goal_access.binding(goal), "mode": "full",
                                "grants": {"d" * 64: {"decision": "deny", "remaining": 0, "commands": denied}}}
        store = Store(goal)
        # A new timeout gives a new digest, but the same command stays denied.
        result, _ = store.authorize_commands("g", [["python", "scripts/deploy.py", "dist"]], "e" * 64, "discovered")
        self.assertEqual(result["basis"], "command_access_denied")
        allowed, _ = store.authorize_commands("g", [["python", "scripts/build.py"]], "f" * 64, "discovered")
        self.assertIs(allowed, True)

    def test_native_cli_receives_deny_rules_or_an_advisory_note(self):
        from our_harness.models import ProviderRequest, ProviderWorkspaceContext
        from our_harness.providers import native_execution
        rules, unenforceable = native_execution.claude_deny_rules([("rm", "-rf", "./dist"), ("echo", "a,b")])
        self.assertIn("Bash(rm -rf ./dist)", rules)
        self.assertIn("Bash(rm -rf dist)", rules)
        self.assertEqual(unenforceable, [["echo", "a,b"]])
        context = ProviderWorkspaceContext(project_id="p", project_path=str(self.root), execution_path=str(self.root),
                                           execution_mode="facilitator", denied_commands=(("rm", "-rf", "dist"),))
        request = ProviderRequest("prefix", "dynamic", [{"role": "user", "content": "work"}], "model",
                                  workspace_context=context, native_execution="work", working_directory=str(self.root))
        advisory = native_execution.instructions(request)
        self.assertIn("USER-DENIED COMMANDS", advisory)
        self.assertIn("cannot enforce", advisory)
        self.assertIn("enforces this denial", native_execution.instructions(request, denials_enforced=True))
        config = _config(self.root)
        config.data["providers"] = {"claude": {"kind": "claude-cli", "model": "m"},
                                    "codex": {"kind": "codex-cli", "model": "m"}}
        self.assertEqual(facilitator.deny_enforcement(config, "claude"), "cli_rule")
        self.assertEqual(facilitator.deny_enforcement(config, "codex"), "advisory")
        goal = {"goal_id": "g", "project": {"id": "p", "path": str(self.root)},
                "agents": [{"id": "a", "name": "Ada", "who": "claude"}, {"id": "b", "name": "Bo", "who": "codex"}]}
        goal["agent_access"] = {"schema_version": 1, "binding": goal_access.binding(goal), "mode": "full",
                                "grants": {"d" * 64: {"decision": "deny", "remaining": 0, "commands": [["rm", "-rf", "dist"]]}}}
        report = facilitator.denial_report(goal, config)
        self.assertEqual({one["name"]: one["enforcement"] for one in report[0]["agents"]}, {"Ada": "cli_rule", "Bo": "advisory"})
        self.assertIn("advisory", report[0]["note"])
        # Write access is kept either way.
        self.assertEqual(facilitator.native_profile(goal, True), "work")


class LeaseQueue(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.state = self.base / "state"

    def test_waiters_are_admitted_in_arrival_order(self):
        import threading, time
        order, started = [], []
        release = threading.Event()
        holder_in = threading.Event()
        def holder():
            with ops.claim(self.root, self.state):
                holder_in.set()
                release.wait(10)
        def waiter(name):
            started.append(name)
            with ops.claim(self.root, self.state, wait_seconds=20):
                order.append(name)
                time.sleep(0.05)
        first = threading.Thread(target=holder)
        first.start()
        self.assertTrue(holder_in.wait(10))
        threads = []
        for name in ("one", "two", "three", "four"):
            thread = threading.Thread(target=waiter, args=(name,))
            thread.start()
            threads.append(thread)
            time.sleep(0.4)  # Each waiter has queued before the next arrives.
        release.set()
        for thread in [first, *threads]:
            thread.join(30)
        self.assertEqual(order, ["one", "two", "three", "four"])

    def test_a_cancelled_or_handed_off_waiter_stops_queueing(self):
        from our_harness import cancellation
        token = cancellation.Cancellation()
        token.cancel()
        with ops.claim(self.root, self.state):
            with cancellation.use_unchecked(token) if hasattr(cancellation, "use_unchecked") else _current(token):
                with self.assertRaises(cancellation.ChatCancelled):
                    with ops.claim(self.root, self.state, wait_seconds=60):
                        self.fail("admitted")
        store = mock.Mock()
        store.get.return_value = {"status": "running", "tasks": [{"id": "t", "lease_id": "new-owner"}]}
        stopped = ops._stopped(mock.Mock(store=store), {"goal_id": "g"}, {"id": "t", "lease_id": "old-owner"})
        self.assertTrue(stopped())


def _current(token):
    from contextlib import contextmanager
    from our_harness import cancellation

    @contextmanager
    def held():
        reset = cancellation._CURRENT.set(token)
        try:
            yield token
        finally:
            cancellation._CURRENT.reset(reset)
    return held()


class RepeatGuardAndHistory(unittest.TestCase):
    def test_context_history_keeps_a_window_and_compacts_old_output(self):
        steps = [{"step_id": str(n), "state": "complete",
                  "results": [{"name": "run_command", "result": {"stdout": "x" * 90_000}}]} for n in range(100)]
        steps.insert(3, {"step_id": "pending", "state": "tools_pending", "results": []})
        task = {"context_steps": steps}
        long_horizon._trim_context_steps(task)
        kept = task["context_steps"]
        self.assertEqual(len(kept), long_horizon.CONTEXT_STEP_WINDOW + 1)
        self.assertEqual(kept[0]["step_id"], "pending")  # unfinished steps are never dropped
        self.assertEqual(task["context_steps_trimmed"], 101 - long_horizon.CONTEXT_STEP_WINDOW - 1)
        self.assertEqual(len(kept[-1]["results"][0]["result"]["stdout"]), 90_000)
        self.assertLess(len(kept[1]["results"][0]["result"]["stdout"]), 20_000)
        self.assertTrue(kept[1]["output_compacted"])

    def test_identical_repeat_guard_is_generous(self):
        self.assertGreaterEqual(long_horizon.MAX_IDENTICAL_TOOL_REPEATS, 100)


class ExplicitTestRequests(unittest.TestCase):
    def test_clause_boundaries_and_repair_wording(self):
        from our_harness.goal_verification import tests_explicitly_requested as asked
        for text in ("Don't touch the backend but add unit tests", "no new dependencies, and add unit tests",
                     "Do not change the public API, and make sure the tests pass", "Fix the failing tests",
                     "The tests are failing; fix them", "Update the tests for the new API",
                     "write a failing test first"):
            with self.subTest(text=text):
                self.assertTrue(asked(text))
        for text in ("no tests needed", "don't write tests", "Make the footer say 2026",
                     "build a typing test game", "Do not add tests", "skip the tests"):
            with self.subTest(text=text):
                self.assertFalse(asked(text))


class SkippedLocationWrites(unittest.TestCase):
    def test_write_to_an_existing_build_output_does_not_conflict(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        root = base / "project"
        (root / "target").mkdir(parents=True)
        (root / "target" / "app.js").write_text("old build")
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "patch.js").write_text("old")
        manifest = long_horizon._project_baseline_manifest(root)
        config = _config(root)
        for path in ("target/app.js", "node_modules/pkg/patch.js"):
            with self.subTest(path=path):
                result = goal_tools.execute(config, root, "write_file", {"path": path, "content": "new " + path},
                                            facilitator=True, expected_baselines=manifest, runtime_root=base / "state")
                self.assertEqual(result.get("applied_to"), "selected_project", result)
                self.assertEqual([one["path"] for one in result["observed_changes"]], [path])
                self.assertEqual((root / path).read_text(), "new " + path)


class RoundThree(unittest.TestCase):
    def test_shown_tool_results_fit_one_budget_newest_first(self):
        results = [{"call_id": f"c{n}", "name": "run_command", "result": {"stdout": "x" * 150_000, "stderr": ""}}
                   for n in range(40)]
        shown = long_horizon._shown_tool_results(results)
        self.assertEqual(len(shown), 40)
        self.assertLessEqual(len(long_horizon._canonical(shown)), long_horizon.SHOWN_TOOL_RESULTS_BUDGET + 2_000)
        newest = shown[-1]["result"]["stdout"]
        self.assertGreater(len(newest), 50_000)  # the newest result is shown at its full budget
        self.assertTrue(shown[0].get("omitted"))
        self.assertIn("call the tool again", shown[0]["note"])
        # Small results are all shown whole.
        small = [{"call_id": str(n), "name": "read_file", "result": {"content": "ok"}} for n in range(80)]
        self.assertEqual(long_horizon._shown_tool_results(small), small)

    def test_deny_rules_cover_spellings_and_never_widen(self):
        from our_harness.providers import native_execution
        rules, unenforceable = native_execution.claude_deny_rules([
            ("C:/tools/nodejs/npm.cmd", "test"), ("echo", "*"), ("git", "commit", "-m", "two words"),
            ("say", "'quoted'")])
        for rule in ("Bash(npm test)", "Bash(npm.cmd test)", "Bash(npm.exe test)",
                     "PowerShell(npm test)", "PowerShell(npm.cmd test)"):
            self.assertIn(rule, rules)
        self.assertFalse(any("*:*" in rule or "echo" in rule for rule in rules))
        self.assertEqual(unenforceable, [["echo", "*"], ["git", "commit", "-m", "two words"], ["say", "'quoted'"]])
        from our_harness.models import ProviderRequest, ProviderWorkspaceContext
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = str(Path(temporary.name).resolve())
        context = ProviderWorkspaceContext(project_id="p", project_path=root, execution_path=root,
                                           execution_mode="facilitator", denied_commands=(("npm", "test"),))
        request = ProviderRequest("prefix", "dynamic", [{"role": "user", "content": "work"}], "model",
                                  workspace_context=context, native_execution="work", working_directory=root)
        for enforced in (True, False):
            self.assertIn("You must not run them, in any spelling.",
                          native_execution.instructions(request, denials_enforced=enforced))
        config = _config(Path(root))
        config.data["providers"] = {"claude": {"kind": "claude-cli", "model": "m"}}
        goal = {"goal_id": "g", "project": {"id": "p", "path": root}, "agents": [{"id": "a", "name": "Ada", "who": "claude"}]}
        goal["agent_access"] = {"schema_version": 1, "binding": goal_access.binding(goal), "mode": "full",
                                "grants": {"d" * 64: {"decision": "deny", "remaining": 0, "commands": [["echo", "*"]]}}}
        report = facilitator.denial_report(goal, config)
        self.assertEqual(report[0]["agents"][0]["enforcement"], "advisory")
        self.assertEqual(report[0]["unenforceable_by_cli_rules"], [["echo", "*"]])

    def test_stale_or_busy_lease_tickets_never_block_or_crash(self):
        import os, sqlite3, time, uuid
        from contextlib import closing
        from our_harness.pipeline_runs import _process_token
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        project, state = base / "project", base / "state"
        project.mkdir()
        state.mkdir()
        database = state / "project-operations.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            ops._schema(db)
            # A live process's ticket that stopped being refreshed (its drop failed).
            db.execute("INSERT INTO operation_waiters(lease, root, pid, token, last_seen) VALUES(?,?,?,?,?)",
                       (uuid.uuid4().hex, str(project), os.getpid(), _process_token(os.getpid()),
                        time.time() - ops.TICKET_STALE_SECONDS - 5))
        with ops.claim(project, state, wait_seconds=0):
            pass
        real = ops._try_claim_once
        calls = []
        def flaky(*args):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*args)
        with mock.patch.object(ops, "_try_claim_once", side_effect=flaky):
            with ops.claim(project, state, wait_seconds=5):
                pass
        self.assertGreaterEqual(len(calls), 2)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operation_waiters").fetchone()[0], 0)

    def test_negations_about_something_else_keep_the_test_request(self):
        from our_harness.goal_verification import tests_explicitly_requested as asked
        for text in ("Don't forget to add unit tests", "Do not finish until the tests pass", "Never skip the tests",
                     "Don't stop until all tests pass", "Make sure not to ship without tests passing",
                     "Fix the login bug and don't forget to run the test suite"):
            with self.subTest(text=text):
                self.assertTrue(asked(text))
        for text in ("Skip the unit tests for now", "Don't add tests", "Build it without any unit tests",
                     "No tests needed, just fix the footer"):
            with self.subTest(text=text):
                self.assertFalse(asked(text))

    def test_trimmed_steps_keep_requested_files_and_receipts(self):
        steps = [{"step_id": str(n), "state": "complete", "requested_files": [f"file-{n}.txt"],
                  "results": [{"call_id": f"v{n}", "name": "run_selected_verification", "result": {"status": "passed"}}]}
                 for n in range(long_horizon.CONTEXT_STEP_WINDOW + 10)]
        task = {"context_steps": steps}
        long_horizon._trim_context_steps(task)
        self.assertEqual(task["trimmed_requested_files"], [f"file-{n}.txt" for n in range(10)])
        self.assertEqual([one["call_id"] for one in task["trimmed_receipts"]], [f"v{n}" for n in range(10)])
        self.assertEqual(task["trimmed_receipts"][0]["result"], {"status": "passed"})


class RoundFour(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def test_exact_rules_and_cmd_metacharacters(self):
        from our_harness.providers import native_execution
        rules, unenforceable = native_execution.claude_deny_rules([("git",), ("npm", "install")])
        self.assertIn("Bash(git)", rules)
        self.assertIn("PowerShell(npm install)", rules)
        self.assertFalse(any(rule.endswith(":*)") for rule in rules), rules)
        for argv in (("echo", "%PATH%"), ("run", "a^b"), ("say", "hi!")):
            with self.subTest(argv=argv):
                self.assertEqual(native_execution.claude_deny_rules([argv]), ([], [list(argv)]))

    def test_previous_tool_effects_share_the_prompt_budget(self):
        big = {"stdout": "x" * 150_000, "stderr": ""}
        effects = [{"call_id": f"c{n}", "name": "run_command", "result": big} for n in range(4)]
        shown = long_horizon._shown_tool_results(effects, long_horizon.PREVIOUS_EFFECTS_BUDGET)
        self.assertLessEqual(len(long_horizon._canonical(shown)), long_horizon.PREVIOUS_EFFECTS_BUDGET + 2_000)
        self.assertLessEqual(long_horizon.PREVIOUS_EFFECTS_BUDGET, long_horizon.SHOWN_TOOL_RESULTS_BUDGET // 2)
        # The facilitator prompt uses it (rendered through the real context builder).
        import inspect
        self.assertIn("_shown_tool_results(", inspect.getsource(facilitator.context))

    def test_queued_tickets_stay_fresh_during_a_long_hold(self):
        import threading, time
        project, state = self.base / "project", self.base / "state"
        project.mkdir()
        order, holder_in, release = [], threading.Event(), threading.Event()
        def holder():
            with ops.claim(project, state):
                holder_in.set()
                release.wait(20)
        def waiter(name):
            with ops.claim(project, state, wait_seconds=30):
                order.append(name)
        with mock.patch.object(ops, "TICKET_STALE_SECONDS", 0.5):
            first = threading.Thread(target=holder)
            first.start()
            self.assertTrue(holder_in.wait(10))
            older = threading.Thread(target=waiter, args=("older",))
            older.start()
            time.sleep(0.3)
            newer = threading.Thread(target=waiter, args=("newer",))
            newer.start()
            time.sleep(1.5)  # far longer than the staleness window
            release.set()
            for thread in (first, older, newer):
                thread.join(30)
        self.assertEqual(order, ["older", "newer"])

    def test_trimmed_snapshot_receipt_is_still_found(self):
        from our_harness import workspace_collaboration as wc
        steps = [{"step_id": "s0", "state": "complete", "results": [{"call_id": "c0", "name": "workspace_snapshot",
                  "result": {"snapshot_id": "snap-1", "path": str(self.base / "elsewhere"), "fingerprint": "f",
                             "files": {f"f{n}": {} for n in range(500)}}}]}]
        steps += [{"step_id": f"s{n}", "state": "complete", "results": []} for n in range(1, 70)]
        task = {"id": "t", "context_steps": steps}
        long_horizon._trim_context_steps(task)
        self.assertEqual(task["trimmed_receipts"][0]["result"],
                         {"snapshot_id": "snap-1", "path": str(self.base / "elsewhere"), "fingerprint": "f"})
        with self.assertRaises(HarnessError) as caught:
            wc.load_snapshot({"goal_id": "g", "tasks": [task]}, self.base / "runtime", "snap-1")
        # It found the receipt (and then refused the foreign path), rather than
        # saying no snapshot was ever taken.
        self.assertIn("ownership", str(caught.exception))

    def test_dependency_copy_skips_outside_links_cycles_and_oversized_trees(self):
        import subprocess
        from our_harness import goal_workspaces as gw
        source = self.base / "project"
        (source / "node_modules" / "pkg").mkdir(parents=True)
        (source / "node_modules" / "pkg" / "index.js").write_text("ok")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("SECRET")
        def link(at, target):
            if os.name == "nt":
                return subprocess.run(["cmd", "/c", "mklink", "/J", str(at), str(target)],
                                      capture_output=True).returncode == 0
            try:
                at.symlink_to(target, target_is_directory=True)
                return True
            except OSError:
                return False
        if not (link(source / "node_modules" / "linked", outside)
                and link(source / "node_modules" / "pkg" / "node_modules", source / "node_modules")):
            self.skipTest("links unavailable")
        copy_root = self.base / "copy"
        copy_root.mkdir()
        notes = gw._provide_dependency_trees(source, copy_root)
        self.assertEqual((copy_root / "node_modules" / "pkg" / "index.js").read_text(), "ok")
        self.assertFalse((copy_root / "node_modules" / "linked" / "id_rsa").exists())
        self.assertTrue(any("linked" in one and "outside" in one for one in notes), notes)
        self.assertTrue(any("cycle" in one for one in notes), notes)
        # A tree over the machine bound is not copied at all, and says so.
        other = self.base / "copy2"
        other.mkdir()
        with mock.patch.object(gw, "DEPENDENCY_COPY_MAX_FILES", 0):
            notes = gw._provide_dependency_trees(source, other)
        self.assertFalse((other / "node_modules").exists())
        self.assertTrue(any("not copied" in one for one in notes), notes)
        document = {"execution_workspace": {"dependency_trees_unavailable": notes}}
        self.assertIn("were not copied into the private copy", long_horizon._dependency_copy_note(document))


if __name__ == "__main__":
    unittest.main()
