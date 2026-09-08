"""Enforce one execution budget across every job in a GitHub workflow attempt.

Only the independent watchdog job needs actions:write. Publication uses the
read-only ``check`` command immediately before a separately time-limited step.
See docs/CI_BUDGET.md for the hosted-runner boundary and integration contract.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TARGET_SECONDS = 600
LIMIT_SECONDS = 900
CANCEL_RESERVE_SECONDS = 60
WORK_SECONDS = LIMIT_SECONDS - CANCEL_RESERVE_SECONDS
POLL_SECONDS = 10
API_SECONDS = 5
FORCE_GRACE_SECONDS = 10
MAX_JOB_PAGES = 10


class BudgetError(RuntimeError):
    """A budget or its evidence could not be verified; never a successful skip."""


def log(message: str) -> None:
    print(message, flush=True)


@dataclass(frozen=True)
class Identity:
    repository: str
    run_id: int
    attempt: int

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Identity:
        repository = env.get("GITHUB_REPOSITORY", "")
        run_id = env.get("GITHUB_RUN_ID", "")
        attempt = env.get("GITHUB_RUN_ATTEMPT", "")
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or any(part in {".", ".."} for part in repository.split("/"))
                or not run_id.isdecimal() or int(run_id) < 1
                or not attempt.isdecimal() or int(attempt) < 1):
            raise BudgetError("Missing or invalid GitHub repository/run/attempt identity.")
        return cls(repository, int(run_id), int(attempt))

    @property
    def path(self) -> str:
        # Join API route segments, not filesystem paths (including on Windows).
        return "/".join(("", "repos", self.repository, "actions", "runs", str(self.run_id)))

    def validate(self, run: dict) -> None:
        repository = run.get("repository") or {}
        if (run.get("id") != self.run_id or run.get("run_attempt") != self.attempt
                or not isinstance(repository, dict)
                or not isinstance(repository.get("full_name"), str)
                or repository["full_name"].casefold() != self.repository.casefold()):
            raise BudgetError("GitHub returned a different repository, run, or attempt; refusing cancellation.")


class GitHubAPI:
    """Stdlib-only transport with a real wall-time bound, including DNS/body reads.

    A short-lived child owns each HTTP request. Killing it on timeout also
    bounds slow DNS and trickling responses that socket timeouts alone miss.
    Tokens travel on stdin, never in command arguments or diagnostic output.
    """

    def __init__(self, env: dict[str, str]):
        self.token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
        if not self.token:
            raise BudgetError("GH_TOKEN or GITHUB_TOKEN is required; no unauthenticated budget bypass.")
        self.base = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise BudgetError("GITHUB_API_URL must be an HTTPS API origin without credentials.")

    def request(self, method: str, path: str, timeout: float = API_SECONDS) -> dict:
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "_request"],
                input=json.dumps({"method": method, "url": self.base + path, "token": self.token}),
                text=True, capture_output=True, timeout=max(0.1, min(API_SECONDS, timeout)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BudgetError(f"GitHub API request did not complete ({type(error).__name__}).") from None
        if result.returncode:
            # Neither a response body nor arbitrary subprocess output is safe
            # to echo: API errors may contain credentials or untrusted text.
            raise BudgetError("GitHub API request failed; budget cannot be verified.")
        try:
            response = json.loads(result.stdout)
            status = response["status"]
            body = response["body"]
        except (ValueError, KeyError, TypeError):
            raise BudgetError("Malformed GitHub API transport response.") from None
        expected = 200 if method == "GET" else 202
        if not isinstance(status, int):
            raise BudgetError("Malformed GitHub API HTTP status.")
        if status != expected or not isinstance(body, dict):
            raise BudgetError(f"GitHub API {method} returned HTTP {status}; expected {expected}.")
        return body


def _request_worker() -> int:
    class NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        payload = json.load(sys.stdin)
        request = urllib.request.Request(
            payload["url"], method=payload["method"],
            data=b"" if payload["method"] == "POST" else None,
            headers={"Authorization": "Bearer " + payload["token"],
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2026-03-10",
                     "User-Agent": "nexus-ci-budget"},
        )
        try:
            with urllib.request.build_opener(NoRedirects).open(request, timeout=API_SECONDS) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    return 1
                body = json.loads(raw) if raw.strip() else {}
                print(json.dumps({"status": response.status, "body": body}))
        except urllib.error.HTTPError as error:
            print(json.dumps({"status": error.code, "body": {}}))
        return 0
    except Exception:
        return 1


class WorkflowBudget:
    def __init__(self, identity: Identity, api, *, wall: Callable = time.time,
                 monotonic: Callable = time.monotonic, sleep: Callable = time.sleep,
                 log: Callable = log):
        self.identity, self.api = identity, api
        self.wall, self.monotonic, self.sleep, self.log = wall, monotonic, sleep, log
        self.started_at: float | None = None
        self.deadline: float | None = None

    def load(self) -> dict:
        run = self.api.request("GET", f"{self.identity.path}/attempts/{self.identity.attempt}")
        self.identity.validate(run)
        try:
            stamp = dt.datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timestamp has no timezone")
            self.started_at = stamp.timestamp()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BudgetError("The workflow attempt has no valid run_started_at timestamp.") from None
        elapsed = self.wall() - self.started_at
        if elapsed < -5:
            raise BudgetError("The workflow start is in the future; runner clock cannot verify the budget.")
        self.deadline = self.monotonic() + WORK_SECONDS - max(0, elapsed)
        self.log(f"Workflow {self.identity.repository}/{self.identity.run_id} attempt {self.identity.attempt}: "
                 f"elapsed={max(0, elapsed):.1f}s, target={TARGET_SECONDS}s, "
                 f"cancel_at={WORK_SECONDS}s, ceiling={LIMIT_SECONDS}s.")
        return run

    def remaining(self) -> float:
        if self.deadline is None:
            raise BudgetError("Workflow budget has not been initialized.")
        return self.deadline - self.monotonic()

    def finish(self, outcome: str) -> None:
        elapsed = WORK_SECONDS - self.remaining() if self.deadline is not None else None
        self.log(f"Workflow budget {outcome}; elapsed={elapsed:.1f}s."
                 if elapsed is not None else f"Workflow budget {outcome}; elapsed=unverified.")

    def check(self, reserve_seconds: int = 60) -> None:
        if not 0 <= reserve_seconds < WORK_SECONDS:
            raise BudgetError("Publication reserve must be between 0 and 839 seconds.")
        run = self.load()
        if run.get("status") != "in_progress" or run.get("conclusion") is not None:
            raise BudgetError("Publication requires an active, unfinished workflow attempt.")
        if self.remaining() <= reserve_seconds:
            raise BudgetError(f"Not enough workflow time remains for a {reserve_seconds}s publication step.")
        self.finish(f"publication check passed (reserved {reserve_seconds}s)")

    def jobs(self) -> list[dict]:
        jobs: list[dict] = []
        for page in range(1, MAX_JOB_PAGES + 1):
            if self.remaining() <= 0:
                raise BudgetError("The workflow execution budget expired.")
            result = self.api.request(
                "GET", f"{self.identity.path}/attempts/{self.identity.attempt}/jobs?per_page=100&page={page}",
                timeout=min(API_SECONDS, self.remaining()),
            )
            batch, count = result.get("jobs"), result.get("total_count")
            if (not isinstance(batch, list) or not isinstance(count, int) or count < 0
                    or any(not isinstance(job, dict) for job in batch)):
                raise BudgetError("Malformed workflow jobs evidence.")
            jobs.extend(batch)
            if len(jobs) >= count:
                if len(jobs) != count:
                    raise BudgetError("Workflow job pagination changed; no complete evidence snapshot.")
                return jobs
            if not batch:
                break
        raise BudgetError("Workflow jobs evidence is incomplete or exceeds the bounded page limit.")

    def cancel(self, reason: str) -> None:
        self.log(f"::error::Workflow budget failed: {reason}")
        try:
            current = self.api.request("GET", self.identity.path)
            self.identity.validate(current)
            if current.get("status") == "completed":
                return
            try:
                self.api.request("POST", f"{self.identity.path}/cancel")
                self.log("Cancellation requested for the entire workflow; waiting at most 10s before force cancellation.")
            except BudgetError:
                self.log("::error::Normal cancellation was not confirmed; checking once before force cancellation.")
            self.sleep(FORCE_GRACE_SECONDS)
            current = self.api.request("GET", self.identity.path)
            self.identity.validate(current)
            if current.get("status") != "completed":
                self.api.request("POST", f"{self.identity.path}/force-cancel")
                self.log("Force cancellation requested for the entire workflow.")
        except BudgetError as error:
            self.log(f"::error::Could not confirm whole-workflow cancellation: {error}")
        finally:
            self.finish("FAILED")

    def watch(self, required: list[str], allow_skipped: list[str] = (),
              watchdog_name: str = "Workflow deadline") -> bool:
        if (not required or len(required) != len(set(required)) or any(not name.strip() for name in required)
                or watchdog_name in required or not set(allow_skipped) <= set(required)):
            raise BudgetError("Require distinct, nonempty job names; skipped exceptions must name required jobs.")
        try:
            run = self.load()
            if run.get("status") != "in_progress" or run.get("conclusion") is not None:
                raise BudgetError("The workflow attempt is not active; this watchdog cannot certify it.")
            while True:
                if self.remaining() <= 0:
                    raise BudgetError("The workflow execution budget expired.")
                rows = self.jobs()
                found: dict[str, dict] = {}
                for row in rows:
                    if row.get("run_id") != self.identity.run_id:
                        raise BudgetError("Job evidence belongs to another workflow run.")
                    name = row.get("name")
                    if not isinstance(name, str) or name in found:
                        raise BudgetError("Workflow job names are missing or ambiguous.")
                    found[name] = row
                unknown = set(found) - set(required) - {watchdog_name}
                if unknown:
                    raise BudgetError("Workflow contains jobs absent from the required budget manifest.")
                for name in required:
                    row = found.get(name, {})
                    if row.get("status") == "completed":
                        allowed = {"success", "skipped"} if name in allow_skipped else {"success"}
                        if row.get("conclusion") not in allowed:
                            raise BudgetError(f"Required job {name!r} completed with {row.get('conclusion')!r}.")
                if self.remaining() <= 0:
                    raise BudgetError("The workflow execution budget expired before verification completed.")
                if all(found.get(name, {}).get("status") == "completed" for name in required):
                    self.finish("PASSED")
                    return True
                self.sleep(min(POLL_SECONDS, self.remaining()))
        except BudgetError as error:
            self.cancel(str(error))
            return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch", help="Watch the full expected job manifest and cancel overdue work")
    watch.add_argument("--required-job", action="append", required=True)
    watch.add_argument("--allow-skipped-job", action="append", default=[])
    watch.add_argument("--watchdog-name", default="Workflow deadline")
    check = commands.add_parser("check", help="Verify remaining time before a bounded publication step")
    check.add_argument("--reserve-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    budget = None
    try:
        budget = WorkflowBudget(Identity.from_env(os.environ), GitHubAPI(os.environ))
        if args.command == "watch":
            return 0 if budget.watch(args.required_job, args.allow_skipped_job, args.watchdog_name) else 1
        budget.check(args.reserve_seconds)
        return 0
    except BudgetError as error:
        log(f"::error::{error}")
        if budget is not None:
            budget.finish("FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(_request_worker() if sys.argv[1:] == ["_request"] else main())
