"""Local reusable user prompts, independently versioned from agent instructions."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

from .filesystem_paths import filesystem_path
from .models import HarnessError
from .safety import confined_path

CONTRACT = "user-prompt-library/v1-exact-text-revision"
FINGERPRINT = hashlib.sha256(CONTRACT.encode()).hexdigest()
MAX_TEXT = 100_000
MAX_PROMPTS = 1000


@contextmanager
def _database(config):
    target = confined_path(config.project_root, ".harness/prompt-library.sqlite3", allow_missing=True, allow_control=True)
    filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(filesystem_path(target)), timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS library_contract (version INTEGER, fingerprint TEXT)")
        saved = db.execute("SELECT * FROM library_contract").fetchall()
        if not saved:
            db.execute("INSERT INTO library_contract VALUES (1, ?)", (FINGERPRINT,))
        elif len(saved) != 1 or saved[0]["version"] != 1 or saved[0]["fingerprint"] != FINGERPRINT:
            raise HarnessError("This prompt library needs a compatible Nexus version. Its prompts have been kept.")
        db.execute("CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL, updated_ms INTEGER NOT NULL)")
        yield db


def listing(config):
    with _database(config) as db:
        return {"schema_version": 1, "prompts": [dict(row) for row in db.execute(
            "SELECT * FROM prompts ORDER BY updated_ms DESC, id")]}


def update(config, payload):
    action = payload.get("action")
    if action not in {"save", "delete"}:
        raise HarnessError("Choose save or delete for a library prompt")
    identity = payload.get("id") or ""
    if not isinstance(identity, str) or len(identity) > 80:
        raise HarnessError("Invalid library prompt identity")
    if action == "save":
        title, body = payload.get("title"), payload.get("body")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise HarnessError("Give the prompt a title of 1 to 160 characters")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_TEXT:
            raise HarnessError("Prompts support 1 to 100,000 characters. Nothing was truncated.")
    with _database(config) as db:
        previous = db.execute("SELECT * FROM prompts WHERE id=?", (identity,)).fetchone() if identity else None
        if identity and (previous is None or type(payload.get("revision")) is not int
                         or payload["revision"] != previous["revision"]):
            raise HarnessError("This saved prompt changed in another window. Reopen it before saving or deleting.")
        if action == "delete":
            if not previous:
                raise HarnessError("Choose a saved prompt to delete")
            db.execute("DELETE FROM prompts WHERE id=?", (identity,))
            return {"deleted": identity}
        if previous is None and db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] >= MAX_PROMPTS:
            raise HarnessError("The library contains 1,000 prompts. Delete an unused prompt before adding another.")
        prompt = {"id": identity or uuid.uuid4().hex, "title": title.strip(), "body": body,
                  "revision": previous["revision"] + 1 if previous else 1, "updated_ms": time.time_ns() // 1_000_000}
        db.execute("INSERT OR REPLACE INTO prompts VALUES (:id,:title,:body,:revision,:updated_ms)", prompt)
        return {"prompt": prompt}
