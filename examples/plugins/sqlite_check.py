"""An example plugin: a check kind that asks a SQLite file a question.

This is a whole plugin in one file. It shows the three parts every plugin has:
a check kind that says what its cases look like, a function that runs one, and
a `plugin()` function the harness calls to register it.

To use it, put this file somewhere in your project and add both lines to your
own trusted local config, `.harness/config.local.json`:

    {"plugins": {"enabled": ["sqlite_check"], "paths": ["tools/sqlite_check.py"]}}

The name in `enabled` is the file name without `.py`. Then write a case:

    {
      "id": "one-admin",
      "title": "There is exactly one admin account",
      "kind": "sqlite",
      "database": "data/app.db",
      "query": "SELECT count(*) FROM users WHERE role = 'admin'",
      "expect": {"rows": 1, "first_value": "1"}
    }
"""

from __future__ import annotations

import sqlite3

from our_harness.qa import CheckKind, QaError, QaSkipped
from our_harness.safety import confined_path


def run_sqlite_check(case, runner):
    """Run one query and compare what came back with what the case expects.

    Returns (reasons, short evidence, full evidence). No reasons means it passed.
    """

    database = str(case.field("database", ""))
    query = str(case.field("query", ""))
    if not database or not query:
        raise QaError("A sqlite case needs both a database and a query")
    if not query.lstrip().upper().startswith("SELECT"):
        # A check reads. It never changes the thing it is checking.
        raise QaError("A sqlite case may only run a SELECT query")

    path = confined_path(runner.root, database, allow_missing=True)
    if not path.is_file():
        raise QaSkipped(f"There is no database at {database} yet, so this check cannot run.")

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise QaError(f"Cannot open {database}: {exc}") from exc
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(query).fetchmany(100)
    except sqlite3.Error as exc:
        raise QaError(f"The query did not run: {exc}") from exc
    finally:
        connection.close()

    reasons = []
    expected_rows = case.expect_extra("rows")
    if expected_rows is not None and len(rows) != int(expected_rows):
        reasons.append(f"The query returned {len(rows)} rows; the case expects {expected_rows}")
    expected_first = case.expect_extra("first_value")
    if expected_first is not None:
        found = str(rows[0][0]) if rows and rows[0] else ""
        if found != str(expected_first):
            reasons.append(
                f"The first value is \"{found}\"; the case expects \"{expected_first}\""
            )
    evidence = "\n".join(str(row) for row in rows[:20]) or "no rows"
    return tuple(reasons), evidence, f"{query}\n\n{evidence}"


SQLITE_CHECK = CheckKind(
    name="sqlite",
    summary="Ask a SQLite file a read-only question and check the answer.",
    fields=frozenset({"database", "query"}),
    expectations=frozenset({"rows", "first_value"}),
    run=run_sqlite_check,
)


class SqliteCheckPlugin:
    name = "sqlite_check"

    def register(self, registry) -> None:
        registry.add_check_kind(SQLITE_CHECK)


def plugin() -> SqliteCheckPlugin:
    return SqliteCheckPlugin()
