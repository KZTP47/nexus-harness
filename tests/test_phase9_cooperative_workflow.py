from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.config import load_isolated_config
from our_harness.models import CommandResult, HarnessError, ProviderResponse
from our_harness.workflow import HarnessApplication


POSITIVE_TEST_COMMAND = [
    "py", "-c",
    "import json; print(json.dumps({'summary': {'executed': 1, 'failed': 0}}))",
]
POSITIVE_TEST_STDOUT = '{"summary": {"executed": 1, "failed": 0}}\n'


def positive_test_project() -> dict:
    """Trusted fixture contract proving that one custom-runner test executed."""
    return {
        "test_commands": [list(POSITIVE_TEST_COMMAND)],
        "test_evidence_contracts": [{
            "command": list(POSITIVE_TEST_COMMAND),
            "format": "json-stdout",
            "total_field": "summary.executed",
            "failed_field": "summary.failed",
        }],
    }


def agent(route: str, role: str) -> dict:
    return {
        "provider_route": route,
        "model": f"{route}-model",
        "role_name": role,
        "system_prompt": f"Act only as {role}.",
        "capabilities": ["workspace.read"],
    }


def cooperative_graph() -> dict:
    return {
        "schema_version": 2,
        "name": "parallel-plan-merge-tail",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "plan_a", "type": "planner", "config": agent("route_a", "Planner A")},
            {"id": "plan_b", "type": "planner", "config": agent("route_b", "Planner B")},
            {"id": "merge", "type": "merge", "config": {**agent("route_a", "Plan merger"), "required_slots": ["a", "b"], "output_field": "merged_output", "output_contract": "implementation_plan"}},
            {"id": "coder", "type": "coder", "config": agent("route_b", "Coder")},
            {"id": "lint", "type": "tool", "config": {"role": "syntax"}},
            {"id": "test", "type": "tool", "config": {"role": "unit_test"}},
            {"id": "reviewer", "type": "evaluator", "config": agent("route_a", "Reviewer")},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "start-a", "source": "start", "target": "plan_a", "mode": "state", "variables": ["task"]},
            {"id": "start-b", "source": "start", "target": "plan_b", "mode": "state", "variables": ["task"]},
            {"id": "a-merge", "source": "plan_a", "target": "merge", "mode": "merge_input", "target_slot": "a", "variables": ["plan"]},
            {"id": "b-merge", "source": "plan_b", "target": "merge", "mode": "merge_input", "target_slot": "b", "variables": ["plan"]},
            {"id": "merge-code", "source": "merge", "target": "coder", "mode": "state", "variables": ["plan"]},
            {"id": "code-lint", "source": "coder", "target": "lint", "mode": "state", "variables": ["plan", "candidate", "source_code"]},
            {"id": "lint-test", "source": "lint", "target": "test", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed"]},
            {"id": "test-review", "source": "test", "target": "reviewer", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed"]},
            {"id": "review-end", "source": "reviewer", "target": "end", "mode": "state", "condition": "review_passed == true", "variables": ["review"]},
        ],
    }


def parallel_evaluator_graph(*, same_route: bool = False) -> dict:
    graph = cooperative_graph()
    graph["name"] = "parallel-evaluators"
    reviewer = next(node for node in graph["nodes"] if node["id"] == "reviewer")
    reviewer["config"] = agent("route_a", "Reviewer A")
    route = "route_a" if same_route else "route_b"
    graph["nodes"].insert(-1, {"id": "reviewer_b", "type": "evaluator", "config": agent(route, "Reviewer B")})
    graph["edges"].append({
        "id": "test-review-b", "source": "test", "target": "reviewer_b", "mode": "state",
        "condition": "stage_passed == true", "variables": ["verification", "tests_passed"],
    })
    graph["edges"].append({
        "id": "review-b-end", "source": "reviewer_b", "target": "end", "mode": "state",
        "condition": "review_passed == true", "variables": ["review"],
    })
    return graph


def pure_evaluator_fanout_graph(*, same_route: bool = False) -> dict:
    route = "route_a" if same_route else "route_b"
    return {
        "schema_version": 2,
        "name": "pure-evaluator-fanout",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "planner", "type": "planner", "config": agent("route_a", "Planner")},
            {"id": "coder", "type": "coder", "config": agent("route_b", "Coder")},
            {"id": "lint", "type": "tool", "config": {"role": "syntax"}},
            {"id": "test", "type": "tool", "config": {"role": "unit_test"}},
            {"id": "reviewer", "type": "evaluator", "config": agent("route_a", "Reviewer A")},
            {"id": "reviewer_b", "type": "evaluator", "config": agent(route, "Reviewer B")},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "start-plan", "source": "start", "target": "planner", "mode": "state", "variables": ["task"]},
            {"id": "plan-code", "source": "planner", "target": "coder", "mode": "state", "variables": ["plan"]},
            {"id": "code-lint", "source": "coder", "target": "lint", "mode": "state", "variables": ["plan", "candidate"]},
            {"id": "lint-test", "source": "lint", "target": "test", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed"]},
            {"id": "test-review", "source": "test", "target": "reviewer", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed"]},
            {"id": "test-review-b", "source": "test", "target": "reviewer_b", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed"]},
            {"id": "review-end", "source": "reviewer", "target": "end", "mode": "state", "condition": "review_passed == true", "variables": ["review"]},
            {"id": "review-b-end", "source": "reviewer_b", "target": "end", "mode": "state", "condition": "review_passed == true", "variables": ["review"]},
        ],
    }


def mixed_ready_provider_graph() -> dict:
    return {
        "schema_version": 2,
        "name": "mixed-ready-provider-agents",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "initial_plan", "type": "planner", "config": agent("route_a", "Initial planner")},
            {"id": "coder", "type": "coder", "config": agent("route_b", "Coder")},
            {"id": "lint", "type": "tool", "config": {"role": "syntax"}},
            {"id": "test", "type": "tool", "config": {"role": "unit_test"}},
            {"id": "replan", "type": "planner", "config": agent("route_a", "Replanner")},
            {"id": "reviewer", "type": "evaluator", "config": agent("route_b", "Reviewer")},
            {"id": "merge", "type": "merge", "config": {
                **agent("route_a", "Plan merger"), "required_slots": ["strategy", "review"],
                "output_field": "merged_output", "output_contract": "implementation_plan",
            }},
            {"id": "final_review", "type": "evaluator", "config": agent("route_b", "Final reviewer")},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "start-plan", "source": "start", "target": "initial_plan", "mode": "state", "variables": ["task"]},
            {"id": "plan-code", "source": "initial_plan", "target": "coder", "mode": "state", "variables": ["plan"]},
            {"id": "code-lint", "source": "coder", "target": "lint", "mode": "state", "variables": ["plan", "candidate"]},
            {"id": "lint-test", "source": "lint", "target": "test", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed", "plan", "candidate"]},
            {"id": "test-replan", "source": "test", "target": "replan", "mode": "delegate", "condition": "stage_passed == true", "variables": ["verification", "candidate", "source_code"], "return_fields": ["plan"]},
            {"id": "test-review", "source": "test", "target": "reviewer", "mode": "state", "condition": "stage_passed == true", "variables": ["verification", "tests_passed"]},
            {"id": "replan-merge", "source": "replan", "target": "merge", "mode": "merge_input", "target_slot": "strategy", "variables": ["plan"]},
            {"id": "review-merge", "source": "reviewer", "target": "merge", "mode": "merge_input", "target_slot": "review", "variables": ["review"]},
            {"id": "merge-final-review", "source": "merge", "target": "final_review", "mode": "state", "condition": "plan_ready == true", "variables": ["plan", "candidate", "verification", "tests_passed"]},
            {"id": "final-review-end", "source": "final_review", "target": "end", "mode": "state", "condition": "review_passed == true", "variables": ["review"]},
        ],
    }


class RoutedFixtureProvider:
    def __init__(self, tracker: dict):
        self.tracker = tracker

    def stream(self, request):
        name = request.response_format.name if request.response_format else ""
        if name == "harness_planner_wire_v3":
            with self.tracker["lock"]:
                self.tracker["active"] += 1
                self.tracker["max_active"] = max(self.tracker["max_active"], self.tracker["active"])
            time.sleep(0.08)
            with self.tracker["lock"]:
                self.tracker["active"] -= 1
            value = {
                "summary": f"plan from {request.model}", "requirement_ledger": [{
                    "id": "R1", "requirement": "tests pass",
                    "category": "behavior", "counterexample": "R1: the fixture tests fail",
                }],
                "non_goals": [], "files": list(self.tracker.get("files", ["fixture.py"])),
                "verification_commands": [] if self.tracker.get("empty_verification") else [list(POSITIVE_TEST_COMMAND)], "risks": [],
            }
        elif name.startswith("harness_merge_"):
            prompt = request.messages[-1]["content"]
            self.tracker["merge_prompt"] = prompt
            value = {"merged_output": {
                "summary": "merged plan", "requirement_ledger": [{
                    "id": "R1", "requirement": "tests pass",
                    "category": "behavior", "counterexample": "R1: the fixture tests fail",
                }],
                "non_goals": [], "files": list(self.tracker.get("files", ["fixture.py"])),
                "verification_commands": [] if self.tracker.get("empty_verification") else [list(POSITIVE_TEST_COMMAND)], "risks": [],
            }}
        elif name.startswith("harness_coder_wire_v3"):
            paths = list(self.tracker.get("files", ["fixture.py"]))
            evidence_path = paths[0] if paths else "project"
            value = {
                "summary": "candidate", "changes": list(self.tracker.get("changes", [])),
                "commands": [], "review": {"verdict": "PASS", "findings": [{
                    "requirement_id": "R1", "file": evidence_path, "code_path": "implementation for R1",
                    "counterexample_result": "The fixture counterexample passes",
                }]}, "memory": [],
            }
        elif name == "harness_reviewer_v1":
            value = {"verdict": "PASS", "findings": [], "residual_risks": []}
        else:
            raise AssertionError(f"Unexpected response format: {name}")
        self.tracker["requests"].append((request.model, name, request.system_prefix, request.messages[-1]["content"]))
        yield {"type": "text_delta", "text": json.dumps(value)}
        yield {"type": "usage", "input_tokens": 10, "output_tokens": 3}
        yield {"type": "done", "finish_reason": "stop"}


class ParallelReviewerFixtureProvider(RoutedFixtureProvider):
    def stream(self, request):
        name = request.response_format.name if request.response_format else ""
        if name == "harness_reviewer_v1":
            role = "reviewer_b" if "Reviewer B" in request.system_prefix else "reviewer_a"
            with self.tracker["lock"]:
                self.tracker["active"] += 1
                self.tracker["max_active"] = max(self.tracker["max_active"], self.tracker["active"])
                self.tracker["reviewer_active"] = self.tracker.get("reviewer_active", 0) + 1
                self.tracker["review_max_active"] = max(
                    self.tracker.get("review_max_active", 0), self.tracker["reviewer_active"]
                )
                self.tracker.setdefault("review_calls", []).append(role)
            time.sleep(0.08)
            with self.tracker["lock"]:
                self.tracker["active"] -= 1
                self.tracker["reviewer_active"] -= 1
        yield from super().stream(request)


class MixedVerdictFixtureProvider(RoutedFixtureProvider):
    def stream(self, request):
        name = request.response_format.name if request.response_format else ""
        if name != "harness_reviewer_v1":
            yield from super().stream(request)
            return
        role = "reviewer_b" if "Reviewer B" in request.system_prefix else "reviewer_a"
        with self.tracker["lock"]:
            self.tracker["active"] += 1
            self.tracker["max_active"] = max(self.tracker["max_active"], self.tracker["active"])
            self.tracker.setdefault("review_calls", []).append(role)
        time.sleep(0.08 if role == "reviewer_a" else 0.02)
        with self.tracker["lock"]:
            self.tracker["active"] -= 1
        blocked = role == self.tracker["block_role"]
        value = {
            "verdict": "BLOCK" if blocked else "PASS",
            "findings": [{
                "severity": "blocker", "path": "fixture.py",
                "evidence": f"{role} found the retained blocker", "remedy": "Fix the fixture",
            }] if blocked else [],
            "residual_risks": [f"risk from {role}"] if blocked else [],
        }
        self.tracker["requests"].append((request.model, name, request.system_prefix, request.messages[-1]["content"]))
        yield {"type": "text_delta", "text": json.dumps(value)}
        yield {"type": "usage", "input_tokens": 10, "output_tokens": 3}
        yield {"type": "done", "finish_reason": "stop"}


class StagedFixtureProvider(RoutedFixtureProvider):
    def stream(self, request):
        name = request.response_format.name if request.response_format else ""
        if not name.endswith("_action_v1"):
            yield from super().stream(request)
            return
        round_number = self.tracker.get("staged_round", 0)
        self.tracker["staged_round"] = round_number + 1
        if round_number == 0:
            self.tracker["staged_first_prompt"] = request.messages[-1]["content"]
        if round_number == 0:
            value = {"action": "tool", "tool": {"call_id": "stage-state", "name": "stage_file_state", "arguments": {"path": "sample.txt"}}}
        elif round_number == 1:
            value = {"action": "tool", "tool": {"call_id": "stage-edit", "name": "stage_replace_file", "arguments": {
                "path": "sample.txt", "expected_sha256": self.tracker["baseline"], "content": "after\n", "reason": "fixture",
            }}}
        elif round_number == 2:
            value = {"action": "tool", "tool": {"call_id": "stage-check", "name": "stage_run_verification", "arguments": {"action": "verification-1"}}}
        elif round_number == 3:
            value = {"action": "tool", "tool": {"call_id": "stage-submit", "name": "stage_finalize", "arguments": {}}}
        else:
            value = {"action": "final", "result": {
                "summary": "staged candidate", "changes": [], "commands": [],
                "review": {"verdict": "PASS", "findings": [{
                    "requirement_id": "R1", "file": "sample.txt", "code_path": "file contents",
                    "counterexample_result": "The fixture counterexample passes",
                }]}, "memory": [],
            }}
        self.tracker["requests"].append((request.model, name, request.system_prefix, request.messages[-1]["content"]))
        yield {"type": "text_delta", "text": json.dumps(value)}
        yield {"type": "usage", "input_tokens": 5, "output_tokens": 2}
        yield {"type": "done", "finish_reason": "stop"}


class InvalidPlannerProvider:
    def stream(self, request):
        yield {"type": "text_delta", "text": "{}"}
        yield {"type": "usage", "input_tokens": 11, "output_tokens": 1}
        yield {"type": "done", "finish_reason": "stop"}


class CooperativeWorkflowTests(unittest.TestCase):
    @staticmethod
    def parallel_config(root: Path) -> object:
        return load_isolated_config(root, {
            "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
            "providers": {
                "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
            },
            "project": positive_test_project(),
            "workflow": {"require_review": True, "reviewers": 1},
            "memory": {"embedding_model": ""},
        })

    def test_parallel_evaluators_use_independent_routes_and_commit_in_graph_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}
            checkpoints: list[str] = []
            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: ParallelReviewerFixtureProvider(tracker)):
                with HarnessApplication(self.parallel_config(root), sink=lambda event: checkpoints.append(event["node"]) if event["kind"] == "checkpoint" else None) as app:
                    result = app.run_task("Implement the fixture", graph=pure_evaluator_fanout_graph())
            self.assertEqual(result["state"], "complete")
            self.assertGreaterEqual(tracker["max_active"], 2)
            self.assertEqual(checkpoints[-3:], ["reviewer", "reviewer_b", "end"])
            review_models = {model for model, name, _prefix, _prompt in tracker["requests"] if name == "harness_reviewer_v1"}
            self.assertEqual(review_models, {"route_a-model", "route_b-model"})

    def test_same_route_parallel_evaluators_obey_max_concurrency_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}
            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: ParallelReviewerFixtureProvider(tracker)):
                with HarnessApplication(self.parallel_config(root)) as app:
                    result = app.run_task("Implement the fixture", graph=pure_evaluator_fanout_graph(same_route=True))
            self.assertEqual(result["state"], "complete")
            self.assertEqual(tracker["max_active"], 1)
            self.assertEqual(tracker["review_max_active"], 1)

    def test_mixed_ready_planner_and_evaluator_overlap_across_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}
            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: ParallelReviewerFixtureProvider(tracker)):
                with HarnessApplication(self.parallel_config(root)) as app:
                    result = app.run_task("Implement the fixture", graph=mixed_ready_provider_graph())
            self.assertEqual(result["state"], "complete")
            self.assertGreaterEqual(tracker["max_active"], 2)
            delegated_prompt = next(
                prompt for _model, name, prefix, prompt in tracker["requests"]
                if name == "harness_planner_wire_v3" and "Replanner" in prefix
            )
            self.assertIn('"delegated_by": "test"', delegated_prompt)

    def test_crash_after_first_parallel_evaluator_commit_resumes_only_uncommitted_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}
            crashed = False

            def sink(event):
                nonlocal crashed
                if not crashed and event["kind"] == "checkpoint" and event["node"] == "reviewer":
                    crashed = True
                    raise KeyboardInterrupt("crash after first evaluator commit")

            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: ParallelReviewerFixtureProvider(tracker)):
                with HarnessApplication(self.parallel_config(root), sink=sink) as app:
                    with self.assertRaises(KeyboardInterrupt):
                        app.run_task("Implement the fixture", graph=pure_evaluator_fanout_graph())
                    run_id = app.memory.list_run_checkpoints()[0].run_id
                before = list(tracker["review_calls"])
                with HarnessApplication(self.parallel_config(root)) as resumed:
                    result = resumed.resume_task(run_id)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(before.count("reviewer_a"), 1)
            self.assertEqual(tracker["review_calls"].count("reviewer_a"), 1)
            self.assertEqual(tracker["review_calls"].count("reviewer_b"), 2)

    def test_parallel_evaluator_block_is_order_independent_and_preserved(self) -> None:
        for blocked_role in ("reviewer_a", "reviewer_b"):
            with self.subTest(blocked_role=blocked_role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
                tracker = {
                    "lock": threading.Lock(), "active": 0, "max_active": 0,
                    "requests": [], "merge_prompt": "", "block_role": blocked_role,
                }
                events: list[dict] = []
                with patch("our_harness.workflow.create_provider", side_effect=lambda _config: MixedVerdictFixtureProvider(tracker)):
                    with HarnessApplication(self.parallel_config(root), sink=events.append) as app:
                        with self.assertRaisesRegex(HarnessError, "no ready work"):
                            app.run_task("Implement the fixture", graph=pure_evaluator_fanout_graph())
                reviews = [event["payload"] for event in events if event["kind"] == "review"]
                self.assertEqual(len(reviews), 2)
                self.assertEqual(reviews[0]["verdict"], "PENDING")
                final = reviews[-1]
                self.assertEqual(final["verdict"], "BLOCK")
                self.assertTrue(final["complete"])
                self.assertEqual(final["required_evaluators"], ["reviewer", "reviewer_b"])
                self.assertEqual(
                    {node: value["verdict"] for node, value in final["by_evaluator"].items()},
                    {"reviewer": "BLOCK" if blocked_role == "reviewer_a" else "PASS",
                     "reviewer_b": "BLOCK" if blocked_role == "reviewer_b" else "PASS"},
                )
                self.assertIn("retained blocker", final["findings"][0]["evidence"])

    def test_blocking_evaluator_survives_crash_before_pass_sibling_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            tracker = {
                "lock": threading.Lock(), "active": 0, "max_active": 0,
                "requests": [], "merge_prompt": "", "block_role": "reviewer_a",
            }
            crashed = False

            def sink(event):
                nonlocal crashed
                if not crashed and event["kind"] == "checkpoint" and event["node"] == "reviewer":
                    crashed = True
                    raise KeyboardInterrupt("crash after durable blocker commit")

            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: MixedVerdictFixtureProvider(tracker)):
                with HarnessApplication(self.parallel_config(root), sink=sink) as app:
                    with self.assertRaises(KeyboardInterrupt):
                        app.run_task("Implement the fixture", graph=pure_evaluator_fanout_graph())
                    run_id = app.memory.list_run_checkpoints()[0].run_id
                with HarnessApplication(self.parallel_config(root)) as resumed:
                    with self.assertRaisesRegex(HarnessError, "no ready work"):
                        resumed.resume_task(run_id)
                    reviews = [
                        event["payload"] for event in resumed.memory.events(run_id)
                        if event["kind"] == "review"
                    ]
            self.assertEqual(tracker["review_calls"].count("reviewer_a"), 1)
            self.assertEqual(tracker["review_calls"].count("reviewer_b"), 2)
            self.assertEqual(reviews[-1]["verdict"], "BLOCK")
            self.assertIn("reviewer_a found the retained blocker", reviews[-1]["findings"][0]["evidence"])

    def test_coder_delegation_replans_with_bounded_typed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            config = load_isolated_config(root, {
                "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
                "providers": {
                    "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            graph = cooperative_graph()
            next(edge for edge in graph["edges"] if edge["id"] == "code-lint")["condition"] = "iteration != 1"
            graph["edges"].append({
                "id": "coder-replan", "source": "coder", "target": "plan_a", "mode": "delegate",
                "condition": "iteration == 1", "variables": ["candidate", "source_code"],
                "return_fields": ["plan"],
                "loop": {"max_iterations": 1, "temperature_decay": 0.8, "timeout_seconds": 60},
            })
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}
            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: RoutedFixtureProvider(tracker)):
                with HarnessApplication(config) as app:
                    result = app.run_task("Implement the fixture", graph=graph)
            planner_prompts = [
                prompt for _model, name, _prefix, prompt in tracker["requests"]
                if name == "harness_planner_wire_v3"
            ]
            self.assertEqual(result["state"], "complete")
            self.assertTrue(any("DELEGATED PLANNER CONTEXT" in prompt for prompt in planner_prompts))
            delegated = next(prompt for prompt in planner_prompts if "DELEGATED PLANNER CONTEXT" in prompt)
            self.assertIn('"delegated_by": "coder"', delegated)
            self.assertIn('"source_code": "candidate"', delegated)

    def test_trusted_agent_capabilities_and_route_data_class_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {
                "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
                "providers": {
                    "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                },
                "agents": {"limited_coder": {"provider_ref": "route_b", "role": "Coder", "capabilities": ["workspace.read"]}},
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            }
            graph = cooperative_graph()
            coder = graph["nodes"][4]
            coder["config"].update({"agent_ref": "limited_coder", "provider_route": "", "model": "", "capabilities": ["workspace.read", "workspace.write"]})
            with HarnessApplication(load_isolated_config(root, base)) as app:
                with self.assertRaisesRegex(HarnessError, "not assigned to trusted agent"):
                    app.run_task("Implement the fixture", graph=graph)

            base["providers"]["route_b"]["max_data_class"] = "public"
            coder["config"].update({"agent_ref": "", "provider_route": "route_b", "capabilities": ["workspace.read"]})
            with HarnessApplication(load_isolated_config(root, base)) as app:
                with self.assertRaisesRegex(HarnessError, "max_data_class public"):
                    app.run_task("Implement the fixture", graph=graph)

    def test_failed_parallel_planners_still_persist_billed_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            config = load_isolated_config(root, {
                "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
                "providers": {
                    "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: InvalidPlannerProvider()):
                with HarnessApplication(config) as app:
                    with self.assertRaisesRegex(HarnessError, "missing"):
                        app.run_task("Implement the fixture", graph=cooperative_graph())
                    persisted = app.memory.usage_records(limit=20)["records"]
            self.assertEqual(len(persisted), 6)
            self.assertTrue(all(item["input_tokens"] == 11 for item in persisted))

    def test_parallel_routes_merge_and_complete_serial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            config = load_isolated_config(root, {
                "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
                "providers": {
                    "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}

            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: RoutedFixtureProvider(tracker)):
                with HarnessApplication(config) as app:
                    result = app.run_task("Implement the fixture", graph=cooperative_graph())
                    shared = app.memory.connection.execute(
                        "SELECT discovered.memory_id FROM memory_provenance discovered "
                        "JOIN memory_provenance consumed ON consumed.memory_kind=discovered.memory_kind "
                        "AND consumed.memory_id=discovered.memory_id "
                        "WHERE discovered.relation='discovered_by' AND discovered.node_id='plan_a' "
                        "AND consumed.relation='read_by' AND consumed.node_id='coder' "
                        "AND consumed.provider_route='route_b' LIMIT 1"
                    ).fetchone()

            self.assertEqual(result["state"], "complete")
            self.assertGreaterEqual(tracker["max_active"], 2)
            self.assertIn("plan from route_a-model", tracker["merge_prompt"])
            self.assertIn("plan from route_b-model", tracker["merge_prompt"])
            self.assertEqual(result["provider_usage"]["requests"], 5)
            self.assertIsNotNone(shared)
            prefixes = "\n".join(prefix for _model, _name, prefix, _prompt in tracker["requests"])
            self.assertIn("Planner A", prefixes)
            self.assertIn("Plan merger", prefixes)
            planner_prefixes = [prefix for _model, name, prefix, _prompt in tracker["requests"] if name == "harness_planner_wire_v3"]
            self.assertTrue(any("Planner A" in prefix and "Planner B" not in prefix for prefix in planner_prefixes))
            self.assertTrue(any("Planner B" in prefix and "Planner A" not in prefix for prefix in planner_prefixes))

    def test_same_route_max_concurrency_one_serializes_ready_planners(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.py").write_text("def fixture():\n    return True\n", encoding="utf-8")
            config = load_isolated_config(root, {
                "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
                "providers": {
                    "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            graph = cooperative_graph()
            graph["nodes"][2]["config"]["provider_route"] = "route_a"
            graph["nodes"][2]["config"]["model"] = "route_a-model"
            tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": ""}
            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: RoutedFixtureProvider(tracker)):
                with HarnessApplication(config) as app:
                    result = app.run_task("Implement the fixture", graph=graph)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(tracker["max_active"], 1)

    def test_crash_after_apply_resumes_without_repeating_coder_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "sample.txt"
            target.write_text("before\n", encoding="utf-8")
            baseline = hashlib.sha256(target.read_bytes()).hexdigest()
            config = load_isolated_config(root, {
                "provider": {"name": "ollama", "model": "fallback", "endpoint": "http://127.0.0.1:11434"},
                "providers": {
                    "route_a": {"kind": "ollama", "model": "route-a-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "ollama", "model": "route-b-model", "endpoint": "http://127.0.0.1:11434", "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            tracker = {
                "lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": "",
                "files": ["sample.txt"],
                "changes": [{
                    "path": "sample.txt", "baseline_sha256": baseline, "content": "after\n",
                    "delete": False, "reason": "fixture",
                }],
            }
            crashed = False

            def sink(event):
                nonlocal crashed
                if not crashed and event["kind"] == "mutation" and event["node"] == "coder":
                    crashed = True
                    raise KeyboardInterrupt("fixture crash after apply")

            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: RoutedFixtureProvider(tracker)):
                with HarnessApplication(config, sink=sink) as app:
                    with self.assertRaises(KeyboardInterrupt):
                        app.run_task("Change the fixture", graph=cooperative_graph())
                    checkpoint = app.memory.list_run_checkpoints()[0]
                    run_id = checkpoint.run_id
                self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
                coder_calls = sum(1 for _model, name, _prefix, _prompt in tracker["requests"] if name.startswith("harness_coder_wire_v3"))
                with HarnessApplication(config) as resumed:
                    result = resumed.resume_task(run_id)

            self.assertEqual(result["state"], "complete")
            self.assertEqual(
                sum(1 for _model, name, _prefix, _prompt in tracker["requests"] if name.startswith("harness_coder_wire_v3")),
                coder_calls,
            )

    def test_staged_coder_commits_only_after_finalize_and_does_not_replay_after_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "sample.txt"
            target.write_text("before\n", encoding="utf-8")
            baseline = hashlib.sha256(target.read_bytes()).hexdigest()
            baseline_mode = target.stat().st_mode
            config = load_isolated_config(root, {
                "provider": {"name": "local", "model": "fallback"},
                "execution": {"mode": "docker", "docker_image": "python:3.12-slim", "docker_network": "none"},
                "providers": {
                    "route_a": {"kind": "local", "model": "route-a-model", "endpoint": "http://127.0.0.1:1", "command": ["fixture"], "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "local", "model": "route-b-model", "endpoint": "http://127.0.0.1:1", "command": ["fixture"], "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            graph = cooperative_graph()
            graph["nodes"][4]["config"]["capabilities"] = ["workspace.read", "workspace.write"]
            tracker = {
                "lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": "",
                "files": ["sample.txt"], "baseline": baseline, "staged_round": 0,
                "empty_verification": True,
            }
            finalized_while_source_unchanged = False
            crashed = False

            def sink(event):
                nonlocal finalized_while_source_unchanged, crashed
                if event["kind"] == "tool_result" and event["payload"].get("name") == "stage_finalize":
                    finalized_while_source_unchanged = target.read_text(encoding="utf-8") == "before\n"
                if not crashed and event["kind"] == "mutation" and event["node"] == "coder":
                    crashed = True
                    raise KeyboardInterrupt("fixture crash after staged commit")

            def isolated_run(_runner, argv, cwd=".", timeout=None, stdin_text=None, max_output_bytes=None):
                return CommandResult(list(argv), str(cwd), 0, POSITIVE_TEST_STDOUT, "", 1)

            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: StagedFixtureProvider(tracker)), patch(
                "our_harness.execution.CommandRunner.run", autospec=True, side_effect=isolated_run,
            ):
                with HarnessApplication(config, sink=sink) as app:
                    with self.assertRaises(KeyboardInterrupt):
                        app.run_task("Change the fixture through staged tools", graph=graph)
                    run_id = app.memory.list_run_checkpoints()[0].run_id
                    retained_stage_calls = app.memory.connection.execute(
                        "SELECT COUNT(*) FROM agent_tool_journal WHERE tool_name LIKE 'stage_%'"
                    ).fetchone()[0]
                rounds_before_resume = tracker["staged_round"]
                with HarnessApplication(config) as resumed:
                    result = resumed.resume_task(run_id)
                    tool_names = [
                        event["payload"].get("name")
                        for event in resumed.memory.events(run_id)
                        if event["kind"] == "tool_result"
                    ]

            self.assertTrue(finalized_while_source_unchanged)
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(target.stat().st_mode, baseline_mode)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(retained_stage_calls, 0)
            self.assertEqual(tracker["staged_round"], rounds_before_resume)
            self.assertIn('"name": "verification-1"', tracker["staged_first_prompt"])
            self.assertIn("changes must be an empty array", tracker["staged_first_prompt"])
            self.assertNotIn("Every change must include the exact baseline", tracker["staged_first_prompt"])
            self.assertLess(tool_names.index("stage_replace_file"), tool_names.index("stage_run_verification"))
            self.assertLess(tool_names.index("stage_run_verification"), tool_names.index("stage_finalize"))

    def test_staged_coder_recovers_authenticated_candidate_after_tool_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "sample.txt"
            target.write_text("before\n", encoding="utf-8")
            baseline = hashlib.sha256(target.read_bytes()).hexdigest()
            config = load_isolated_config(root, {
                "provider": {"name": "local", "model": "fallback"},
                "execution": {"mode": "docker", "docker_image": "python:3.12-slim", "docker_network": "none"},
                "providers": {
                    "route_a": {"kind": "local", "model": "route-a-model", "endpoint": "http://127.0.0.1:1", "command": ["fixture"], "max_concurrency": 1, "allow_project_graphs": True},
                    "route_b": {"kind": "local", "model": "route-b-model", "endpoint": "http://127.0.0.1:1", "command": ["fixture"], "max_concurrency": 1, "allow_project_graphs": True},
                },
                "project": positive_test_project(),
                "workflow": {"require_review": True, "reviewers": 1},
                "memory": {"embedding_model": ""},
            })
            graph = cooperative_graph()
            graph["nodes"][4]["config"]["capabilities"] = ["workspace.read", "workspace.write"]
            tracker = {
                "lock": threading.Lock(), "active": 0, "max_active": 0, "requests": [], "merge_prompt": "",
                "files": ["sample.txt"], "baseline": baseline, "staged_round": 0,
                "empty_verification": True,
            }
            crashed = False

            def sink(event):
                nonlocal crashed
                if (
                    not crashed
                    and event["kind"] == "tool_result"
                    and event["payload"].get("name") == "stage_replace_file"
                ):
                    crashed = True
                    raise KeyboardInterrupt("fixture crash after durable stage edit")

            def isolated_run(_runner, argv, cwd=".", timeout=None, stdin_text=None, max_output_bytes=None):
                return CommandResult(list(argv), str(cwd), 0, POSITIVE_TEST_STDOUT, "", 1)

            with patch("our_harness.workflow.create_provider", side_effect=lambda _config: StagedFixtureProvider(tracker)), patch(
                "our_harness.execution.CommandRunner.run", autospec=True, side_effect=isolated_run,
            ):
                with HarnessApplication(config, sink=sink) as app:
                    with self.assertRaises(KeyboardInterrupt):
                        app.run_task("Change the fixture through staged tools", graph=graph)
                    run_id = app.memory.list_run_checkpoints()[0].run_id
                    planner_calls = sum(
                        1 for _model, name, _prefix, _prompt in tracker["requests"]
                        if name == "harness_planner_wire_v3"
                    )
                self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
                stage_checkpoints = list((root / ".harness" / "checkpoints" / "programmatic").glob("coder-*.json"))
                self.assertEqual(len(stage_checkpoints), 1)
                retained = json.loads(stage_checkpoints[0].read_text(encoding="utf-8"))
                self.assertRegex(retained["checkpoint_hmac_sha256"], r"^[0-9a-f]{64}$")

                with HarnessApplication(config) as resumed:
                    result = resumed.resume_task(run_id)
                    stage_edits = [
                        event for event in resumed.memory.events(run_id)
                        if event["kind"] == "tool_result" and event["payload"].get("name") == "stage_replace_file"
                    ]

            self.assertEqual(result["state"], "complete")
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(len(stage_edits), 1)
            self.assertEqual(
                sum(1 for _model, name, _prefix, _prompt in tracker["requests"] if name == "harness_planner_wire_v3"),
                planner_calls,
            )
            self.assertFalse(stage_checkpoints[0].exists())


if __name__ == "__main__":
    unittest.main()
