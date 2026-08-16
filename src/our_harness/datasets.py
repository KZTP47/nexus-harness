"""Tables of values, named settings, and putting them into a check.

Two features share one idea. A check can run once for every row of a table, so
one written check covers twenty logins. And a project can keep named settings,
so the same checks run against your machine or against a test server without
being edited.

Both feed the same substitution: `${row.username}` takes a value from the
current row, `${env.BASE_URL}` takes one from the chosen settings.

The rules here are deliberately strict, because the older tool this replaces
was bitten by every one of the loose ones:

- Replacement is literal. Nothing built from a user's text is ever compiled as
  a pattern, so a column called `total (net)` cannot break anything.
- A name that has no value is an error that says which name, rather than
  leaving `${something}` in the command and failing later for a strange reason.
- Values stay text. Nothing is quietly turned into a number.
- A value only ever lands in one argument, one path, or one piece of text. There
  is no shell, so a value holding a quote or a semicolon is just a value.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path

ENVIRONMENTS_FILE = ".harness/qa/environments.json"
MAX_ROWS = 1000
MAX_COLUMNS = 100
MAX_VALUE_CHARS = 10_000
MAX_NAME_CHARS = 64
MAX_ENVIRONMENTS = 50

# What a column or setting may be called. Real spreadsheet headers hold spaces,
# dots, dashes, brackets and slashes, so those are allowed. The braces and the
# dollar sign are not, because they are what marks a placeholder.
_NAME_BODY = r"[A-Za-z0-9_][A-Za-z0-9_ .\-()/]{0,63}"
# ${row.name} and ${env.NAME}. Nothing else is a placeholder. The two patterns
# are built from one piece, so anything you may call a column you may also ask
# for by name.
_PLACEHOLDER = re.compile(r"\$\{(row|env)\.(" + _NAME_BODY + r")\}")
_COLUMN_PATTERN = re.compile("^" + _NAME_BODY + "$")


class DataError(HarnessError):
    """A table or settings problem the user can fix."""


@dataclass(frozen=True)
class Row:
    number: int
    label: str
    values: tuple[tuple[str, str], ...]

    def mapping(self) -> dict[str, str]:
        return {name: value for name, value in self.values}

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "label": self.label, "values": self.mapping()}


def _text(value: object, label: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise DataError(f"{label} must be text, a number, true, false, or empty")
    if len(value) > MAX_VALUE_CHARS:
        raise DataError(f"{label} must be at most {MAX_VALUE_CHARS} characters")
    return value


def _column(name: object, label: str) -> str:
    if not isinstance(name, str) or not _COLUMN_PATTERN.fullmatch(name.strip()):
        raise DataError(
            f"{label} must be letters, digits, or any of _ . - ( ) / and spaces, "
            f"start with a letter, digit or underscore, and be at most {MAX_NAME_CHARS} characters"
        )
    return name.strip()


def rows_from_list(value: object, label: str = "rows") -> tuple[Row, ...]:
    """A table written straight into the suite file."""

    if not isinstance(value, list):
        raise DataError(f"{label} must be a list of rows")
    if not value:
        raise DataError(f"{label} must hold at least one row")
    if len(value) > MAX_ROWS:
        raise DataError(f"{label} must hold at most {MAX_ROWS} rows")
    built: list[Row] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise DataError(f"{label} row {index} must be an object of column names and values")
        if len(item) > MAX_COLUMNS:
            raise DataError(f"{label} row {index} holds more than {MAX_COLUMNS} columns")
        pairs = tuple(
            (_column(name, f"{label} row {index} column name"), _text(item[name], f"{label} row {index}.{name}"))
            for name in item
        )
        built.append(Row(number=index, label=_row_label(index, pairs), values=pairs))
    return tuple(built)


def _row_label(number: int, values: Sequence[tuple[str, str]]) -> str:
    """A short name for one row, so a report says which row failed."""

    for name, value in values:
        if name.casefold() in ("label", "name", "case", "id") and value.strip():
            return value.strip()[:40]
    if values and values[0][1].strip():
        return values[0][1].strip()[:40]
    return f"row {number}"


def rows_from_csv(text: str, label: str = "the table") -> tuple[Row, ...]:
    """Read a comma separated table properly, quotes and line breaks included."""

    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        records = [record for record in reader]
    except csv.Error as exc:
        raise DataError(f"{label} could not be read as a comma separated table: {exc}") from exc
    records = [record for record in records if any(cell.strip() for cell in record)]
    if len(records) < 2:
        raise DataError(f"{label} needs a header line and at least one row")
    headers = [_column(name, f"{label} column name") for name in records[0]]
    if len(headers) != len(set(headers)):
        raise DataError(f"{label} uses the same column name twice")
    if len(headers) > MAX_COLUMNS:
        raise DataError(f"{label} holds more than {MAX_COLUMNS} columns")
    if len(records) - 1 > MAX_ROWS:
        raise DataError(f"{label} holds more than {MAX_ROWS} rows")
    built: list[Row] = []
    for index, record in enumerate(records[1:], start=1):
        if len(record) != len(headers):
            raise DataError(
                f"{label} row {index} has {len(record)} values but the header names {len(headers)} columns"
            )
        pairs = tuple(
            (name, _text(value, f"{label} row {index}.{name}"))
            for name, value in zip(headers, record)
        )
        built.append(Row(number=index, label=_row_label(index, pairs), values=pairs))
    return tuple(built)


def rows_from_json(text: str, label: str = "the table") -> tuple[Row, ...]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError(f"{label} is not valid JSON: {exc.msg}") from exc
    if isinstance(value, Mapping):
        found = value.get("rows", value.get("data"))
        if found is None:
            raise DataError(f"{label} must hold a list, or an object with a rows list")
        value = found
    return rows_from_list(value, label)


def read_rows(config: LoadedConfig, relative: str) -> tuple[Row, ...]:
    """Read a table from a file in the project."""

    if re.split(r"[\\/]", relative)[0].lower() == ".git":
        raise DataError("A table may not be read from inside the .git folder")
    path = confined_path(config.project_root, relative, allow_missing=True, allow_control=True)
    if not path.is_file():
        raise DataError(f"There is no table at {relative}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DataError(f"Cannot read {relative}: {exc}") from exc
    if path.suffix.lower() == ".json":
        return rows_from_json(text, relative)
    return rows_from_csv(text, relative)


# ---------------------------------------------------------------------------
# Named settings
# ---------------------------------------------------------------------------


def environments_path(config: LoadedConfig) -> Path:
    return confined_path(config.project_root, ENVIRONMENTS_FILE, allow_missing=True, allow_control=True)


def parse_environments(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise DataError("Settings must be an object of names to value sets")
    body = value.get("environments", value)
    if not isinstance(body, Mapping):
        raise DataError("Settings must hold an environments object")
    if len(body) > MAX_ENVIRONMENTS:
        raise DataError(f"There may be at most {MAX_ENVIRONMENTS} named settings")
    built: dict[str, dict[str, str]] = {}
    for name, values in body.items():
        clean = _column(name, "A settings name")
        if not isinstance(values, Mapping):
            raise DataError(f"The settings named {clean} must be an object of names and values")
        if len(values) > MAX_COLUMNS:
            raise DataError(f"The settings named {clean} hold more than {MAX_COLUMNS} values")
        built[clean] = {
            _column(key, f"{clean} value name"): _text(item, f"{clean}.{key}")
            for key, item in values.items()
        }
    return built


def load_environments(config: LoadedConfig) -> dict[str, dict[str, str]]:
    path = environments_path(config)
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"Cannot read the settings file: {exc}") from exc
    return parse_environments(body)


def save_environments(config: LoadedConfig, environments: Mapping[str, Mapping[str, str]]) -> Path:
    checked = parse_environments({"environments": environments})
    path = environments_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "environments": checked}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def chosen_environment(
    config: LoadedConfig, name: str = ""
) -> tuple[str, dict[str, str]]:
    """The settings to use, and their name. An unknown name is refused."""

    known = load_environments(config)
    if not name:
        return "", {}
    if name not in known:
        listed = ", ".join(sorted(known)) or "none are saved"
        raise DataError(f"There are no settings named {name}. Saved ones: {listed}")
    return name, dict(known[name])


# ---------------------------------------------------------------------------
# Putting values into text
# ---------------------------------------------------------------------------


def names_used(text: str) -> tuple[tuple[str, str], ...]:
    """Every ${row.x} and ${env.x} in one piece of text."""

    if not isinstance(text, str):
        return ()
    return tuple((match.group(1), match.group(2)) for match in _PLACEHOLDER.finditer(text))


def fill(text: str, row: Mapping[str, str] | None, environment: Mapping[str, str] | None, where: str) -> str:
    """Put the row and settings values into one piece of text.

    A name with no value is an error naming that name, because a command still
    holding ${row.username} would fail later for a reason nobody could read.
    """

    if not isinstance(text, str) or "${" not in text:
        return text
    row = row or {}
    environment = environment or {}

    def swap(match: re.Match[str]) -> str:
        source, name = match.group(1), match.group(2)
        values = row if source == "row" else environment
        if name in values:
            return values[name]
        known = ", ".join(sorted(values)) or "none"
        kind = "column" if source == "row" else "setting"
        raise DataError(
            f"{where} asks for the {kind} named {name}, which has no value here. "
            f"Available: {known}"
        )

    return _PLACEHOLDER.sub(swap, text)


def fill_value(value: Any, row: Mapping[str, str] | None, environment: Mapping[str, str] | None, where: str) -> Any:
    """Fill in every piece of text inside a value, however deeply it sits."""

    if isinstance(value, str):
        return fill(value, row, environment, where)
    if isinstance(value, list):
        return [fill_value(item, row, environment, f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, tuple):
        return tuple(fill_value(item, row, environment, where) for item in value)
    if isinstance(value, dict):
        return {key: fill_value(item, row, environment, f"{where}.{key}") for key, item in value.items()}
    return value


def describe_row(row: Row, limit: int = 3) -> str:
    """One short line naming what makes this row different."""

    shown = [f"{name}={value}" for name, value in row.values[:limit] if value != ""]
    extra = max(0, len(row.values) - limit)
    text = ", ".join(shown)
    return f"{text}{f' and {extra} more' if extra else ''}" or row.label
