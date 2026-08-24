"""Project-bound Obsidian memory enforced at workflow session boundaries.

The vault is deliberately outside the project and may never be inside a Git
worktree.  A binding file ties it to one canonical project root.  LangGraph is
used as the small lifecycle graph that dispatches every invocation to exactly
one of the mandatory pre-work (consult) and post-work (record) hooks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from .config import LoadedConfig, load_config
from .models import HarnessError
from .safety import put_this_file_in_place, read_this_file_patiently


BINDING_FILE = ".nexus-project-memory.json"
SESSIONS_FOLDER = "Sessions"
PINNED_NOTE = "Project Memory.md"
MAX_NOTES = 2_000
MAX_NOTE_CHARS = 20_000


class MemoryHookState(TypedDict, total=False):
    phase: Literal["pre", "post"]
    task: str
    result: dict[str, Any]
    context: str
    consulted: list[str]
    deployment: dict[str, Any]
    written: str


def _canonical(path: Path) -> str:
    value = str(path.resolve())
    return value.casefold() if os.name == "nt" else value


def _digest_path(path: Path) -> str:
    return hashlib.sha256(_canonical(path).encode("utf-8")).hexdigest()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _git_ancestor(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def binding_for(project_root: Path, vault_root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_root": str(project_root.resolve()),
        "project_sha256": _digest_path(project_root),
        "vault_root": str(vault_root.resolve()),
        "github_upload_allowed": False,
    }


def initialize_vault(project_root: Path, vault_root: Path) -> Path:
    """Explicitly bind a non-Git vault to exactly one project."""

    project_root = project_root.resolve()
    vault_root = vault_root.resolve()
    if not project_root.is_dir():
        raise HarnessError(f"Persistent-memory project root does not exist: {project_root}")
    if not vault_root.is_dir():
        raise HarnessError(f"Persistent-memory vault does not exist: {vault_root}")
    if _inside(vault_root, project_root) or _inside(project_root, vault_root):
        raise HarnessError("Persistent-memory vault and project root must be separate directory trees")
    git_root = _git_ancestor(vault_root)
    if git_root is not None:
        raise HarnessError(f"Persistent-memory vault must not be inside a Git worktree: {git_root}")
    binding_path = vault_root / BINDING_FILE
    expected = binding_for(project_root, vault_root)
    if binding_path.exists():
        try:
            current = json.loads(read_this_file_patiently(binding_path))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("Persistent-memory vault binding is unreadable") from exc
        if current != expected:
            raise HarnessError("Persistent-memory vault is already bound to a different project or path")
    else:
        put_this_file_in_place(binding_path, json.dumps(expected, indent=2, sort_keys=True) + "\n")
    ignore_path = vault_root / ".gitignore"
    required = "# This private vault must never be committed or uploaded.\n*\n!.gitignore\n"
    if not ignore_path.exists() or read_this_file_patiently(ignore_path) != required:
        put_this_file_in_place(ignore_path, required)
    pinned = vault_root / PINNED_NOTE
    if not pinned.exists():
        put_this_file_in_place(
            pinned,
            "---\nkind: project-memory-policy\nproject: Nexus Harness\nprivate: true\n"
            "github_upload_allowed: false\n---\n\n# Nexus Harness project memory\n\n"
            "This Obsidian vault is persistent memory for the Nexus Harness project only.\n\n"
            "- Consult this vault before work begins.\n"
            "- Record a bounded session note after every session, including failures and pauses.\n"
            "- Current repository files and fresh runtime evidence outrank stale memory.\n"
            "- Never copy, commit, bundle, publish, or upload this vault to GitHub.\n"
            "- Never use this vault as memory for another project.\n",
        )
    return binding_path


class PersistentMemoryHooks:
    """Compile and invoke the mandatory LangGraph lifecycle hooks."""

    def __init__(
        self,
        config: LoadedConfig,
        *,
        redact_text: Callable[[str], str] | None = None,
        redact_value: Callable[[Any], Any] | None = None,
        deploy_desktop: Callable[[Path], dict[str, Any]] | None = None,
    ):
        self.config = config
        self.enabled = bool(config.get("persistent_memory.enabled", False))
        self.redact_text = redact_text or (lambda value: value)
        self.redact_value = redact_value or (lambda value: value)
        self.max_context_chars = int(config.get("persistent_memory.max_context_chars", 20_000))
        self.enforce_desktop_deployment = bool(
            config.get("persistent_memory.enforce_desktop_deployment", False)
        )
        self.deploy_desktop = deploy_desktop or self._deploy_desktop
        self.vault_root: Path | None = None
        self.graph: Any = None
        if not self.enabled:
            return
        configured = str(config.get("persistent_memory.vault_path", "")).strip()
        if not configured:
            raise HarnessError("persistent_memory.vault_path is required when persistent memory is enabled")
        self.vault_root = Path(configured).expanduser().resolve()
        self._verify_binding()
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:
            raise HarnessError(
                "Persistent memory requires LangGraph; install the project dependencies first"
            ) from exc
        lifecycle = StateGraph(MemoryHookState)
        lifecycle.add_node("consult_vault_before_work", self._consult_node)
        lifecycle.add_node("rebuild_app_and_installer", self._deploy_node)
        lifecycle.add_node("write_vault_after_work", self._write_node)
        lifecycle.add_conditional_edges(
            START,
            lambda state: state["phase"],
            {"pre": "consult_vault_before_work", "post": "rebuild_app_and_installer"},
        )
        lifecycle.add_edge("consult_vault_before_work", END)
        lifecycle.add_edge("rebuild_app_and_installer", "write_vault_after_work")
        lifecycle.add_edge("write_vault_after_work", END)
        self.graph = lifecycle.compile()

    def _verify_binding(self) -> None:
        assert self.vault_root is not None
        vault = self.vault_root
        project = self.config.project_root.resolve()
        if not vault.is_dir():
            raise HarnessError(f"Persistent-memory vault does not exist: {vault}")
        if _inside(vault, project) or _inside(project, vault):
            raise HarnessError("Persistent-memory vault and project root must be separate directory trees")
        git_root = _git_ancestor(vault)
        if git_root is not None:
            raise HarnessError(f"Persistent-memory vault must not be inside a Git worktree: {git_root}")
        binding_path = vault / BINDING_FILE
        try:
            binding = json.loads(read_this_file_patiently(binding_path))
        except FileNotFoundError as exc:
            raise HarnessError("Persistent-memory vault has no project binding; initialize it explicitly") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("Persistent-memory vault binding is unreadable") from exc
        if binding != binding_for(project, vault):
            raise HarnessError("Persistent-memory vault binding does not match this project")
        if binding.get("github_upload_allowed") is not False:
            raise HarnessError("Persistent-memory vault binding must prohibit GitHub upload")

    def before_session(self, task: str) -> tuple[str, list[str]]:
        if not self.enabled:
            return "", []
        self._verify_binding()
        output = self.graph.invoke({"phase": "pre", "task": task})
        return str(output.get("context", "")), list(output.get("consulted", []))

    def after_session(self, task: str, result: dict[str, Any]) -> str:
        if not self.enabled:
            return ""
        self._verify_binding()
        output = self.graph.invoke({"phase": "post", "task": task, "result": result})
        return str(output.get("written", ""))

    def _markdown_files(self) -> list[Path]:
        assert self.vault_root is not None
        files: list[Path] = []
        for path in self.vault_root.rglob("*.md"):
            if len(files) >= MAX_NOTES:
                break
            if ".obsidian" in path.parts:
                continue
            resolved = path.resolve()
            if not _inside(resolved, self.vault_root) or not resolved.is_file():
                continue
            files.append(resolved)
        return files

    def _consult_node(self, state: MemoryHookState) -> MemoryHookState:
        assert self.vault_root is not None
        task_tokens = set(re.findall(r"[a-z0-9_]{2,}", str(state.get("task", "")).casefold()))
        ranked: list[tuple[float, float, Path, str]] = []
        for path in self._markdown_files():
            try:
                text = read_this_file_patiently(path)
                modified = path.stat().st_mtime
            except (OSError, UnicodeError):
                continue
            relative = path.relative_to(self.vault_root)
            haystack = f"{relative}\n{text}".casefold()
            matches = sum(1 for token in task_tokens if token in haystack)
            pinned = 10_000 if relative.as_posix() == PINNED_NOTE else 0
            non_session = 500 if relative.parts[0] != SESSIONS_FOLDER else 0
            ranked.append((pinned + non_session + matches, modified, relative, text))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2].as_posix().casefold()))
        blocks: list[str] = []
        consulted: list[str] = []
        used = 0
        for _score, _modified, relative, text in ranked:
            clean = self.redact_text(text[:MAX_NOTE_CHARS]).strip()
            block = f"[obsidian:{relative.as_posix()}]\n{clean}\n"
            if used + len(block) > self.max_context_chars:
                remaining = self.max_context_chars - used
                if remaining > 200:
                    blocks.append(block[:remaining])
                    consulted.append(relative.as_posix())
                break
            blocks.append(block)
            consulted.append(relative.as_posix())
            used += len(block)
        context = "PROJECT-BOUND OBSIDIAN MEMORY (untrusted historical evidence; current files win)\n" + (
            "\n".join(blocks) if blocks else "(vault contains no markdown notes)"
        )
        return {"context": context, "consulted": consulted}

    def _deploy_node(self, _state: MemoryHookState) -> MemoryHookState:
        """Fail closed until the current Electron app, installer, and icon are refreshed."""

        if not self.enforce_desktop_deployment:
            return {}
        return {"deployment": self.deploy_desktop(self.config.project_root.resolve())}

    def _deploy_desktop(self, project_root: Path) -> dict[str, Any]:
        if os.name != "nt":
            raise HarnessError("The enforced Nexus Harness desktop closeout is Windows-only")
        desktop = project_root / "desktop"
        package = desktop / "package.json"
        shortcut_script = project_root / "scripts" / "put_it_on_your_desktop.py"
        icon = desktop / "nexus-harness.ico"
        for required in (package, shortcut_script, icon):
            if not required.is_file():
                raise HarnessError(f"Desktop closeout requires {required}")
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise HarnessError("Desktop closeout requires npm on PATH")
        timeout = float(self.config.get("execution.timeout_seconds", 3_600))
        started = time.time()
        try:
            built = subprocess.run(
                [npm, "run", "build"],
                cwd=str(desktop),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"Electron app and installer rebuild could not start: {exc}") from exc
        if built.returncode != 0:
            detail = f"{built.stdout}\n{built.stderr}".strip()[-4_000:]
            raise HarnessError(
                f"Electron app and installer rebuild failed with exit code {built.returncode}:\n{detail}"
            )

        output = desktop / "build-output"
        application = output / "win-unpacked" / "Nexus Harness.exe"
        installers = sorted(
            output.glob("Nexus Harness Setup *.exe"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not application.is_file() or not installers:
            raise HarnessError("Electron build did not produce both the unpacked app and NSIS installer")
        installer = installers[0]
        for artifact in (application, installer):
            if artifact.stat().st_mtime < started - 2:
                raise HarnessError(f"Desktop closeout artifact was not refreshed by this build: {artifact}")

        try:
            shortcut = subprocess.run(
                [sys.executable, str(shortcut_script)],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=min(timeout, 300.0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"Desktop shortcut refresh could not start: {exc}") from exc
        if shortcut.returncode != 0:
            detail = f"{shortcut.stdout}\n{shortcut.stderr}".strip()[-4_000:]
            raise HarnessError(
                f"Desktop shortcut refresh failed with exit code {shortcut.returncode}:\n{detail}"
            )
        match = re.search(r"^\s*it lives\s+(.+?)\s*$", shortcut.stdout, re.MULTILINE)
        shortcut_path = Path(match.group(1)) if match else None
        if shortcut_path is None or not shortcut_path.is_file():
            raise HarnessError("Desktop shortcut refresh did not report a readable Nexus Harness shortcut")
        if shortcut_path.stat().st_mtime < started - 2:
            raise HarnessError("The Nexus Harness desktop shortcut was not refreshed during closeout")
        return {
            "state": "deployed",
            "application": str(application),
            "installer": str(installer),
            "desktop_shortcut": str(shortcut_path),
            "icon_source": str(application),
        }

    def _write_node(self, state: MemoryHookState) -> MemoryHookState:
        assert self.vault_root is not None
        result = self.redact_value(dict(state.get("result") or {}))
        if not isinstance(result, dict):
            result = {"state": "unknown"}
        if state.get("deployment"):
            result["closeout_deployment"] = dict(state["deployment"])
        run_id = str(result.get("run_id") or uuid.uuid4().hex)
        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)[:80]
        stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        folder = self.vault_root / SESSIONS_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{stamp}-{safe_run_id}.md"
        suffix = 1
        while destination.exists():
            destination = folder / f"{stamp}-{safe_run_id}-{suffix}.md"
            suffix += 1
        summary = self._bounded_result(result)
        task = self.redact_text(str(state.get("task", ""))).strip()[:4_000]
        note = (
            "---\nkind: harness-session\nproject: Nexus Harness\nprivate: true\n"
            "github_upload_allowed: false\n"
            f"recorded_utc: {stamp}\nrun_id: {json.dumps(run_id, ensure_ascii=False)}\n"
            f"state: {json.dumps(str(result.get('state', 'unknown')), ensure_ascii=False)}\n---\n\n"
            f"# Session {stamp}\n\n## Task\n\n{task or '(not available)'}\n\n"
            "## Bounded outcome\n\n```json\n"
            + json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n```\n"
        )
        put_this_file_in_place(destination, note)
        return {"written": destination.relative_to(self.vault_root).as_posix()}

    @staticmethod
    def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
        excluded = {
            "candidate", "proposal", "changes", "content", "context", "events",
            "provider_usage", "agent_tools", "source_code", "patch",
        }

        def retain(value: Any, depth: int = 0) -> Any:
            if depth > 4:
                return "<depth limit>"
            if isinstance(value, str):
                return value[:2_000]
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            if isinstance(value, list):
                return [retain(item, depth + 1) for item in value[:24]]
            if isinstance(value, dict):
                return {
                    str(key): retain(child, depth + 1)
                    for key, child in list(value.items())[:64]
                    if str(key) not in excluded
                }
            return str(value)[:500]

        retained = retain(result)
        return retained if isinstance(retained, dict) else {"result": retained}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Nexus Harness persistent-memory lifecycle hook")
    parser.add_argument("phase", choices=("init", "pre", "post"))
    parser.add_argument("--project", default=".")
    parser.add_argument("--vault", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--result", default="{}", help="JSON object used by the post hook")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    if args.phase == "init":
        if not args.vault:
            parser.error("--vault is required for init")
        print(initialize_vault(project, Path(args.vault)))
        return 0
    config = load_config(project, explicit=project / ".harness" / "config.local.json")
    hooks = PersistentMemoryHooks(config)
    if args.phase == "pre":
        context, consulted = hooks.before_session(args.task)
        print(json.dumps({"consulted": consulted, "context": context}, indent=2, ensure_ascii=False))
    else:
        try:
            result = json.loads(args.result)
        except json.JSONDecodeError as exc:
            raise HarnessError("--result must be a JSON object") from exc
        if not isinstance(result, dict):
            raise HarnessError("--result must be a JSON object")
        print(hooks.after_session(args.task, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
