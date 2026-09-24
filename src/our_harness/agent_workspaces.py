"""Persistent per-agent candidates; only the goal publisher owns the real project.

Copies are independent files, not OS security sandboxes. Provider permission
enforcement belongs to the native execution backend. State lives outside the
mutable copy and uses the same authenticated storage as goal workspaces.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager

from . import cancellation
from . import goal_workspaces as gw
from .changes import atomic_write
from .models import HarnessError
from .filesystem_paths import filesystem_path
from .safety import ProjectTransactionLock, confined_path, portable_relative_path_key

CONTRACT = "nexus-agent-workspace/v1"
_GENERATED = {".git", ".harness", ".nexus-verification", "node_modules", ".venv", "venv", "__pycache__",
              # Caches that test, lint and build tools write on their own while an
              # agent works. They are not the agent's changes, and collecting them
              # used up the publication budget and published tool litter. Output
              # folders such as dist, build and target can be real deliverables,
              # so they are deliberately not listed here; neither is the
              # ambiguous .cache, which is often a real (tracked) fixture folder.
              ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox",
              ".coverage", ".ipynb_checkpoints", ".eslintcache", ".parcel-cache", ".turbo",
              ".gradle"}


def _is_generated(relative: str) -> bool:
    # One exclusion rule shared with goal copies and closeout snapshots.
    return any(part.casefold() in _GENERATED for part in str(relative).split("/")) \
        or gw.excluded_relative(relative)


def _without_generated(files: dict) -> dict:
    """Drop entries under folders the inventory no longer collects."""
    return {path: value for path, value in (files or {}).items() if not _is_generated(path)}


def inventory(root: Path) -> dict:
    result, keys, total = {}, set(), 0
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in sorted(filesystem_path(folder).iterdir()):
            cancellation.checkpoint()
            # The same rule as goal_workspaces._manifest: dependency/cache
            # trees, virtual environments, bytecode, links and special files
            # are skipped, so every inventory of the same files agrees.
            if path.name.casefold() in _GENERATED or gw.excluded_entry(path):
                continue
            relative = path.relative_to(filesystem_path(root)).as_posix()
            key = portable_relative_path_key(relative)
            if key in keys:
                raise HarnessError("Agent workspace contains aliased paths: " + relative)
            keys.add(key)
            safe = filesystem_path(confined_path(root, relative, allow_missing=False))
            info = safe.stat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(safe)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HarnessError("Agent workspace needs independent regular files: " + relative)
            total += info.st_size
            if total > 2_000_000_000 or len(result) >= 100_000:
                raise HarnessError("Agent workspace exceeds its copy budget")
            digest = hashlib.sha256()
            with safe.open("rb") as stream:
                before = os.fstat(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    cancellation.checkpoint()
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            signature = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_mode, v.st_nlink)
            if signature(info) != signature(before) or signature(before) != signature(after):
                raise HarnessError("Agent file changed while being inspected: " + relative)
            result[relative] = {"sha256": digest.hexdigest(), "size": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
    return result


def binding(goal, agent):
    return gw._digest({"contract": CONTRACT, "goal_id": goal["goal_id"],
        "project": goal["project"], "execution_workspace": goal.get("execution_workspace"),
        "execution_contract": goal.get("execution_contract"),
        "agent": {k: agent.get(k) for k in ("id", "who", "route_binding")}})


def layout(goal, agent, runtime_root):
    if not goal.get("execution_workspace"):
        raise HarnessError("Native agents require a private goal execution workspace")
    gw.validate(goal, runtime_root)
    if not any(one.get("id") == agent.get("id") and binding(goal, one) == binding(goal, agent) for one in goal["agents"]):
        raise HarnessError("The agent does not belong to this goal")
    home = confined_path(Path(runtime_root), "agent-workspaces", allow_control=True)
    # Hash identifiers rather than using provider/user strings as path components.
    slot = gw._digest([goal["goal_id"], agent["id"]])[:32]
    folder = confined_path(home, slot + "/" + binding(goal, agent), allow_control=True)
    return home, folder, folder / "project"


def _read(home, folder):
    path = confined_path(folder, "state.json", allow_control=True, allow_missing=False)
    if path.stat().st_size > 100_000_000:
        raise HarnessError("Agent workspace state exceeds its limit")
    return gw._verify(gw._key(home), json.loads(path.read_text(encoding="utf-8")))


def _write(home, folder, state):
    path = confined_path(folder, "state.json", allow_control=True)
    content = gw._canonical(gw._sign(gw._key(home), state))
    atomic_write(path, content)


def existing_root(goal, agent, runtime_root):
    home, folder, project = layout(goal, agent, runtime_root)
    if not (folder / "state.json").exists():
        return None
    state = _read(home, folder)
    if state.get("contract") != CONTRACT or state.get("binding") != binding(goal, agent) \
            or state.get("identity") != gw._source_identity(gw._direct(project)):
        raise HarnessError("Agent workspace ownership changed")
    return project


def preserve_reconnected_copy(before, after, agent_id, runtime_root):
    """Stage the authenticated draft under its reviewed new provider binding.

    Retain the old copy until and after the goal transaction commits. A failed
    copy can therefore be retried without either losing work or rebinding the
    still-paused original goal to a partially written directory.
    """
    old_agent = next(a for a in before["agents"] if a["id"] == agent_id)
    agent = next(a for a in after["agents"] if a["id"] == agent_id)
    if binding(before, old_agent) == binding(after, agent):
        return
    original = existing_root(before, old_agent, runtime_root)
    if original is None:
        return
    old_home, old_folder, _ = layout(before, old_agent, runtime_root)
    home, folder, project = layout(after, agent, runtime_root)
    with ProjectTransactionLock(old_folder).held(), ProjectTransactionLock(folder).held():
        old_state = _read(old_home, old_folder)
        files = inventory(original)
        intent = {"schema_version": 1, "contract": "agent-workspace-reconnect/v1",
                  "from_binding": binding(before, old_agent), "to_binding": binding(after, agent),
                  "source_sha256": gw._digest([files, old_state])}
        filesystem_path(folder).mkdir(parents=True, exist_ok=True)
        if (folder / "state.json").exists():
            state = _read(home, folder)
            if state.get("reconnect_copy") != intent:
                raise HarnessError("The reconnected draft changed; retain both copies for inspection")
        else:
            filesystem_path(project).mkdir(exist_ok=True)
            if any(filesystem_path(project).iterdir()):
                raise HarnessError("An unowned directory occupies the reconnected draft")
            state = {**copy.deepcopy(old_state), "binding": binding(after, agent),
                     "identity": gw._source_identity(project), "reconnect_copy": intent}
            _write(home, folder, state)
        if state.get("identity") != gw._source_identity(gw._direct(project)):
            raise HarnessError("Reconnected draft directory ownership changed")
        held = inventory(project)
        if any(name not in files or held[name] != files[name] for name in held):
            raise HarnessError("Reconnected draft files changed; retain both copies for inspection")
        for name in files.keys() - held.keys():
            gw._copy_file(original, project, name, files[name])
        if inventory(project) != files or inventory(original) != files or _read(old_home, old_folder) != old_state:
            raise HarnessError("The saved draft changed while reconnecting; retry after inspection")


@contextmanager
def workspace(goal, agent, runtime_root):
    """Serialize an agent's tasks; synchronize accepted work without losing edits.

    A sync intent survives interruption. Repeating a partly applied sync accepts
    only the recorded before or after contents, never silently overwrites a third
    version. Old configuration generations are retained for inspection/recovery.
    """
    home, folder, project = layout(goal, agent, runtime_root)
    folder.mkdir(parents=True, exist_ok=True)
    gw._key(home, create=True)
    with ProjectTransactionLock(folder).held():
        source = gw.root(goal, runtime_root)
        incoming = inventory(source)
        if not (folder / "state.json").exists():
            project.mkdir(exist_ok=True)
            if any(project.iterdir()):
                raise HarnessError("Unowned agent workspace is not empty")
            state = {"schema_version": 1, "contract": CONTRACT, "binding": binding(goal, agent),
                     "identity": gw._source_identity(project), "baseline": {}, "sync": None}
            _write(home, folder, state)
        existing_root(goal, agent, runtime_root)
        # The agent runs commands and native tools here: it needs the goal
        # copy's installed dependencies too. Real copies, never published
        # (inventory ignores them); trees already present are left alone.
        gw._provide_dependency_trees(source, project)
        state = _read(home, folder)
        local = inventory(project)
        if not state.get("sync"):
            old = _without_generated(state["baseline"])
            updates, conflicts = {}, []
            for path in sorted(set(old) | set(incoming)):
                if gw._content(old.get(path)) == gw._content(incoming.get(path)):
                    continue
                actual = gw._content(local.get(path))
                if actual not in (gw._content(old.get(path)), gw._content(incoming.get(path))):
                    conflicts.append(path)
                else:
                    updates[path] = {"before": local.get(path), "after": incoming.get(path)}
            if conflicts:
                raise gw.WorkspaceConflict("Agent changes conflict with accepted team changes", conflicts)
            state["sync"] = {"incoming": incoming, "updates": updates}
            _write(home, folder, state)
        intent = state["sync"]
        # An intent saved before a folder joined _GENERATED (the tool caches)
        # still lists files the inventory now skips; compare and apply without
        # them, or that interrupted sync could never finish.
        intent = {**intent, "incoming": _without_generated(intent["incoming"]),
                  "updates": {path: update for path, update in intent["updates"].items()
                              if not _is_generated(path)}}
        if gw._contents(incoming) != gw._contents(intent["incoming"]):
            raise HarnessError("Accepted team files changed during interrupted agent synchronization")
        for path, update in intent["updates"].items():
            cancellation.checkpoint()
            actual = gw._content(local.get(path))
            if actual == gw._content(update["after"]):
                continue
            if actual != gw._content(update["before"]):
                raise gw.WorkspaceConflict("Agent file changed during synchronization", [path])
            if update["after"] is None:
                filesystem_path(confined_path(project, path, allow_missing=False)).unlink()
            else:
                gw._copy_file(source, project, path, update["after"])
        if inventory(source) != incoming:
            raise HarnessError("Accepted team files changed during agent synchronization")
        state.update(baseline=incoming, sync=None)
        _write(home, folder, state)
        provide_git_history(goal, project, source)
        yield AgentWorkspace(project, source, incoming)


GIT_HISTORY_CONTRACT = "nexus-agent-git-history/v3"
# Opt-in. Read-only git history in an agent's working copy is useful, but a
# repository inside the copy also makes it easy for an agent to "clean up"
# with git in ways publication would then apply. It stays off unless this is
# set to 1 for the Nexus process (see docs/AGENT_BOARD.md).
GIT_HISTORY_ENV = "NEXUS_AGENT_GIT_HISTORY"
BASELINE_BRANCH = "refs/heads/nexus/accepted-baseline"
BASELINE_MESSAGE = "Nexus: accepted baseline"
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Nexus", "GIT_AUTHOR_EMAIL": "nexus@localhost.invalid",
    "GIT_COMMITTER_NAME": "Nexus", "GIT_COMMITTER_EMAIL": "nexus@localhost.invalid",
}
# The Nexus-owned bare repository beside the copy (never inside it). Its
# config, info/attributes and hooks are Nexus's, so hashing the accepted
# files never runs anything an agent wrote into the copy's .git.
_PRIVATE_GIT = "nexus-baseline.git"
# Settings of the user's own repository that change how files are hashed;
# system and global git config (Git for Windows' core.autocrlf, git-lfs)
# still apply because only repository-selecting variables are dropped.
_COPIED_REPOSITORY_SETTINGS = r"^(core\.(autocrlf|eol|safecrlf|ignorecase)|filter\..*)$"


def git_history_enabled() -> bool:
    return os.environ.get(GIT_HISTORY_ENV, "").strip() == "1"


def _git_environment(extra: dict | None = None) -> dict:
    """The process environment without anything that selects a repository.

    Every GIT_* variable is dropped (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE,
    GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_COMMON_DIR,
    GIT_NAMESPACE, GIT_CONFIG*, GIT_CEILING_DIRECTORIES, ...): Nexus started
    from a git hook or IDE task would otherwise point these commands at the
    user's real repository. System and global config are kept.
    """
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    environment.update(extra or {})
    return environment


def _git(git: str, git_dir: Path, work_tree: Path | None, *arguments: str,
         timeout: float = 60.0, extra: dict | None = None,
         hooks: Path | None = None) -> subprocess.CompletedProcess:
    """Run git on exactly one repository, never one chosen by the environment.

    ``hooks`` names an empty Nexus-owned directory used as ``core.hooksPath``
    and turns off ``core.fsmonitor``, so a repository an agent can write to
    cannot run its hooks or monitor inside Nexus.
    """
    selector = [f"--git-dir={git_dir}"]
    if work_tree is not None:
        selector.append(f"--work-tree={work_tree}")
    hardening = []
    if hooks is not None:
        hardening = ["-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false"]
    return subprocess.run(
        [git, *hardening, *selector, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
        timeout=timeout, env=_git_environment(extra), check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _text(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def _private_git(project: Path) -> Path:
    return Path(project).parent / _PRIVATE_GIT


def _no_hooks(private: Path) -> Path:
    return private / "nexus-no-hooks"


def _nexus_git_contract(git: str, dot_git: Path, hooks: Path) -> str:
    marked = _git(git, dot_git, None, "config", "--get", "nexus.agentCopy", timeout=30, hooks=hooks)
    return _text(marked) if marked.returncode == 0 else ""


def _prepare_private(git: str, private: Path, real_git: Path) -> None:
    """Create the Nexus-owned bare repository used to hash accepted files."""
    if not (private / "HEAD").exists():
        shutil.rmtree(private, ignore_errors=True)
        made = subprocess.run(
            [git, "init", "--bare", "--quiet", str(private)], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=60, env=_git_environment(), check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if made.returncode != 0:
            raise OSError("git could not prepare the baseline repository")
        copied = _git(git, real_git, None, "config", "--local", "--get-regexp",
                      _COPIED_REPOSITORY_SETTINGS, timeout=30)
        for line in _text(copied).splitlines():
            name, _space, value = line.partition(" ")
            _git(git, private, None, "config", name, value, timeout=30)
    _no_hooks(private).mkdir(exist_ok=True)
    (private / "info").mkdir(exist_ok=True)
    (private / "info" / "attributes").write_text("", encoding="utf-8")


def _baseline_tree(git: str, private: Path, dot_git: Path, source: Path) -> str:
    """Tree object of the accepted team files, hashed by the private repo.

    Objects are written straight into the copy's object store; the private
    index is kept between syncs so unchanged files are not re-hashed.
    """
    extra = {"GIT_INDEX_FILE": str(private / "baseline-index"),
             "GIT_OBJECT_DIRECTORY": str(dot_git / "objects")}
    hooks = _no_hooks(private)
    # Only what the copy holds: runtime and dependency folders are never
    # copied, so they must not appear as deletions against the baseline.
    excluded = [pattern for name in sorted(_GENERATED) if name != ".git"
                for pattern in (f":(exclude,glob,icase)**/{name}",
                                f":(exclude,glob,icase)**/{name}/**")]
    for attempt in range(2):
        added = _git(git, private, source, "add", "-A", "--", ".", *excluded,
                     timeout=600, extra=extra, hooks=hooks)
        tree = _git(git, private, source, "write-tree", timeout=120, extra=extra, hooks=hooks) \
            if added.returncode == 0 else added
        if tree.returncode == 0:
            return _text(tree)
        # The kept index can name objects an agent pruned from the copy
        # (``git gc --prune=now``); re-hash everything once from scratch.
        (private / "baseline-index").unlink(missing_ok=True)
    raise OSError("git could not record the accepted files")


def _record_baseline(git: str, private: Path, dot_git: Path, project: Path, source: Path) -> None:
    """Commit the accepted team files as the copy's starting point.

    The copy's files are the accepted team baseline, not the user's HEAD, so
    ``git status``/``git diff`` must compare against that: otherwise the
    user's uncommitted edits and teammates' accepted work would look like
    this agent's changes, and an agent reverting them would publish reverts.
    """
    hooks = _no_hooks(private)
    objects = {"GIT_OBJECT_DIRECTORY": str(dot_git / "objects")}
    tree = _baseline_tree(git, private, dot_git, source)
    tip = _git(git, dot_git, None, "rev-parse", "--verify", "--quiet", BASELINE_BRANCH,
               timeout=30, hooks=hooks)
    parent = _text(tip) if tip.returncode == 0 else ""
    if not parent:
        head = _git(git, dot_git, None, "rev-parse", "--verify", "--quiet", "HEAD",
                    timeout=30, hooks=hooks)
        parent = _text(head) if head.returncode == 0 else ""
        if parent:
            _git(git, private, None, "config", "nexus.userHead", parent, timeout=30)
    old_tree = ""
    if tip.returncode == 0:
        old_tree = _text(_git(git, dot_git, None, "rev-parse", f"{parent}^{{tree}}",
                              timeout=30, hooks=hooks))
        if old_tree == tree:
            return
    commit = _git(git, private, None, "commit-tree", tree, *(["-p", parent] if parent else []),
                  "-m", BASELINE_MESSAGE, timeout=60, extra={**_GIT_IDENTITY, **objects},
                  hooks=hooks)
    if commit.returncode != 0:
        raise OSError("git could not record the accepted baseline")
    made = _text(commit)
    if _git(git, dot_git, None, "update-ref", BASELINE_BRANCH, made, timeout=30,
            hooks=hooks).returncode != 0:
        raise OSError("git could not move the accepted baseline")
    head = _git(git, dot_git, None, "symbolic-ref", "--quiet", "HEAD", timeout=30, hooks=hooks)
    on_baseline = head.returncode == 0 and _text(head) == BASELINE_BRANCH
    if tip.returncode != 0:
        _git(git, dot_git, None, "symbolic-ref", "HEAD", BASELINE_BRANCH, timeout=30, hooks=hooks)
        on_baseline = True
    if not on_baseline:
        return  # The agent moved HEAD itself; leave its own state alone.
    staged = _git(git, dot_git, project, "write-tree", timeout=120, hooks=hooks)
    if tip.returncode != 0 or _text(staged) == old_tree:
        _git(git, dot_git, project, "read-tree", made, timeout=300, hooks=hooks)


def _unreachable_from_the_project(git: str, dot_git: Path, real: Path) -> bool:
    """No remote, alternate or config entry can reach the user's repository."""
    remotes = _git(git, dot_git, None, "config", "--get-regexp", r"^remote\.", timeout=30)
    if remotes.stdout.strip():
        return False
    if (dot_git / "objects" / "info" / "alternates").exists():
        return False
    config = (dot_git / "config").read_text(encoding="utf-8", errors="replace")
    spelled = {str(real), str(real.resolve()), real.as_posix(), real.resolve().as_posix()}
    folded = config.casefold().replace("\\\\", "\\")
    return not any(one.casefold() in folded for one in spelled if one)


def _real_git_dir(real: Path) -> Path:
    pointer = real / ".git"
    if pointer.is_file():
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
        target = text.split(":", 1)[1].strip() if text.lower().startswith("gitdir:") else ""
        return (pointer.parent / target).resolve() if target else pointer
    return pointer


def provide_git_history(goal, project: Path, source: Path | None = None) -> bool:
    """Opt-in: let the agent run git status/diff/log in its working copy.

    Nothing happens, not even a baseline refresh of an existing copy, unless
    ``NEXUS_AGENT_GIT_HISTORY=1``. Best effort and never a reason to fail the
    agent's turn. When the real project folder is the top of a git work tree
    and the copy has no ``.git`` yet, an independent clone (``--no-hardlinks``:
    no alternates, no shared files) is made beside the copy. Its ``origin``
    is removed and Nexus verifies that no remote, alternate or config entry
    refers to the user's repository; otherwise the whole ``.git`` is dropped.
    The clone's ``.git`` is moved into the copy, and the accepted team files
    are committed as ``nexus/accepted-baseline`` (checked out, index loaded,
    the copy's files untouched) and refreshed on every later sync. Hashing
    runs in a Nexus-owned bare repository beside the copy, so filters,
    attributes or hooks an agent writes into the copy's ``.git`` never run in
    Nexus; every call on the copy's ``.git`` disables hooks and fsmonitor and
    names its repository explicitly, ignoring GIT_* variables. The copy's
    ``.git`` is never collected or published (it is in ``_GENERATED``); a
    ``.git`` the agent made itself is left alone. The user's repository is
    only read.
    """
    if not git_history_enabled():
        # Opt-in was withdrawn. A copy made while it was on keeps a baseline
        # branch that no longer refreshes, so ``git checkout -- file`` would
        # revert a teammate's accepted work and publish the revert. Remove
        # that .git; it is recognised from its config as plain text, without
        # running git on it. A .git the agent made itself is left alone.
        _drop_nexus_git(Path(project))
        return False
    staging = project.parent / "git-history-staging"
    # Plain paths: git does not accept the Windows long-path prefix.
    destination = Path(project) / ".git"
    private = _private_git(project)
    try:
        git = shutil.which("git")
        if not git:
            return False
        real = Path(str((goal.get("project") or {}).get("path") or ""))
        if not str(real) or not (real / ".git").exists():
            return False
        real_git = _real_git_dir(real)
        _prepare_private(git, private, real_git)
        hooks = _no_hooks(private)
        if destination.is_dir() and not destination.is_symlink():
            contract = _nexus_git_contract(git, destination, hooks)
            if contract == GIT_HISTORY_CONTRACT:
                if source is not None:
                    _record_baseline(git, private, destination, project, source)
                return False
            if contract.startswith("nexus-agent-git-history/"):
                # An earlier contract shared objects, kept origin or hashed
                # with the copy's own config; replace it.
                shutil.rmtree(destination, ignore_errors=True)
        if destination.exists() or destination.is_symlink():
            return False  # The agent's own .git.
        top = _git(git, real_git, real, "rev-parse", "--show-toplevel", timeout=30)
        if top.returncode != 0 or os.path.normcase(str(Path(_text(top)).resolve())) \
                != os.path.normcase(str(real.resolve())):
            return False  # A folder inside a larger repository: history would not line up.
        shutil.rmtree(filesystem_path(staging), ignore_errors=True)
        cloned = subprocess.run(
            [git, "-c", f"core.hooksPath={hooks}", "clone", "--quiet", "--no-checkout",
             "--no-hardlinks", "--", str(real), str(staging)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
            env=_git_environment(), check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if cloned.returncode != 0:
            return False
        staged_git = staging / ".git"
        if _git(git, staged_git, None, "remote", "remove", "origin", timeout=30,
                hooks=hooks).returncode != 0:
            return False
        if _git(git, staged_git, None, "config", "nexus.agentCopy", GIT_HISTORY_CONTRACT,
                timeout=30, hooks=hooks).returncode != 0:
            return False
        if not _unreachable_from_the_project(git, staged_git, real):
            return False
        if destination.exists():
            return False
        (private / "baseline-index").unlink(missing_ok=True)
        os.replace(staged_git, destination)
        try:
            _record_baseline(git, private, destination, project, source or project)
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(destination, ignore_errors=True)
            return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(filesystem_path(staging), ignore_errors=True)


_NEXUS_GIT_MARK = re.compile(r"^\s*agentcopy\s*=\s*nexus-agent-git-history/",
                             re.IGNORECASE | re.MULTILINE)


def _drop_nexus_git(project: Path) -> bool:
    """Remove a Nexus-made .git from a copy without running git on it."""
    dot_git = Path(project) / ".git"
    try:
        if dot_git.is_symlink() or not dot_git.is_dir():
            return False
        config = (dot_git / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not _NEXUS_GIT_MARK.search(config):
        return False

    def writable(function, path, _error):
        # git makes pack files read-only; Windows refuses to delete those.
        os.chmod(path, stat.S_IWRITE)
        function(path)

    for folder in (dot_git, _private_git(project)):
        if folder.exists():
            if sys.version_info >= (3, 12):
                shutil.rmtree(folder, onexc=writable)
            else:  # pragma: no cover - older interpreters
                shutil.rmtree(folder, onerror=writable)
    return not dot_git.exists()


def _held_back_deletions(root: Path, paths: list[str]) -> set[str]:
    """Deleted baseline files that must not be deleted from the project.

    Those are files the copy's git ignores and that the user never committed
    (``.env`` and the like): the agent did not create them in this session,
    and ``git clean -fdx`` or ``git stash -a`` in the copy must not remove
    them from the real project. A tracked file that happens to match an
    ignore rule (``git add -f build/out.txt``) is an ordinary deletion.
    """
    if not paths or not git_history_enabled():
        return set()
    dot_git = Path(root) / ".git"
    private = _private_git(Path(root))
    git = shutil.which("git")
    if not git or not dot_git.is_dir() or not (private / "HEAD").exists():
        return set()
    hooks = _no_hooks(private)
    try:
        if _nexus_git_contract(git, dot_git, hooks) != GIT_HISTORY_CONTRACT:
            return set()
        checked = subprocess.run(
            [git, "-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false",
             f"--git-dir={private}", f"--work-tree={root}", "check-ignore",
             "--no-index", "--stdin", "-z"],
            input=("\0".join(paths) + "\0").encode("utf-8"), capture_output=True,
            timeout=60, env=_git_environment(), check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if checked.returncode not in (0, 1):
            raise OSError("git could not check ignore rules")
        ignored = {one for one in checked.stdout.decode("utf-8", "replace").split("\0") if one}
        if not ignored:
            return set()
        user_head = _text(_git(git, private, None, "config", "--get", "nexus.userHead", timeout=30))
        tracked: set[str] = set()
        if user_head:
            listed = _git(git, private, None, "ls-tree", "-r", "-z", "--name-only", user_head,
                          "--", *sorted(ignored), timeout=60,
                          extra={"GIT_OBJECT_DIRECTORY": str(dot_git / "objects")}, hooks=hooks)
            if listed.returncode != 0:
                raise OSError("git could not read the user's HEAD")
            tracked = {one for one in listed.stdout.decode("utf-8", "replace").split("\0") if one}
        return ignored - tracked
    except (OSError, ValueError, subprocess.SubprocessError):
        # Cannot tell: hold every such deletion back (and say so) rather
        # than risk deleting the user's .env.
        return set(paths)


class AgentWorkspace:
    def __init__(self, root, source, baseline):
        self.root, self.source, self.baseline = root, source, baseline
        # Deletions Nexus did not publish; see _held_back_deletions.
        self.held_back_deletions: list[str] = []

    def changes(self, *, max_files=12, max_bytes=4_000_000):
        current = inventory(self.root)
        changes, total = [], 0
        kept_ignored = _held_back_deletions(
            self.root, [path for path in self.baseline if path not in current])
        self.held_back_deletions = sorted(kept_ignored)
        for path in sorted(set(current) | set(self.baseline)):
            if gw._content(current.get(path)) == gw._content(self.baseline.get(path)):
                continue
            if path in kept_ignored:
                continue
            one = {"path": path, "reason": "Collected from the agent's working copy"}
            if path not in current:
                one["delete"] = True
            else:
                one["mode"] = current[path]["mode"] & 0o777
                raw = filesystem_path(confined_path(self.root, path, allow_missing=False)).read_bytes()
                if hashlib.sha256(raw).hexdigest() != current[path]["sha256"]:
                    raise HarnessError("Agent changed its output while Nexus collected it: " + path)
                total += len(raw)
                try:
                    one["content"] = raw.decode("utf-8")
                except UnicodeError:
                    one["content_base64"] = base64.b64encode(raw).decode("ascii")
            changes.append(one)
            if len(changes) > max_files or total > max_bytes:
                raise HarnessError("Agent candidate exceeds the configured publication budget; its copy is retained")
        return changes

    def collect_action(self, action, *, max_bytes, max_files=12):
        from .swarm_work import _validated_changes
        from .changes import FileTransaction
        if action.get("tool_calls") or action.get("needs_files") or action.get("action") not in {"work", "complete", "request_review"}:
            return action
        # Native output and structured proposals must agree if they touch the
        # same path. Apply remaining proposals to the private copy for inspection.
        native = {one["path"]: one for one in self.changes(max_bytes=max_bytes, max_files=max_files)}
        for one in action.get("changes", []):
            prior = native.get(one.get("path"))
            if prior and any((prior.get(key) or "") != (one.get(key) or "")
                             for key in ("content", "content_base64", "delete")):
                raise HarnessError("Native edits and structured proposal disagree: " + str(one.get("path")))
        plans = _validated_changes(self.root, action.get("changes", []))
        if plans:
            FileTransaction(self.root, max_files=max_files, max_bytes=max_bytes).apply(plans)
        changes = self.changes(max_bytes=max_bytes, max_files=max_files)
        held_back = {"_nexus_held_back_deletions": {
            "paths": list(self.held_back_deletions),
            "note": ("Nexus did not delete these gitignored files from the project: "
                     "they were in the accepted baseline and never committed by the "
                     "user, so a git clean/stash in the working copy is not taken as "
                     "a request to delete them. Delete them explicitly if you meant to."),
        }} if self.held_back_deletions else {}
        return {**action, "changes": changes, **held_back,
                "_nexus_agent_workspace": str(self.root),
                "_nexus_agent_baseline": {one["path"]: self.baseline.get(one["path"]) for one in changes}}


def descriptors(goal, runtime_root):
    entries = [{"id": "real", "label": "Real project", "path": str(goal["project"]["path"]), "available": True}]
    if goal.get("execution_workspace"):
        for agent in goal.get("agents", []):
            root = existing_root(goal, agent, runtime_root)
            entries.append({"id": str(agent["id"]), "label": str(agent["name"]) + "'s copy",
                            "path": str(root or layout(goal, agent, runtime_root)[2]), "available": root is not None})
    return entries


def inspect(goal, runtime_root, workspace_id, relative="", *, cursor=""):
    """Read-only UI browsing: callers select an owned ID, never an absolute root."""
    if workspace_id == "real":
        root = gw._direct(Path(goal["project"]["path"]))
    else:
        agent = next((one for one in goal.get("agents", []) if one["id"] == workspace_id), None)
        if agent is None:
            raise HarnessError("This workspace does not belong to the selected goal")
        root = existing_root(goal, agent, runtime_root)
        if root is None:
            raise HarnessError("This agent's copy will be created when it starts work")
    target = filesystem_path(root if relative in ("", ".") else confined_path(root, relative, allow_missing=False))
    if any(part.casefold() in _GENERATED for part in Path(relative).parts):
        raise HarnessError("Runtime and dependency folders are not shown in the workspace viewer")
    if target.is_dir():
        entries = []
        for path in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if path.name.casefold() in _GENERATED:
                continue
            path = confined_path(root, path.relative_to(filesystem_path(root)).as_posix(), allow_missing=False)
            entries.append({"name": path.name, "path": path.relative_to(root).as_posix(),
                            "directory": filesystem_path(path).is_dir()})
            if len(entries) > 2000:
                raise HarnessError("This folder has too many entries for the workspace viewer")
        return {"kind": "directory", "path": relative, "entries": entries, "root": str(root)}
    if target.stat().st_size > 8_000_000:
        return {"kind": "binary", "path": relative, "size": target.stat().st_size, "root": str(root)}
    from .bounded_file_read import read_file_page
    raw = target.read_bytes()
    try:
        if not relative.lower().endswith(".docx"):
            raw.decode("utf-8")
        page = read_file_page(raw, {"path": relative, "start_line": 1, "end_line": 10_000_000,
            "max_bytes": 12000, "cursor": cursor},
            output_limit=20000, configured_output_limit=20000, max_file_bytes=8_000_000)
    except UnicodeError:
        return {"kind": "binary", "path": relative, "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "root": str(root)}
    return {"kind": "file", "root": str(root), **page}
