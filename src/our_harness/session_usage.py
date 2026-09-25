"""How much each session used: provider calls and token counts, counted once.

A session is a goal ("goal:<id>") or a board chat ("chat:<id>"). Every physical
provider call is recorded under its own id, so recording the same call again
(a retry of the bookkeeping, a restart) never counts it twice. Token numbers
are what the provider reported; calls without numbers are counted separately
rather than guessed. Subscription plans do not bill per token, so these are
usage figures, not costs.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
import time
import uuid
from pathlib import Path
from typing import Any

CONTRACT = "nexus-session-usage/v1"
_LOCK = threading.Lock()


def _where() -> Path:
    from .swarm_runs import _base
    return _base().parent / "session-usage.sqlite3"


def _connect() -> sqlite3.Connection:
    where = _where()
    where.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(where), timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS session_usage(
        session TEXT NOT NULL, call_id TEXT NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, at_ms INTEGER NOT NULL,
        PRIMARY KEY(session, call_id))""")
    return db


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def record(session: str, *, route: str, model: str, response: Any = None, call_id: str = "") -> None:
    """Count one provider call for ``session``; never raises into the caller."""
    session = str(session or "").strip()
    if not session:
        return
    try:
        # closing(): a sqlite3 "with" only commits; an open file blocks cleanup on Windows.
        with _LOCK, closing(_connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO session_usage VALUES(?,?,?,?,?,?,?,?)", (
                session[:300], str(call_id or uuid.uuid4().hex)[:160], str(route or "")[:200],
                str(model or "")[:200], _count(getattr(response, "input_tokens", None)),
                _count(getattr(response, "output_tokens", None)),
                _count(getattr(response, "cached_input_tokens", None)), int(time.time() * 1000)))
    except (OSError, sqlite3.Error):
        return


def summary(session: str) -> dict[str, Any]:
    """Totals for one session, by model, with calls that reported no numbers."""
    empty = {"contract": CONTRACT, "session": session, "calls": 0, "input_tokens": 0, "output_tokens": 0,
             "cached_input_tokens": 0, "calls_without_token_counts": 0, "by_model": []}
    try:
        with _LOCK, closing(_connect()) as db:
            rows = db.execute("""SELECT model, COUNT(*), SUM(COALESCE(input_tokens,0)), SUM(COALESCE(output_tokens,0)),
                SUM(COALESCE(cached_input_tokens,0)), SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL THEN 1 ELSE 0 END)
                FROM session_usage WHERE session=? GROUP BY model ORDER BY model""", (str(session),)).fetchall()
    except (OSError, sqlite3.Error):
        return empty
    result = dict(empty)
    result["by_model"] = []
    for model, calls, given, produced, cached, unknown in rows:
        result["calls"] += calls
        result["input_tokens"] += given or 0
        result["output_tokens"] += produced or 0
        result["cached_input_tokens"] += cached or 0
        result["calls_without_token_counts"] += unknown or 0
        result["by_model"].append({"model": model, "calls": calls, "input_tokens": given or 0,
                                   "output_tokens": produced or 0})
    return result
