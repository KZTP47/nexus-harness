"""The workflow clock spans serial jobs and cannot turn missing evidence green."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import subprocess
import unittest
from unittest import mock

from scripts import ci_budget as ci


IDENTITY = ci.Identity("another-owner/portable-project", 1234, 2)
START = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()


def run_data(**changes):
    result = {"id": 1234, "run_attempt": 2,
              "repository": {"full_name": IDENTITY.repository},
              "run_started_at": "2026-01-01T00:00:00Z",
              "status": "in_progress", "conclusion": None}
    result.update(changes)
    return result


def job(name="Build", status="completed", conclusion="success", **changes):
    result = {"run_id": 1234, "name": name, "status": status, "conclusion": conclusion}
    result.update(changes)
    return result


class Clock:
    def __init__(self, elapsed=0):
        self.elapsed = elapsed
        self.wall_offset = 0
        self.sleeps = []

    def wall(self):
        return START + self.elapsed + self.wall_offset

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class FakeAPI:
    def __init__(self, snapshots=None):
        self.attempt_run = run_data()
        self.current_runs = [run_data()]
        self.snapshots = list(snapshots if snapshots is not None else [[job()]])
        self.requests = []
        self.errors = {}
        self.callback = None

    def request(self, method, path, timeout=ci.API_SECONDS):
        self.requests.append((method, path, timeout))
        if self.callback:
            self.callback(method, path)
        for ending, error in self.errors.items():
            if path.endswith(ending):
                raise error
        if method == "POST":
            return {}
        if "/jobs?" in path:
            snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            return snapshot if isinstance(snapshot, dict) else {"jobs": snapshot, "total_count": len(snapshot)}
        if "/attempts/" in path:
            return self.attempt_run
        return self.current_runs.pop(0) if len(self.current_runs) > 1 else self.current_runs[0]

    def posts(self):
        return [path for method, path, _ in self.requests if method == "POST"]


class WorkflowBudgetTests(unittest.TestCase):
    def setup_budget(self, elapsed=0, snapshots=None):
        self.clock = Clock(elapsed)
        self.api = FakeAPI(snapshots)
        self.messages = []
        self.budget = ci.WorkflowBudget(IDENTITY, self.api, wall=self.clock.wall,
                                        monotonic=self.clock.monotonic,
                                        sleep=self.clock.sleep, log=self.messages.append)
        return self.budget

    def test_completed_jobs_exit_immediately_instead_of_holding_the_workflow_open(self):
        budget = self.setup_budget(120, [[job(), job("Workflow deadline", "in_progress", None)]])
        self.assertTrue(budget.watch(["Build"]))
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.api.posts(), [])
        self.assertIn("elapsed=120.0s", self.messages[-1])

    def test_serial_jobs_not_yet_materialized_cannot_be_treated_as_complete(self):
        budget = self.setup_budget(snapshots=[[job()], [job(), job("Publish")]])
        self.assertTrue(budget.watch(["Build", "Publish"]))
        self.assertEqual(self.clock.sleeps, [ci.POLL_SECONDS])

    def test_clock_starts_at_workflow_attempt_start_not_watchdog_start(self):
        budget = self.setup_budget(839, [[job(status="in_progress", conclusion=None)]])
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(self.clock.sleeps, [1, ci.FORCE_GRACE_SECONDS])
        self.assertEqual(self.api.posts(), [IDENTITY.path + "/cancel", IDENTITY.path + "/force-cancel"])
        self.assertLess(self.clock.elapsed, ci.LIMIT_SECONDS)

    def test_already_expired_workflow_never_returns_success_even_when_jobs_are_green(self):
        budget = self.setup_budget(ci.WORK_SECONDS)
        self.assertFalse(budget.watch(["Build"]))
        self.assertTrue(self.api.posts())
        self.assertFalse(any("/jobs?" in path for _, path, _ in self.api.requests))

    def test_failed_cancelled_neutral_and_silently_skipped_jobs_fail(self):
        for conclusion in ("failure", "cancelled", "neutral", "skipped", "timed_out", None):
            with self.subTest(conclusion=conclusion):
                budget = self.setup_budget(snapshots=[[job(conclusion=conclusion)]])
                self.assertFalse(budget.watch(["Build"]))
                self.assertEqual(len(self.api.posts()), 2)
                self.assertTrue(self.messages[-1].startswith("Workflow budget FAILED"))

    def test_only_explicitly_named_intentional_skip_is_accepted(self):
        budget = self.setup_budget(snapshots=[[job(), job("Publish", conclusion="skipped")]])
        self.assertTrue(budget.watch(["Build", "Publish"], ["Publish"]))

    def test_manifest_rejects_empty_duplicate_and_unrelated_skip_exceptions(self):
        for required, skipped in (([], []), (["Build", "Build"], []), ([""], []),
                                  (["Workflow deadline"], []), (["Build"], ["Other"])):
            with self.subTest(required=required, skipped=skipped):
                budget = self.setup_budget()
                with self.assertRaises(ci.BudgetError):
                    budget.watch(required, skipped)
                self.assertEqual(self.api.requests, [])

    def test_unexpected_jobs_and_ambiguous_names_cannot_escape_the_manifest(self):
        for rows in ([job(), job("New lane")], [job(), job()], [job(name=None)]):
            with self.subTest(rows=rows):
                budget = self.setup_budget(snapshots=[rows])
                self.assertFalse(budget.watch(["Build"]))

    def test_missing_jobs_are_not_success_and_expire_at_the_shared_deadline(self):
        budget = self.setup_budget(830, [[]])
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(self.clock.sleeps, [10, 10])

    def test_job_failure_stops_every_job_through_the_run_endpoint(self):
        budget = self.setup_budget(30, [[job(conclusion="failure"), job("Peer", "in_progress", None)]])
        self.assertFalse(budget.watch(["Build", "Peer"]))
        self.assertEqual(self.api.posts(), [IDENTITY.path + "/cancel", IDENTITY.path + "/force-cancel"])
        self.assertEqual(self.clock.elapsed, 40)

    def test_normal_cancellation_that_completes_needs_no_force_cancel(self):
        budget = self.setup_budget(ci.WORK_SECONDS)
        self.api.current_runs = [run_data(), run_data(status="completed", conclusion="cancelled")]
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(self.api.posts(), [IDENTITY.path + "/cancel"])

    def test_uncertain_normal_cancel_receipt_still_attempts_force_cancel(self):
        budget = self.setup_budget(ci.WORK_SECONDS)
        self.api.errors["/cancel"] = ci.BudgetError("transport timeout")
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(self.api.posts(), [IDENTITY.path + "/cancel", IDENTITY.path + "/force-cancel"])

    def test_rerun_during_cancellation_is_never_force_cancelled_by_an_old_watchdog(self):
        budget = self.setup_budget(ci.WORK_SECONDS)
        self.api.current_runs = [run_data(), run_data(run_attempt=3)]
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(self.api.posts(), [IDENTITY.path + "/cancel"])
        self.assertTrue(any("different repository, run, or attempt" in item for item in self.messages))

    def test_wrong_run_attempt_repository_or_job_identity_never_certifies_success(self):
        for changes in ({"id": 99}, {"run_attempt": 1}, {"repository": {"full_name": "other/repo"}},
                        {"repository": "malformed"}):
            with self.subTest(changes=changes):
                budget = self.setup_budget()
                self.api.attempt_run = self.api.current_runs[0] = run_data(**changes)
                self.assertFalse(budget.watch(["Build"]))
                self.assertEqual(self.api.posts(), [])
        budget = self.setup_budget(snapshots=[[job(run_id=99)]])
        self.assertFalse(budget.watch(["Build"]))

    def test_jobs_api_failure_fails_closed_and_cancels_with_bounded_requests(self):
        budget = self.setup_budget()
        self.api.errors["/jobs?per_page=100&page=1"] = ci.BudgetError("HTTP 503")
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(len(self.api.posts()), 2)
        self.assertLessEqual(len(self.api.requests), 6)
        self.assertTrue(all(timeout <= ci.API_SECONDS for _, _, timeout in self.api.requests))

    def test_authentication_or_api_outage_never_becomes_an_unverified_success(self):
        budget = self.setup_budget()
        self.api.errors["/attempts/2"] = ci.BudgetError("HTTP 403")
        self.api.errors[IDENTITY.path] = ci.BudgetError("HTTP 403")
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(len(self.api.requests), 2)
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("elapsed=unverified", self.messages[-1])

    def test_pagination_reads_all_jobs_before_success(self):
        budget = self.setup_budget(snapshots=[{"total_count": 2, "jobs": [job()]},
                                             {"total_count": 2, "jobs": [job("Publish")]}])
        self.assertTrue(budget.watch(["Build", "Publish"]))
        self.assertTrue(any("page=2" in path for _, path, _ in self.api.requests))

    def test_incomplete_or_malformed_pagination_is_bounded_failure(self):
        for response in ({"total_count": 2, "jobs": []}, {"jobs": [job()]},
                         {"total_count": 1, "jobs": ["bad"]}):
            with self.subTest(response=response):
                budget = self.setup_budget(snapshots=[response])
                self.assertFalse(budget.watch(["Build"]))
                self.assertLess(len(self.api.requests), 10)

    def test_excessive_pagination_cannot_keep_the_watchdog_fetching_forever(self):
        budget = self.setup_budget(snapshots=[{"total_count": 99999, "jobs": [job()]}])
        self.assertFalse(budget.watch(["Build"]))
        requests = [path for _, path, _ in self.api.requests if "/jobs?" in path]
        self.assertEqual(len(requests), ci.MAX_JOB_PAGES)

    def test_network_response_crossing_deadline_cannot_produce_a_late_pass(self):
        budget = self.setup_budget(839)
        self.api.callback = lambda method, path: self.clock.sleep(2) if "/jobs?" in path else None
        self.assertFalse(budget.watch(["Build"]))

    def test_wall_clock_rollback_after_anchor_cannot_extend_execution_budget(self):
        budget = self.setup_budget(830, [[job(status="in_progress", conclusion=None)]])
        self.api.callback = lambda method, path: setattr(self.clock, "wall_offset", -300) if "/jobs?" in path else None
        self.assertFalse(budget.watch(["Build"]))
        self.assertEqual(self.clock.elapsed, 850)

    def test_invalid_naive_or_future_start_timestamp_fails(self):
        for timestamp in (None, "bad", "2026-01-01T00:00:00", "2026-01-01T00:01:00Z"):
            with self.subTest(timestamp=timestamp):
                budget = self.setup_budget()
                self.api.attempt_run["run_started_at"] = timestamp
                self.assertFalse(budget.watch(["Build"]))

    def test_publication_requires_room_for_the_entire_step_before_cancellation(self):
        budget = self.setup_budget(779)
        budget.check(60)
        self.assertEqual(self.api.posts(), [])
        budget = self.setup_budget(780)
        with self.assertRaises(ci.BudgetError):
            budget.check(60)
        self.assertEqual(self.api.posts(), [])

    def test_publication_rejects_completed_cancelled_and_uninitialized_evidence(self):
        for changes in ({"status": "completed", "conclusion": "success"},
                        {"status": "queued"}, {"conclusion": "cancelled"}):
            with self.subTest(changes=changes):
                budget = self.setup_budget()
                self.api.attempt_run.update(changes)
                with self.assertRaises(ci.BudgetError):
                    budget.check(60)


class TransportAndInterfaceTests(unittest.TestCase):
    def test_identity_requires_portable_explicit_run_coordinates(self):
        identity = ci.Identity.from_env({"GITHUB_REPOSITORY": "a-team/a.repo",
                                         "GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "3"})
        self.assertEqual(identity.path, "/repos/a-team/a.repo/actions/runs/5")
        for env in ({}, {"GITHUB_REPOSITORY": "../evil", "GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "3"}):
            with self.assertRaises(ci.BudgetError):
                ci.Identity.from_env(env)

    def test_api_requires_credentials_without_logging_them(self):
        with self.assertRaises(ci.BudgetError):
            ci.GitHubAPI({})
        token = "test-secret-must-not-be-logged"
        api = ci.GitHubAPI({"GH_TOKEN": token})
        with mock.patch.object(ci.subprocess, "run", side_effect=subprocess.TimeoutExpired(["worker"], 1)) as request:
            with self.assertRaises(ci.BudgetError) as caught:
                api.request("GET", "/some/path", timeout=1)
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(token, " ".join(request.call_args.args[0]))
        self.assertEqual(request.call_args.kwargs["timeout"], 1)
        self.assertTrue(request.call_args.kwargs["capture_output"])

    def test_http_errors_malformed_bodies_and_secret_output_fail_closed(self):
        token = "unprintable-test-secret"
        api = ci.GitHubAPI({"GH_TOKEN": token})
        for body in ("bad-json", json.dumps({"status": 403, "body": {"message": token}}),
                     json.dumps({"status": 200, "body": []}),
                     json.dumps({"status": {"secret": token}, "body": {}})):
            with self.subTest(body=body), mock.patch.object(ci.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, body, token)):
                with self.assertRaises(ci.BudgetError) as caught:
                    api.request("GET", "/some/path")
                self.assertNotIn(token, str(caught.exception))

    def test_worker_uses_authentication_and_does_not_follow_redirects(self):
        request_body = json.dumps({"url": "https://api.github.com/repos/example/repo/actions/runs/5",
                                   "method": "GET", "token": "only-on-stdin"})
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"id": 5}'
        output = io.StringIO()
        with mock.patch.object(ci.sys, "stdin", io.StringIO(request_body)), \
                contextlib.redirect_stdout(output), \
                mock.patch.object(ci.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(ci._request_worker(), 0)
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer only-on-stdin")
        handler_type = build.call_args.args[0]
        self.assertIsNone(handler_type().redirect_request(None, None, 302, "", {}, "https://other.invalid"))
        self.assertEqual(json.loads(output.getvalue()), {"status": 200, "body": {"id": 5}})
        self.assertNotIn("only-on-stdin", output.getvalue())

    def test_successful_transport_preserves_read_only_and_cancel_responses(self):
        api = ci.GitHubAPI({"GH_TOKEN": "test-only"})
        for method, status, body in (("GET", 200, {"id": 5}), ("POST", 202, {})):
            with self.subTest(method=method), mock.patch.object(ci.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps({"status": status, "body": body}), "")):
                self.assertEqual(api.request(method, "/some/path"), body)

    def test_publication_failure_cli_reports_elapsed_time(self):
        clock = Clock(800)
        budget = ci.WorkflowBudget(IDENTITY, FakeAPI(), wall=clock.wall,
                                   monotonic=clock.monotonic, sleep=clock.sleep)
        output = io.StringIO()
        with mock.patch.object(ci.Identity, "from_env", return_value=IDENTITY), \
                mock.patch.object(ci, "GitHubAPI"), \
                mock.patch.object(ci, "WorkflowBudget", return_value=budget), \
                contextlib.redirect_stdout(output):
            self.assertEqual(ci.main(["check", "--reserve-seconds", "60"]), 1)
        self.assertIn("FAILED; elapsed=800.0s", output.getvalue())

    def test_cli_fails_without_platform_identity_instead_of_silently_disabling_guard(self):
        with mock.patch.dict(ci.os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ci.main(["watch", "--required-job", "Build"]), 1)

    def test_policy_constants_reserve_stop_time_within_fifteen_minutes(self):
        self.assertEqual(ci.LIMIT_SECONDS, 15 * 60)
        self.assertEqual(ci.TARGET_SECONDS, 10 * 60)
        self.assertEqual(ci.WORK_SECONDS, 14 * 60)
        self.assertLessEqual(4 * ci.API_SECONDS + ci.FORCE_GRACE_SECONDS, ci.CANCEL_RESERVE_SECONDS)


if __name__ == "__main__":
    unittest.main()
