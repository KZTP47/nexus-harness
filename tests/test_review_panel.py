from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from our_harness.config import load_isolated_config
from our_harness.providers import create_provider
from our_harness.review_panel import ReviewPanel, _run_reviewer
from our_harness.runstate import canonical_json, canonical_json_sha256


PASS = {"verdict": "PASS", "findings": [], "residual_risks": []}


class ScriptedProvider:
    def __init__(self, payload: dict[str, object], delay: float = 0.0):
        self.payload = payload
        self.delay = delay
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        yield {"type": "text_delta", "text": json.dumps(self.payload)}
        yield {
            "type": "usage",
            "input_tokens": 20,
            "output_tokens": 5,
            "cached_input_tokens": 12,
            "cache_write_input_tokens": 3,
        }
        yield {"type": "done", "finish_reason": "stop"}


def pid_is_active(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


LOCAL_REVIEW_PROVIDER = r"""
import json, sys, time
request = json.load(sys.stdin)
settings = json.loads(sys.argv[1])
policy = request["system_prefix"]
lens = next(line[6:] for line in policy.splitlines() if line.startswith("Lens: "))
record = settings[lens]
expected = record.get("expected_packet")
if expected is not None:
    assert request["dynamic_context"] == ""
    assert request["messages"] == [{"role": "user", "content": expected}]
    assert "PACKET_ONLY_MARKER" not in policy
if record.get("delay"):
    time.sleep(record["delay"])
print(json.dumps({"text": json.dumps(record["payload"], ensure_ascii=False)}, ensure_ascii=False))
"""


def panel_config(
    root: Path,
    reviewers: int,
    parallelism: int,
    lenses: list[str] | None = None,
    payloads: list[dict[str, object]] | None = None,
    delays: list[float] | None = None,
    expected_packet: str | None = None,
):
    workflow: dict[str, object] = {"reviewers": reviewers, "review_parallelism": parallelism}
    if lenses is not None:
        workflow["reviewer_lenses"] = lenses
    selected_lenses = lenses or [f"independent-{index + 1}" for index in range(reviewers)]
    selected_payloads = payloads or [PASS for _ in range(reviewers)]
    selected_delays = delays or [0.0 for _ in range(reviewers)]
    settings = {
        lens: {"payload": payload, "delay": delay, "expected_packet": expected_packet}
        for lens, payload, delay in zip(selected_lenses, selected_payloads, selected_delays)
    }
    return load_isolated_config(
        root,
        {
            "workflow": workflow,
            "provider": {
                "name": "local",
                "model": "review-fixture",
                "command": [sys.executable, "-P", "-s", "-S", "-c", LOCAL_REVIEW_PROVIDER, json.dumps(settings)],
            },
        },
    )


class ReviewPanelTests(unittest.TestCase):
    def test_parallel_reviewers_reduce_wall_time_and_record_usage_latency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel = ReviewPanel(panel_config(Path(temporary), 3, 3, delays=[0.25, 0.25, 0.25]))
            started = time.monotonic()
            result = panel.review({"patch": "same"}, deadline_at=started + 2)
            parallel_elapsed = time.monotonic() - started
            self.assertTrue(result.passed)
            self.assertTrue(all(item.latency_ms >= 200 for item in result.reviews))

        with tempfile.TemporaryDirectory() as temporary:
            serial = ReviewPanel(panel_config(Path(temporary), 3, 1, delays=[0.25, 0.25, 0.25]))
            started = time.monotonic()
            self.assertTrue(serial.review({"patch": "same"}, deadline_at=started + 3).passed)
            serial_elapsed = time.monotonic() - started
        self.assertLess(parallel_elapsed, serial_elapsed * 0.75)

    def test_reviewer_core_preserves_usage_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = panel_config(Path(temporary), 1, 1)
            packet = {"patch": "same"}
            result = _run_reviewer(
                ScriptedProvider(PASS),
                config,
                0,
                "usage",
                canonical_json(packet),
                canonical_json_sha256(packet),
                time.monotonic() + 2,
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.usage["cached_input_tokens"], 12)
            self.assertEqual(result.usage["cache_write_input_tokens"], 3)

    def test_one_blocking_reviewer_blocks_the_panel_and_unions_evidence(self) -> None:
        blocker = {
            "verdict": "BLOCK",
            "findings": [{"severity": "blocker", "path": "a.py", "evidence": "wrong", "remedy": "fix"}],
            "residual_risks": ["risk-b"],
        }
        advisory = {
            "verdict": "PASS",
            "findings": [{"severity": "advisory", "path": "b.py", "evidence": "note", "remedy": "consider"}],
            "residual_risks": ["risk-a"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            panel = ReviewPanel(
                panel_config(Path(temporary), 3, 3, payloads=[PASS, blocker, advisory]),
            )
            result = panel.review({"patch": "x"}, deadline_at=time.monotonic() + 2)
            self.assertEqual(result.verdict, "BLOCK")
            self.assertEqual([finding["severity"] for finding in result.findings], ["blocker", "advisory"])
            self.assertEqual(result.residual_risks, ["risk-a", "risk-b"])

    def test_shared_deadline_cancels_unfinished_reviewers_without_losing_finished_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel = ReviewPanel(panel_config(Path(temporary), 2, 2, delays=[0.01, 2.0]))
            started = time.monotonic()
            result = panel.review({"patch": "x"}, deadline_at=started + 0.50)
            self.assertLess(time.monotonic() - started, 1.50)
            self.assertEqual(result.verdict, "BLOCK")
            self.assertIn("passed", [item.status for item in result.reviews])
            self.assertIn("cancelled", [item.status for item in result.reviews])
            self.assertFalse(panel._active_workers)

    def test_explicit_cancellation_returns_without_waiting_for_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel = ReviewPanel(panel_config(Path(temporary), 1, 1, delays=[2.0]))
            holder = {}
            thread = threading.Thread(
                target=lambda: holder.setdefault(
                    "result", panel.review({"patch": "x"}, deadline_at=time.monotonic() + 2)
                )
            )
            thread.start()
            time.sleep(0.05)
            panel.cancel()
            thread.join(timeout=1.50)
            self.assertFalse(thread.is_alive())
            self.assertEqual(holder["result"].reviews[0].status, "cancelled")
            self.assertFalse(panel._active_workers)
            self.assertFalse(any(item.name.startswith("harness-review-panel") for item in threading.enumerate()))

    def test_noncooperative_provider_cannot_hold_process_or_descendant_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "provider.pid"
            program = (
                "import json,os,pathlib,sys,time;"
                "data={'pid':os.getpid(),'ppid':os.getppid(),'pgrp':os.getpgrp() if hasattr(os,'getpgrp') else None};"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps(data),encoding='utf-8');"
                "time.sleep(30)"
            )
            script = "\n".join(
                [
                    "import sys,time",
                    "from pathlib import Path",
                    "from our_harness.config import load_isolated_config",
                    "from our_harness.review_panel import ReviewPanel",
                    "root=Path(sys.argv[1])",
                    "pid_path=root/'provider.pid'",
                    f"program={program!r}",
                    "config=load_isolated_config(root, {'workflow': {'reviewers': 1, 'review_parallelism': 1}, 'provider': {'name': 'local', 'model': 'fixture', 'command': [sys.executable, '-c', program, str(pid_path)]}})",
                    "started=time.monotonic()",
                    "result=ReviewPanel(config).review({'patch':'x'}, deadline_at=started+0.50)",
                    "assert result.reviews[0].status == 'cancelled'",
                    "assert not any(item.name.startswith('harness-review-panel') for item in __import__('threading').enumerate())",
                    "print(time.monotonic()-started)",
                ]
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(entry or os.getcwd() for entry in sys.path)
            started = time.monotonic()
            completed = subprocess.run(
                [sys.executable, "-c", script, str(root)],
                capture_output=True,
                text=True,
                env=environment,
                timeout=3.0,
                check=False,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 2.0)
            self.assertTrue(pid_path.exists(), "provider child did not start")
            provider_identity = json.loads(pid_path.read_text(encoding="utf-8"))
            provider_pid = int(provider_identity["pid"])
            if os.name != "nt":
                self.assertEqual(provider_identity["pgrp"], provider_identity["ppid"])
            self.assertFalse(pid_is_active(provider_pid), f"provider child {provider_pid} survived cancellation")

    def test_worker_ignores_project_sitecustomize_and_shadow_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site_marker = root / "sitecustomize-ran"
            shadow_marker = root / "shadow-package-ran"
            (root / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(site_marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            shadow = root / "our_harness"
            shadow.mkdir()
            (shadow / "__init__.py").write_text(
                f"from pathlib import Path\nPath({str(shadow_marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            result = ReviewPanel(panel_config(root, 1, 1)).review(
                {"patch": "x"}, deadline_at=time.monotonic() + 2
            )
            self.assertTrue(result.passed, result.findings)
            self.assertFalse(site_marker.exists())
            self.assertFalse(shadow_marker.exists())

    def test_malformed_verdict_is_isolated_and_blocks_aggregate(self) -> None:
        malformed = {"verdict": "MAYBE", "findings": [], "residual_risks": []}
        with tempfile.TemporaryDirectory() as temporary:
            panel = ReviewPanel(panel_config(Path(temporary), 2, 2, payloads=[malformed, PASS]))
            result = panel.review({"patch": "x"}, deadline_at=time.monotonic() + 2)
            self.assertEqual(result.verdict, "BLOCK")
            self.assertEqual([item.status for item in result.reviews].count("failed"), 1)
            self.assertEqual([item.status for item in result.reviews].count("passed"), 1)
            self.assertTrue(any("malformed" in finding["evidence"].lower() for finding in result.findings))

    def test_provider_factory_cannot_reuse_one_instance_between_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = panel_config(Path(temporary), 2, 2)
            shared = create_provider(config)
            panel = ReviewPanel(
                config,
                provider_factory=lambda: shared,
            )
            result = panel.review({"patch": "x"}, deadline_at=time.monotonic() + 2)
            self.assertEqual(result.verdict, "BLOCK")
            self.assertEqual([item.status for item in result.reviews].count("failed"), 2)
            self.assertTrue(any("reused" in finding["evidence"] for finding in result.findings))

    def test_project_provider_factory_fails_closed_outside_trusted_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = ReviewPanel(
                panel_config(Path(temporary), 1, 1),
                provider_factory=lambda: ScriptedProvider(PASS),
            ).review({"patch": "x"}, deadline_at=time.monotonic() + 2)
            self.assertEqual(result.verdict, "BLOCK")
            self.assertEqual(result.reviews[0].status, "failed")

    def test_each_reviewer_receives_same_exact_packet_and_only_its_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet = {"z": ["雪", "🙂"], "a": {"patch": "PACKET_ONLY_MARKER"}}
            canonical = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            config = panel_config(
                Path(temporary),
                2,
                2,
                ["correctness", "counterexample"],
                expected_packet=canonical,
            )
            result = ReviewPanel(config).review(packet, deadline_at=time.monotonic() + 2)
            self.assertTrue(result.passed, result.findings)
            self.assertEqual(len({item.packet_sha256 for item in result.reviews}), 1)
            self.assertEqual([item.lens for item in result.reviews], ["correctness", "counterexample"])

    def test_aggregation_is_deterministic_when_completion_order_changes(self) -> None:
        payloads = [
            {
                "verdict": "PASS",
                "findings": [{"severity": "advisory", "path": "z.py", "evidence": "z", "remedy": "z"}],
                "residual_risks": ["z-risk", "shared"],
            },
            {
                "verdict": "BLOCK",
                "findings": [{"severity": "blocker", "path": "a.py", "evidence": "a", "remedy": "a"}],
                "residual_risks": ["a-risk", "shared"],
            },
        ]
        snapshots = []
        for delays in ((0.10, 0.01), (0.01, 0.10)):
            with tempfile.TemporaryDirectory() as temporary:
                panel = ReviewPanel(
                    panel_config(Path(temporary), 2, 2, payloads=payloads, delays=list(delays)),
                )
                result = panel.review({"patch": "x"}, deadline_at=time.monotonic() + 2)
                snapshots.append((result.verdict, result.findings, result.residual_risks))
        self.assertEqual(snapshots[0], snapshots[1])


if __name__ == "__main__":
    unittest.main()
