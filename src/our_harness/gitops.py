from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import LoadedConfig
from .execution import CommandRunner
from .models import HarnessError


@dataclass(frozen=True)
class GitState:
    available: bool
    branch: str
    status: list[str]
    remote: str


class GitAdapter:
    def __init__(self, config: LoadedConfig, runner: CommandRunner):
        self.config = config
        self.runner = runner

    def inspect(self) -> GitState:
        if not (self.config.project_root / ".git").exists():
            return GitState(False, "", [], "")
        branch = self.runner.run(["git", "branch", "--show-current"])
        status = self.runner.run(["git", "status", "--short"])
        remote = self.runner.run(["git", "remote", "get-url", "origin"])
        return GitState(
            True,
            branch.stdout.strip() if branch.passed else "",
            status.stdout.splitlines() if status.passed else [],
            remote.stdout.strip() if remote.passed else "",
        )

    def guard_publication(self) -> GitState:
        state = self.inspect()
        if not state.available:
            raise HarnessError("This project is not a Git repository")
        protected = set(self.config.get("git.protected_branches", []))
        if state.branch in protected:
            raise HarnessError(f"Publication is blocked on protected branch: {state.branch}")
        prefix = str(self.config.get("git.required_branch_prefix", ""))
        if prefix and not state.branch.startswith(prefix):
            raise HarnessError(f"Branch must start with {prefix!r}")
        return state

    def commit(self, paths: list[str], message: str) -> None:
        if not self.config.get("git.allow_commit"):
            raise HarnessError("Git commits are disabled; set git.allow_commit to true")
        self.guard_publication()
        if not paths:
            raise HarnessError("Commit requires an explicit path list")
        self.runner.run(["git", "add", "--", *paths])
        staged = self.runner.run(["git", "diff", "--cached", "--name-only"])
        actual = set(staged.stdout.splitlines())
        if actual != set(paths):
            raise HarnessError("Staged scope differs from the explicit commit path list")
        result = self.runner.run(["git", "commit", "-m", message])
        if not result.passed:
            raise HarnessError(result.stderr or result.stdout or "git commit failed")

    def push(self) -> None:
        if not self.config.get("git.allow_push"):
            raise HarnessError("Git pushes are disabled; set git.allow_push to true")
        state = self.guard_publication()
        result = self.runner.run(["git", "push", "--set-upstream", "origin", state.branch])
        if not result.passed:
            raise HarnessError(result.stderr or result.stdout or "git push failed")
