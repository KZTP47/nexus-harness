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

# Leases serialize overlapping writers; they never refuse them outright. A
# caller waits (queues) for the lease and only reports "busy" after a generous
# wait, as an observation the agent can act on. These are machine protections
# against two writers corrupting the same files at the same instant.
TOOL_LEASE_WAIT_SECONDS = 120
APPLY_LEASE_WAIT_SECONDS = 300
NATIVE_LEASE_WAIT_SECONDS = 1800
# A proposal's file count is a safety bound only, well above any real edit.
MAX_PROPOSAL_FILES = 500


class OperationBusy(HarnessError):
    pass


def _storage():
    from .swarm_runs import _base
    return _base()


_POLL_SECONDS = 0.1
# A queued waiter refreshes last_seen on every poll. A ticket not seen for this
# long belongs to a waiter that is gone (its drop failed, or its thread died in
# a still-running server) and no longer blocks anyone.
TICKET_STALE_SECONDS = 30.0
_DROP_ATTEMPTS = 5


def _schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS operation_leases(id TEXT PRIMARY KEY, root TEXT, pid INTEGER, token TEXT, contract TEXT)")
    # FIFO queue: a waiter takes a ticket once and is admitted only when no
    # older live waiter for an overlapping root is still queued.
    db.execute("CREATE TABLE IF NOT EXISTS operation_waiters(ticket INTEGER PRIMARY KEY AUTOINCREMENT, "
               "lease TEXT UNIQUE, root TEXT, pid INTEGER, token TEXT, last_seen REAL)")
    columns = {row[1] for row in db.execute("PRAGMA table_info(operation_waiters)")}
    if "last_seen" not in columns:  # A queue table created before heartbeats.
        db.execute("ALTER TABLE operation_waiters ADD COLUMN last_seen REAL")


def _overlaps(path, other):
    return path == other or path in other.parents or other in path.parents


def _take_ticket(database, path, lease):
    from .pipeline_runs import _process_token
    try:
        with closing(sqlite3.connect(database, timeout=2)) as db, db:
            _schema(db)
            db.execute("INSERT OR IGNORE INTO operation_waiters(lease, root, pid, token, last_seen) VALUES(?,?,?,?,?)",
                       (lease, str(path), os.getpid(), _process_token(os.getpid()), time.time()))
        return True
    except sqlite3.OperationalError:
        return False  # Busy database: queue again on the next poll.


def _drop_ticket(database, lease):
    for attempt in range(_DROP_ATTEMPTS):
        try:
            with closing(sqlite3.connect(database, timeout=2)) as db, db:
                _schema(db)
                db.execute("DELETE FROM operation_waiters WHERE lease=?", (lease,))
            return
        except sqlite3.OperationalError:
            time.sleep(0.05 * (attempt + 1))
    # Still busy: the ticket stops being refreshed and turns stale on its own.


def _try_claim(database, path, lease):
    try:
        return _try_claim_once(database, path, lease)
    except sqlite3.OperationalError:
        return False  # A locked/busy database is one more poll, not a crash.


def _try_claim_once(database, path, lease):
    from .pipeline_runs import _owner_is_alive, _process_token
    with closing(sqlite3.connect(database, timeout=2)) as db, db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        # Heartbeat first: a waiter queued behind a long hold (a native turn
        # can take 30 minutes) must stay fresh, or it would look abandoned.
        now = time.time()
        db.execute("UPDATE operation_waiters SET last_seen=? WHERE lease=?", (now, lease))
        for row in db.execute("SELECT id,root,pid,token,contract FROM operation_leases").fetchall():
            if not _owner_is_alive(row[2], row[3]):
                db.execute("DELETE FROM operation_leases WHERE id=?", (row[0],))
                continue
            if _overlaps(path, Path(row[1]).resolve()):
                return False
        mine = db.execute("SELECT ticket FROM operation_waiters WHERE lease=?", (lease,)).fetchone()
        for ticket, other_lease, other_root, pid, token, last_seen in db.execute(
                "SELECT ticket, lease, root, pid, token, last_seen FROM operation_waiters ORDER BY ticket").fetchall():
            if other_lease == lease:
                break
            if not _owner_is_alive(pid, token) or last_seen is None or now - float(last_seen) > TICKET_STALE_SECONDS:
                db.execute("DELETE FROM operation_waiters WHERE ticket=?", (ticket,))
                continue
            if (mine is None or ticket < mine[0]) and _overlaps(path, Path(other_root).resolve()):
                return False  # An older waiter for this project goes first.
        db.execute("DELETE FROM operation_waiters WHERE lease=?", (lease,))
        db.execute("INSERT INTO operation_leases VALUES(?,?,?,?,?)",
                   (lease, str(path), os.getpid(), _process_token(os.getpid()), CONTRACT))
    return True


def _waited(wait_seconds):
    seconds = int(wait_seconds or 0)
    return f" Nexus waited {seconds} seconds for it." if seconds >= 1 else ""


@contextmanager
def claim(root, runtime_root=None, *, wait_seconds=0, should_stop=None):
    """Hold the overlapping-root lease, queueing up to ``wait_seconds`` for it."""
    folder = Path(runtime_root or _storage())
    folder.mkdir(parents=True, exist_ok=True)
    database = folder / "project-operations.sqlite3"
    lease = uuid.uuid4().hex
    path = Path(root).resolve()
    from . import cancellation
    started = time.monotonic()
    deadline = started + max(0.0, float(wait_seconds or 0))
    next_stop_check = started + 2.0
    queued = False
    try:
        while not _try_claim(database, path, lease):
            now = time.monotonic()
            remaining = deadline - now
            if not queued and remaining > 0:
                queued = _take_ticket(database, path, lease)
            token = cancellation.current()
            if token is not None:
                token.checkpoint()  # A stopped or handed-off turn frees its slot now.
            stop = False
            if should_stop is not None and now >= next_stop_check:
                next_stop_check = now + 2.0
                stop = should_stop()
            if remaining <= 0 or stop:
                raise OperationBusy("Another write operation is still using this project." + _waited(now - started)
                                    + " Continue inspection or other work, then retry the write.")
            # One fixed interval; the ticket order, not polling speed, decides who is next.
            time.sleep(min(_POLL_SECONDS, remaining))
    finally:
        if queued:
            _drop_ticket(database, lease)
    try:
        yield
    finally:
        for attempt in range(_DROP_ATTEMPTS):
            try:
                with closing(sqlite3.connect(database, timeout=2)) as db, db:
                    db.execute("DELETE FROM operation_leases WHERE id=?", (lease,))
                break
            except sqlite3.OperationalError:
                if attempt == _DROP_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))


@contextmanager
def transaction(root, runtime_root=None, *, wait_seconds=0, should_stop=None, **limits):
    started = time.monotonic()
    with claim(root, runtime_root, wait_seconds=wait_seconds, should_stop=should_stop):
        transaction = FileTransaction(Path(root), **limits)
        # Older FileTransaction callers hold only the project file lock; queue
        # behind them for whatever remains of the same wait.
        remaining = max(0.0, float(wait_seconds or 0) - (time.monotonic() - started))
        held = transaction.locked(timeout_seconds=remaining)
        try:
            held.__enter__()
        except HarnessError as exc:
            if "transaction lock" in str(exc):
                raise OperationBusy("Another file operation is still using this project." + _waited(wait_seconds)
                                    + " Discussion and inspection can continue.") from exc
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
    with transaction(root, runtime.store.root, wait_seconds=APPLY_LEASE_WAIT_SECONDS,
                     should_stop=_stopped(runtime, goal),
                     max_files=max(MAX_PROPOSAL_FILES, int(runtime.config.get("execution.max_changed_files") or 0)),
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


def _stopped(runtime, goal, task=None):
    """Stop queueing for a lease once the user paused or cancelled the goal,
    or once the waiting task lost its own lease (it was handed off)."""
    goal_id = goal.get("goal_id") if isinstance(goal, dict) else None
    store = getattr(runtime, "store", None)
    if not goal_id or store is None or not hasattr(store, "get"):
        return None

    def stopped():
        try:
            current = store.get(goal_id)
            if current.get("status") in {"paused", "cancelled", "cancelling", "waiting_for_user"}:
                return True
            if isinstance(task, dict) and task.get("lease_id"):
                held = next((one for one in current.get("tasks", []) if one.get("id") == task.get("id")), None)
                return held is None or held.get("lease_id") != task.get("lease_id")
            return False
        except Exception:  # A vanished goal has nothing left to wait for.
            return True
    return stopped


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
def native_turn(runtime, goal, route, profile, observations, task=None):
    from contextlib import ExitStack
    from . import long_horizon as lh
    root = lh._execution_root(goal)
    with ExitStack() as stack:
        if profile != "work" or not native_capable(runtime.config, route):
            yield profile, ""
            return
        try:
            # Queue behind a teammate's writable turn rather than dropping to
            # inspection: both agents keep write access and their writes are
            # serialized. Only a very long wait falls back to inspection.
            stack.enter_context(transaction(root, runtime.store.root, wait_seconds=NATIVE_LEASE_WAIT_SECONDS,
                                            should_stop=_stopped(runtime, goal, task)))
            require_coordinated(runtime, goal)
        except OperationBusy as exc:
            # Release a partially acquired lock before allowing inspection.
            stack.close()
            yield "inspect", str(exc) + " This invocation has native inspection access. You can still communicate, read files and propose changes through Nexus tools. Saved permissions have not changed."
            return
        # Change record: reuse hashes only before the effect; afterwards every
        # file is hashed again, so an in-place same-size rewrite is seen.
        before = lh._project_effect_manifest(root, reuse_hashes=True)
        try:
            yield profile, ""
        finally:
            observations.extend(observed_changes(before, lh._project_effect_manifest(root)))


def run_effect(runtime, goal, callback, observations=None):
    from . import long_horizon as lh
    root = lh._execution_root(goal)
    try:
        with transaction(root, runtime.store.root, wait_seconds=TOOL_LEASE_WAIT_SECONDS,
                         should_stop=_stopped(runtime, goal)):
            require_coordinated(runtime, goal)
            before = lh._project_effect_manifest(root, reuse_hashes=True)
            try:
                return callback()
            finally:
                if observations is not None:
                    observations.extend(observed_changes(before, lh._project_effect_manifest(root)))
    except OperationBusy as exc:
        return unavailable(exc)


@contextmanager
def publication_claim(root, runtime_root, timeout_seconds=None):
    with claim(root, runtime_root, wait_seconds=60 if timeout_seconds is None else timeout_seconds):
        yield
