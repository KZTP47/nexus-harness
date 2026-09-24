"""One durable store for a v3 team: messages, tasks, leases, questions, results.

Replaces the five separate message stores of the older engines for teams run
on Agent Runtime v3. SQLite in WAL mode, written by the Nexus server and by the
per-agent MCP server processes (``mcp_server.py``).

Tasks follow a closure contract taken from OpenRig's "hot-potato" rule: a task
can only be closed with a reason saying where the work went (handed off to,
blocked on, denied, cancelled, no follow-on, escalation), and every state change
is kept in an append-only transition log.
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..models import HarnessError

SCHEMA_VERSION = 1
TASK_STATES = ("open", "claimed", "in_progress", "blocked", "done", "cancelled")
CLOSURE_REASONS = ("completed", "handed_off", "blocked_on", "denied", "cancelled", "no_follow_on", "escalated")
NEEDS_TARGET = {"handed_off", "blocked_on", "escalated"}
MAX_TEXT = 20_000
LEASE_SECONDS = 30 * 60


def _now() -> float:
    return time.time()


class Mailbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY, team TEXT NOT NULL, seq INTEGER NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL,
                    created REAL NOT NULL, delivered REAL);
                CREATE INDEX IF NOT EXISTS messages_by_team ON messages(team, seq);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, team TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
                    owner TEXT NOT NULL, state TEXT NOT NULL, closure_reason TEXT, closure_target TEXT,
                    created_by TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS task_transitions(
                    task TEXT NOT NULL, team TEXT NOT NULL, at REAL NOT NULL, by TEXT NOT NULL,
                    from_state TEXT, to_state TEXT NOT NULL, reason TEXT, target TEXT, note TEXT);
                CREATE TABLE IF NOT EXISTS leases(
                    team TEXT NOT NULL, pattern TEXT NOT NULL, holder TEXT NOT NULL,
                    expires REAL NOT NULL, PRIMARY KEY(team, pattern));
                CREATE TABLE IF NOT EXISTS questions(
                    id TEXT PRIMARY KEY, team TEXT NOT NULL, asker TEXT NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, answer TEXT, created REAL NOT NULL, answered REAL);
                CREATE TABLE IF NOT EXISTS results(
                    team TEXT NOT NULL, agent TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
                    created REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            held = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            if int(held) > SCHEMA_VERSION:
                raise HarnessError("This team store was written by a newer Nexus. Update Nexus to open it.")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    # -- messages

    def send(self, team: str, sender: str, recipient: str, text: str) -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise HarnessError("A message needs some text.")
        with self._db() as db:
            seq = (db.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE team=?", (team,)).fetchone()[0]) + 1
            row = {"id": uuid.uuid4().hex, "team": team, "seq": seq, "sender": sender,
                   "recipient": str(recipient or "*"), "text": text[:MAX_TEXT], "created": _now(), "delivered": None}
            db.execute("INSERT INTO messages VALUES(:id,:team,:seq,:sender,:recipient,:text,:created,:delivered)", row)
        return row

    def undelivered(self, team: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(one) for one in db.execute(
                "SELECT * FROM messages WHERE team=? AND delivered IS NULL ORDER BY seq", (team,))]

    def mark_delivered(self, message_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE messages SET delivered=? WHERE id=?", (_now(), message_id))

    def messages(self, team: str, *, for_agent: str = "", after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._db() as db:
            if for_agent:
                rows = db.execute("SELECT * FROM messages WHERE team=? AND seq>? AND (recipient=? OR recipient='*' "
                                  "OR sender=?) ORDER BY seq LIMIT ?", (team, after, for_agent, for_agent, limit))
            else:
                rows = db.execute("SELECT * FROM messages WHERE team=? AND seq>? ORDER BY seq LIMIT ?",
                                  (team, after, limit))
            return [dict(one) for one in rows]

    # -- tasks

    def create_task(self, team: str, by: str, title: str, detail: str = "", owner: str = "") -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise HarnessError("A task needs a title.")
        now = _now()
        row = {"id": "task-" + uuid.uuid4().hex[:10], "team": team, "title": title[:300], "detail": str(detail)[:MAX_TEXT],
               "owner": str(owner or ""), "state": "claimed" if owner else "open", "closure_reason": None,
               "closure_target": None, "created_by": by, "created": now, "updated": now}
        with self._db() as db:
            db.execute("INSERT INTO tasks VALUES(:id,:team,:title,:detail,:owner,:state,:closure_reason,"
                       ":closure_target,:created_by,:created,:updated)", row)
            self._transition(db, row["id"], team, by, None, row["state"], None, None, "created")
        return row

    def update_task(self, team: str, by: str, task_id: str, state: str, *, reason: str = "", target: str = "",
                    note: str = "", owner: str | None = None) -> dict[str, Any]:
        if state not in TASK_STATES:
            raise HarnessError(f"Unknown task state {state!r}. Use one of: {', '.join(TASK_STATES)}.")
        closing = state in ("done", "cancelled")
        if closing:
            if reason not in CLOSURE_REASONS:
                raise HarnessError("Closing a task needs a closure_reason: " + ", ".join(CLOSURE_REASONS) + ".")
            if reason in NEEDS_TARGET and not str(target or "").strip():
                raise HarnessError(f"closure_reason {reason} needs a target: who or what the work went to.")
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND team=?", (task_id, team)).fetchone()
            if row is None:
                raise HarnessError(f"There is no task {task_id} in this team.")
            new_owner = row["owner"] if owner is None else str(owner)
            if state == "claimed" and not new_owner:
                new_owner = by
            db.execute("UPDATE tasks SET state=?, owner=?, closure_reason=?, closure_target=?, updated=? WHERE id=?",
                       (state, new_owner, reason if closing else None, target if closing else None, _now(), task_id))
            self._transition(db, task_id, team, by, row["state"], state, reason or None, target or None, note or None)
            return dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def _transition(self, db, task_id, team, by, old, new, reason, target, note) -> None:
        db.execute("INSERT INTO task_transitions VALUES(?,?,?,?,?,?,?,?,?)",
                   (task_id, team, _now(), by, old, new, reason, target, note))

    def tasks(self, team: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(one) for one in db.execute("SELECT * FROM tasks WHERE team=? ORDER BY created", (team,))]

    def transitions(self, team: str, task_id: str = "") -> list[dict[str, Any]]:
        with self._db() as db:
            if task_id:
                rows = db.execute("SELECT * FROM task_transitions WHERE team=? AND task=? ORDER BY at", (team, task_id))
            else:
                rows = db.execute("SELECT * FROM task_transitions WHERE team=? ORDER BY at", (team,))
            return [dict(one) for one in rows]

    def stuck(self, team: str, idle_seconds: float) -> list[dict[str, Any]]:
        """Open work nobody has touched for a while. Reported, never acted on."""

        cutoff = _now() - idle_seconds
        with self._db() as db:
            return [dict(one) for one in db.execute(
                "SELECT * FROM tasks WHERE team=? AND state IN ('open','claimed','in_progress','blocked') "
                "AND updated<? ORDER BY updated", (team, cutoff))]

    # -- file leases (advisory: they warn, they never block an agent)

    def reserve(self, team: str, holder: str, patterns: list[str], seconds: float = LEASE_SECONDS) -> dict[str, Any]:
        now = _now()
        granted, conflicts = [], []
        with self._db() as db:
            db.execute("DELETE FROM leases WHERE expires<?", (now,))
            held = [dict(one) for one in db.execute("SELECT * FROM leases WHERE team=?", (team,))]
            for pattern in [str(one).strip().replace("\\", "/") for one in patterns if str(one).strip()][:50]:
                clash = [one for one in held if one["holder"] != holder and (
                    fnmatch.fnmatch(pattern, one["pattern"]) or fnmatch.fnmatch(one["pattern"], pattern)
                    or pattern == one["pattern"])]
                if clash:
                    conflicts.append({"pattern": pattern, "held_by": clash[0]["holder"]})
                db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?,?)", (team, pattern, holder, now + seconds))
                granted.append(pattern)
        return {"granted": granted, "conflicts": conflicts}

    def release(self, team: str, holder: str, patterns: list[str] | None = None) -> int:
        with self._db() as db:
            if patterns:
                count = 0
                for pattern in patterns:
                    count += db.execute("DELETE FROM leases WHERE team=? AND holder=? AND pattern=?",
                                        (team, holder, str(pattern).replace("\\", "/"))).rowcount
                return count
            return db.execute("DELETE FROM leases WHERE team=? AND holder=?", (team, holder)).rowcount

    def leases(self, team: str) -> list[dict[str, Any]]:
        with self._db() as db:
            db.execute("DELETE FROM leases WHERE expires<?", (_now(),))
            return [dict(one) for one in db.execute("SELECT * FROM leases WHERE team=? ORDER BY pattern", (team,))]

    # -- questions for the user (ask_user, approvals) and final results

    def ask(self, team: str, asker: str, kind: str, text: str) -> str:
        identity = uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?)",
                       (identity, team, asker, kind, str(text)[:MAX_TEXT], None, _now(), None))
        return identity

    def answer(self, question_id: str, answer: str) -> None:
        with self._db() as db:
            changed = db.execute("UPDATE questions SET answer=?, answered=? WHERE id=? AND answer IS NULL",
                                 (str(answer)[:MAX_TEXT], _now(), question_id)).rowcount
            if not changed:
                raise HarnessError("That question was already answered or does not exist.")

    def question(self, question_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
            return dict(row) if row else None

    def open_questions(self, team: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(one) for one in db.execute(
                "SELECT * FROM questions WHERE team=? AND answer IS NULL ORDER BY created", (team,))]

    def report(self, team: str, agent: str, status: str, summary: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO results VALUES(?,?,?,?,?)", (team, agent, str(status)[:40], str(summary)[:MAX_TEXT], _now()))

    def results(self, team: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(one) for one in db.execute("SELECT * FROM results WHERE team=? ORDER BY created", (team,))]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
