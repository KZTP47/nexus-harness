"""Short-lived, cross-process coordination of overlapping project mutations.

Conversation ownership is not a write lease. SQLite arbitrates overlapping
roots; the existing transaction lock also coordinates publication and older
FileTransaction callers. A dead process cannot retain a lease across restart.
"""
from contextlib import contextmanager, closing
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .changes import FileTransaction
from .models import HarnessError

CONTRACT = "overlapping-project-operation-leases/v1"


class OperationBusy(HarnessError):
    pass


def _storage():
    from .swarm_runs import _base
    return _base()


@contextmanager
def claim(root, runtime_root=None):
    from .pipeline_runs import _owner_is_alive, _process_token
    folder = Path(runtime_root or _storage())
    folder.mkdir(parents=True, exist_ok=True)
    database = folder / "project-operations.sqlite3"
    lease = uuid.uuid4().hex
    path = Path(root).resolve()
    with closing(sqlite3.connect(database, timeout=2)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS operation_leases(id TEXT PRIMARY KEY, root TEXT, pid INTEGER, token TEXT, contract TEXT)")
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT id,root,pid,token,contract FROM operation_leases").fetchall():
            if not _owner_is_alive(row[2], row[3]):
                db.execute("DELETE FROM operation_leases WHERE id=?", (row[0],))
                continue
            other = Path(row[1]).resolve()
            if path == other or path in other.parents or other in path.parents:
                raise OperationBusy("Another write operation is using this project. Continue inspection or other work, then retry the write.")
        db.execute("INSERT INTO operation_leases VALUES(?,?,?,?,?)",
                   (lease, str(path), os.getpid(), _process_token(os.getpid()), CONTRACT))
    try:
        yield
    finally:
        with closing(sqlite3.connect(database, timeout=2)) as db, db:
            db.execute("DELETE FROM operation_leases WHERE id=?", (lease,))


@contextmanager
def transaction(root, runtime_root=None, **limits):
    with claim(root, runtime_root):
        transaction = FileTransaction(Path(root), **limits)
        held = transaction.locked(timeout_seconds=0)
        try:
            held.__enter__()
        except HarnessError as exc:
            if "transaction lock" in str(exc):
                raise OperationBusy("Another file operation is using this project. Discussion and inspection can continue.") from exc
            raise
        try:
            yield transaction
        finally:
            held.__exit__(None, None, None)


def unavailable(error):
    return {"status": "busy", "executed": False, "reason": str(error),
            "coordination_contract": CONTRACT}


def observed_changes(before, after):
    return [{"path": path, "before_sha256": before.get(path, "").removeprefix("file:"),
             "after_sha256": after.get(path, "").removeprefix("file:")}
            for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]


def apply_proposal(runtime, goal, task, action):
    from . import long_horizon as lh
    root = lh._execution_root(goal)
    with transaction(root, runtime.store.root, max_files=12,
                     max_bytes=int(runtime.config.get("execution.max_changed_bytes"))) as tx:
        require_coordinated(runtime, goal)
        baselines = action.get("_nexus_baselines")
        if not isinstance(baselines, dict):
            raise HarnessError("The proposal has no observed project baseline")
        for change in action["changes"]:
            path = str(change.get("path") or "").replace("\\", "/").strip()
            if path not in baselines or baselines[path] != lh._path_baseline_marker(root, path):
                raise ProposalConflict("File changed since your observation: " + path + ". Read the current file and propose the intended edit again.")
        plans = lh.swarm_work._validated_changes(root, action["changes"])
        pending = task.get("pending_transaction") or {}
        if not plans:
            if pending:
                raise HarnessError("An unfinished transaction requires exact recovery")
            merkle, tree = lh.swarm_work._project_tree_merkle(root)
            return {"kind": "verified_no_change", "tree_merkle": merkle, "file_count": len(tree), "observed_at_ms": lh._now()}
        txid = pending.get("transaction_id") or FileTransaction.new_transaction_id()
        if not pending:
            runtime.store.prepare_transaction(goal["goal_id"], task, txid, action["changes"])
        result = tx.apply(plans, transaction_id=txid)
        artifact = {"kind": "file_transaction", "transaction_id": txid,
                    "changes": result.get("changes", []), "patch": lh._short(result.get("patch"), 80000),
                    "patch_sha256": result.get("patch_sha256", ""),
                    "tree_merkle": lh.swarm_work._project_tree_merkle(root)[0]}
        runtime.store.record_transaction_applied(goal["goal_id"], task, artifact)
        return artifact


class ProposalConflict(HarnessError):
    pass


def native_capable(config, route):
    from .providers import ProviderRegistry
    if str(route).startswith("web:"):
        return False
    routed = ProviderRegistry(config).provider_config(route) if route else config
    return routed.get("provider.name") in {"codex-cli", "claude-cli"}


def require_coordinated(runtime, goal):
    root = Path(goal["project"]["path"])
    external = runtime.external_project_conflicts(root) if runtime.external_project_conflicts else []
    if external or runtime.store.uncoordinated_writers(goal):
        raise OperationBusy("An older project writer is active. Discussion and inspection remain available; retry this write after that operation finishes.")


@contextmanager
def native_turn(runtime, goal, route, profile, observations):
    from contextlib import ExitStack
    from . import long_horizon as lh
    root = lh._execution_root(goal)
    with ExitStack() as stack:
        if profile != "work" or not native_capable(runtime.config, route):
            yield profile, ""
            return
        try:
            stack.enter_context(transaction(root, runtime.store.root))
            require_coordinated(runtime, goal)
        except OperationBusy as exc:
            # Release a partially acquired lock before allowing inspection.
            stack.close()
            yield "inspect", str(exc) + " This invocation has native inspection access. You can still communicate, read files and propose changes through Nexus tools. Saved permissions have not changed."
            return
        before = lh._project_baseline_manifest(root)
        try:
            yield profile, ""
        finally:
            observations.extend(observed_changes(before, lh._project_baseline_manifest(root)))


def run_effect(runtime, goal, callback, observations=None):
    from . import long_horizon as lh
    root = lh._execution_root(goal)
    try:
        with transaction(root, runtime.store.root):
            require_coordinated(runtime, goal)
            before = lh._project_baseline_manifest(root)
            try:
                return callback()
            finally:
                if observations is not None:
                    observations.extend(observed_changes(before, lh._project_baseline_manifest(root)))
    except OperationBusy as exc:
        return unavailable(exc)


@contextmanager
def publication_claim(root, runtime_root, timeout_seconds=None):
    deadline = time.monotonic() + (60 if timeout_seconds is None else timeout_seconds)
    while True:
        held = claim(root, runtime_root)
        try:
            held.__enter__()
            break
        except OperationBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        held.__exit__(None, None, None)
