"""Private SQLite retrieval index for the project-bound Obsidian vault.

Markdown remains canonical.  This database is disposable generated context:
FTS5 answers which notes are relevant, while the small KV table records hook
health and freshness without turning transient state into Obsidian prose.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterator

from .models import HarnessError


INDEX_FOLDER = ".nexus-memory"
INDEX_DATABASE = "memory-index.sqlite3"
INDEX_SCHEMA_VERSION = 1
MAX_CHUNK_CHARS = 4_000
EXCLUDED_FOLDERS = {".obsidian", INDEX_FOLDER}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or bool(is_junction and is_junction()):
        return True
    # pathlib.Path.is_junction was added after the bundled Python 3.11
    # runtime. On Windows, junctions and other directory/file reparse points
    # must still fail closed before resolve() follows them outside the vault.
    if os.name == "nt":
        try:
            attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
        except (FileNotFoundError, OSError, ValueError):
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def chunk_markdown(text: str) -> list[tuple[str, int, str]]:
    """Split Markdown by second-level headings into bounded searchable chunks."""

    lines = text.splitlines()
    title = ""
    heading = ""
    start_line = 1
    buffer: list[str] = []
    chunks: list[tuple[str, int, str]] = []

    def flush() -> None:
        nonlocal start_line
        body = "\n".join(buffer).strip()
        if not body:
            return
        label = heading or title
        while len(body) > MAX_CHUNK_CHARS:
            cut = body.rfind("\n", 0, MAX_CHUNK_CHARS)
            if cut < MAX_CHUNK_CHARS // 2:
                cut = MAX_CHUNK_CHARS
            piece = body[:cut]
            chunks.append((label, start_line, piece))
            start_line += piece.count("\n") + 1
            body = body[cut:].lstrip("\n")
        if body:
            chunks.append((label, start_line, body))

    for line_number, line in enumerate(lines, 1):
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            start_line = line_number
            buffer = [line]
        else:
            buffer.append(line)
    flush()
    return chunks


class VaultMemoryIndex:
    """Incremental FTS5 + KV index confined to one already-bound vault."""

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root.resolve(strict=True)
        self.index_root = self.vault_root / INDEX_FOLDER
        self.database_path = self.index_root / INDEX_DATABASE

    def _validate_index_root(self) -> None:
        if self.index_root.exists() and _is_link_or_junction(self.index_root):
            raise HarnessError("Persistent-memory index folder must not be a link or junction")
        self.index_root.mkdir(parents=True, exist_ok=True)
        resolved = self.index_root.resolve(strict=True)
        if not _inside(resolved, self.vault_root):
            raise HarnessError("Persistent-memory index escaped the bound vault")
        if self.database_path.exists() and _is_link_or_junction(self.database_path):
            raise HarnessError("Persistent-memory index database must not be a link or junction")

    def _connect(self) -> sqlite3.Connection:
        self._validate_index_root()
        try:
            database = sqlite3.connect(self.database_path)
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA busy_timeout = 30000")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS files(
                    path TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks(
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    heading TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    body,
                    heading,
                    path UNINDEXED,
                    content='chunks',
                    content_rowid='id'
                );
                CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                );
                """
            )
            return database
        except sqlite3.Error as exc:
            raise HarnessError(f"Persistent-memory SQLite index is unavailable: {exc}") from exc

    def _iter_markdown_files(self) -> Iterator[tuple[str, Path]]:
        for directory, directory_names, file_names in os.walk(self.vault_root, followlinks=False):
            current = Path(directory).resolve(strict=True)
            if not _inside(current, self.vault_root):
                raise HarnessError("Persistent-memory note directory escaped the bound vault")
            kept: list[str] = []
            for name in directory_names:
                child = Path(directory) / name
                if name in EXCLUDED_FOLDERS:
                    continue
                if _is_link_or_junction(child):
                    raise HarnessError(
                        f"Persistent-memory note directory must not be a link or junction: {child}"
                    )
                kept.append(name)
            directory_names[:] = kept
            for name in file_names:
                if not name.casefold().endswith(".md"):
                    continue
                raw_path = Path(directory) / name
                if _is_link_or_junction(raw_path):
                    raise HarnessError(
                        f"Persistent-memory note must not be a link or junction: {raw_path}"
                    )
                path = raw_path.resolve(strict=True)
                if not _inside(path, self.vault_root) or not path.is_file():
                    raise HarnessError("Persistent-memory note escaped the bound vault")
                yield path.relative_to(self.vault_root).as_posix(), path

    @staticmethod
    def _remove_file(database: sqlite3.Connection, relative: str) -> None:
        rows = database.execute(
            "SELECT id, body, heading, path FROM chunks WHERE path = ?", (relative,)
        ).fetchall()
        for row in rows:
            database.execute(
                "INSERT INTO chunks_fts(chunks_fts, rowid, body, heading, path) "
                "VALUES('delete', ?, ?, ?, ?)",
                (row["id"], row["body"], row["heading"], row["path"]),
            )
        database.execute("DELETE FROM chunks WHERE path = ?", (relative,))
        database.execute("DELETE FROM files WHERE path = ?", (relative,))

    @staticmethod
    def _set_kv(database: sqlite3.Connection, key: str, value: Any) -> None:
        database.execute(
            "INSERT INTO kv(key, value_json, updated_utc) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_utc=excluded.updated_utc",
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True), _utc_now()),
        )

    def set_kv(self, key: str, value: Any) -> None:
        with closing(self._connect()) as database:
            self._set_kv(database, key, value)
            database.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        with closing(self._connect()) as database:
            row = database.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value_json"]))
        except json.JSONDecodeError:
            return default

    def refresh(self, *, rebuilt: bool = False) -> dict[str, Any]:
        """Incrementally synchronize the private index with canonical Markdown."""

        try:
            with closing(self._connect()) as database:
                database.execute("BEGIN IMMEDIATE")
                if rebuilt:
                    database.executescript("DELETE FROM chunks; DELETE FROM files;")
                    database.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                seen: set[str] = set()
                changed = 0
                for relative, path in self._iter_markdown_files():
                    seen.add(relative)
                    stat = path.stat()
                    row = database.execute(
                        "SELECT mtime_ns, size FROM files WHERE path = ?", (relative,)
                    ).fetchone()
                    if row and row["mtime_ns"] == stat.st_mtime_ns and row["size"] == stat.st_size:
                        continue
                    self._remove_file(database, relative)
                    text = path.read_text(encoding="utf-8")
                    for heading, line, body in chunk_markdown(text):
                        cursor = database.execute(
                            "INSERT INTO chunks(path, heading, line, body) VALUES(?, ?, ?, ?)",
                            (relative, heading, line, body),
                        )
                        database.execute(
                            "INSERT INTO chunks_fts(rowid, body, heading, path) VALUES(?, ?, ?, ?)",
                            (cursor.lastrowid, body, heading, relative),
                        )
                    database.execute(
                        "INSERT INTO files(path, mtime_ns, size) VALUES(?, ?, ?)",
                        (relative, stat.st_mtime_ns, stat.st_size),
                    )
                    changed += 1
                indexed = {
                    str(row["path"])
                    for row in database.execute("SELECT path FROM files").fetchall()
                }
                for relative in indexed - seen:
                    self._remove_file(database, relative)
                    changed += 1
                files = int(database.execute("SELECT COUNT(*) FROM files").fetchone()[0])
                chunks = int(database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                status = {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "files": files,
                    "chunks": chunks,
                    "changed_files": changed,
                    "refreshed_utc": _utc_now(),
                    "database": f"{INDEX_FOLDER}/{INDEX_DATABASE}",
                }
                self._set_kv(database, "index.status", status)
                database.commit()
                return status
        except (OSError, UnicodeError, sqlite3.Error) as exc:
            raise HarnessError(f"Persistent-memory index refresh failed: {exc}") from exc

    @staticmethod
    def _fts_query(query: str) -> str:
        ignored = {
            "and", "are", "for", "from", "into", "our", "that", "the", "this", "to", "with",
        }
        # Python's Unicode-aware ``\w`` keeps natural project vocabulary such
        # as Swedish "Åtgärd" searchable instead of silently reducing it to an
        # ASCII suffix. Underscore-only prefixes are excluded while dots and
        # hyphens remain useful for symbols and filenames.
        tokens = [
            token
            for token in re.findall(r"[^\W_][\w.-]*", query.casefold(), flags=re.UNICODE)
            if len(token) >= 2 and token not in ignored
        ]
        unique = list(dict.fromkeys(tokens))[:24]
        return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in unique)

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise HarnessError("Persistent-memory search limit must be between 1 and 100")
        fts_query = self._fts_query(query)
        if not fts_query:
            return []
        sql = (
            "SELECT c.path, c.heading, c.line, "
            "snippet(chunks_fts, 0, '[', ']', ' ... ', 24) AS snippet, "
            "bm25(chunks_fts) AS rank "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        try:
            with closing(self._connect()) as database:
                rows = database.execute(sql, (fts_query, limit)).fetchall()
        except sqlite3.Error as exc:
            raise HarnessError(f"Persistent-memory search failed: {exc}") from exc
        return [
            {
                "path": str(row["path"]),
                "heading": str(row["heading"]),
                "line": int(row["line"]),
                "snippet": " ".join(str(row["snippet"]).split()),
                "score": round(-float(row["rank"]), 4),
            }
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as database:
            files = int(database.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            chunks = int(database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            keys = {
                str(row["key"]): json.loads(str(row["value_json"]))
                for row in database.execute(
                    "SELECT key, value_json FROM kv WHERE key IN "
                    "('hook.last_pre', 'hook.last_post', 'index.status')"
                ).fetchall()
            }
        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "database": f"{INDEX_FOLDER}/{INDEX_DATABASE}",
            "files": files,
            "chunks": chunks,
            "kv": keys,
        }

    def contains_paths(self, paths: list[str]) -> dict[str, bool]:
        normalized = list(dict.fromkeys(paths))
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with closing(self._connect()) as database:
            present = {
                str(row["path"])
                for row in database.execute(
                    f"SELECT path FROM files WHERE path IN ({placeholders})", normalized
                ).fetchall()
            }
        return {path: path in present for path in normalized}
