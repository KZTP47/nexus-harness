# Plugins

A plugin is one Python file that adds something to the harness. It can add a
kind of check, a way of recognising a project, a workflow node, or a health
check for `harness doctor`.

Nothing is loaded unless you name it in your own trusted local config. A file
sitting in the project does nothing on its own.

## Turning one on

Put both lines in `.harness/config.local.json`, which is your own file and is
never shared:

```json
{"plugins": {"enabled": ["sqlite_check"], "paths": ["tools/sqlite_check.py"]}}
```

The name in `enabled` is the file name without `.py`. A plugin named in `paths`
but not in `enabled` is ignored, so you can keep one around while it is off.

A plugin runs as you, with your permissions. Only turn on a file you have read.

## What a plugin looks like

```python
class MyPlugin:
    name = "my_plugin"

    def register(self, registry) -> None:
        registry.add_check_kind(MY_CHECK)


def plugin() -> MyPlugin:
    return MyPlugin()
```

The harness calls `plugin()` once, then `register(registry)`. The registry
takes four things:

| Call | What it adds |
|---|---|
| `registry.add_check_kind(kind)` | A kind of QA check, such as one that asks a database a question. |
| `registry.add_detector(detector)` | A way of recognising a project and its commands. |
| `registry.add_workflow_node(name, node)` | A node type for the workflow graph. |
| `registry.add_doctor_check(check)` | An extra line in `harness doctor`. |

## Adding a kind of check

A check kind says what its cases look like and how to run one.

```python
from our_harness.qa import CheckKind, QaError, QaSkipped

def run_my_check(case, runner):
    target = str(case.field("target", ""))
    if not target:
        raise QaError("A my-kind case needs a target")
    ...
    return (), "short evidence", "full evidence"

MY_CHECK = CheckKind(
    name="my-kind",
    summary="One line saying what this kind of check does.",
    fields=frozenset({"target"}),
    expectations=frozenset({"answer"}),
    run=run_my_check,
)
```

| Part | Meaning |
|---|---|
| `name` | Lowercase letters, digits, dash, or underscore. It may not be one of the built-in kinds. |
| `summary` | One line, for people reading your plugin. |
| `fields` | The extra case fields your kind understands. Anything else is still refused when the suite is read. |
| `expectations` | The keys your kind's `expect` block may hold. |
| `run(case, runner)` | Returns `(reasons, short evidence, full evidence)`. No reasons means it passed. |

Inside `run`:

- `case.field("name")` reads one of your declared fields.
- `case.expect_extra("name")` reads one of your declared expectations.
- `runner.root` is the project folder, and `runner.commands` is the same
  process runner the built-in command checks use, with the same limits.
- Raise `QaError` for a mistake the user can fix.
- Raise `QaSkipped` when the check cannot run here, for example when the tool it
  needs is not installed. A skipped check never fails the run.
- Anything else your code raises is caught, and only your case fails.

### Your plugin must stop itself

A command check runs in its own process, so the harness can time it out and kill
it. A plugin runs inside the harness, so nothing outside can stop it. A plugin
that waits forever holds up the whole run.

Bound your own work. The `timeout_seconds` on a case is yours to honour, through
whatever the library you are calling offers: a timeout argument, a deadline, or
a progress handler. The SQLite example asks SQLite to give up after a set amount
of work, so a recursive query cannot hang the run.

Your fields may not take a name the suite already owns, such as `id`, `title`,
`tags`, `retries`, or `timeout_seconds`. Two plugins may not add the same kind
name, and no plugin may replace a built-in kind. Every one of those mistakes is
refused as the suite is read, not halfway through a run.

### What a suite may put in your fields

A suite file is data, so a plugin field may hold text, a number, true, false,
null, or a flat list of those. Text is capped at 20,000 characters and a list at
100 values. A nested object, a list of lists, or a number that is not real is
refused when the suite is read.

This matters because a suite can be checked into a shared repository. Your
plugin is trusted code; the suite that drives it is not, so the harness bounds
what it can hand you.

## A working example

`examples/plugins/sqlite_check.py` is a complete plugin in one file. It adds a
`sqlite` check kind that runs one read-only query against a SQLite file:

```json
{
  "id": "one-admin",
  "title": "There is exactly one admin account",
  "kind": "sqlite",
  "database": "data/app.db",
  "query": "SELECT count(*) FROM users WHERE role = 'admin'",
  "expect": {"rows": 1, "first_value": "1"}
}
```

It refuses any query that is not a SELECT, opens the file read only, cannot
read outside the project, and reports a missing database as skipped rather than
failed. The tests in `tests/test_plugin_check_kinds.py` prove each of those.

## What the harness does with your kind

Once the plugin is on, your kind behaves exactly like a built-in one:

- `harness qa list` shows its cases.
- `harness qa run` runs them side by side with the rest, with the same retries,
  flaky marking, evidence files, and reports.
- `harness qa advise` counts them in its history.
- The control panel lists them in the Checks tab.
- `harness qa generate` may propose them, and the same accept step applies.

With the plugin off, a suite that uses its kind is refused with a message naming
the kinds that are known, so nothing runs by accident.
