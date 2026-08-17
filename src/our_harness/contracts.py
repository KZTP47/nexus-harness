"""Checking that an answer from a server has the shape it promised.

A contract here is an ordinary JSON Schema. This reads a useful part of that
standard and says, in plain words, exactly where an answer does not match:

    data.items[0].price must be a number, but it holds "12.50"

Two rules keep this honest.

First, a word in the schema that this tool does not understand is refused when
the schema is read, naming the word. The older tool this replaces understood
almost nothing and quietly ignored the rest, so a schema full of rules passed
everything. A checker that silently skips rules is worse than no checker,
because people trust it.

Second, nothing is fetched. A `$ref` may only point inside the same file. A
schema that points at a web address is refused, so reading a contract can never
reach the network.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import HarnessError

# Every word this tool understands. Anything else in a schema is refused.
KNOWN_WORDS = frozenset({
    "$ref", "$defs", "definitions", "$schema", "$id", "title", "description", "examples",
    "default", "deprecated", "readOnly", "writeOnly", "$comment",
    "type", "enum", "const",
    "properties", "required", "additionalProperties", "minProperties", "maxProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format",
    "anyOf", "allOf", "oneOf", "not", "nullable",
})
# Words that only describe the schema and change nothing about the answer.
_JUST_WORDS = frozenset({
    "$schema", "$id", "title", "description", "examples", "default", "deprecated",
    "readOnly", "writeOnly", "$comment", "$defs", "definitions",
})
TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")
FORMATS = ("date-time", "date", "time", "email", "uri", "uuid", "ipv4", "hostname")
MAX_DEPTH = 30
MAX_PATTERN_CHARS = 300
MAX_PROBLEMS = 50
# How much text a pattern is tried against. Python has no way to stop a pattern
# that takes forever, and a long answer is exactly what makes a careless pattern
# take forever, so a value longer than this is reported instead of tested.
MAX_PATTERN_INPUT = 20_000
# A repeated group that itself holds a repeat, such as (a+)+, or a choice, such
# as (a|aa)+. On the wrong text these take longer than the age of the universe,
# and there is no way to interrupt one, so the whole run would sit there doing
# nothing. Both shapes are rare in a real contract and easy to rewrite.
_REPEAT_IN_REPEAT = re.compile(
    r"\([^()]*(?:[*+|]|\{\d+,\d*\})[^()]*\)\s*(?:[*+]|\{\d+,\d*\})"
)

_FORMAT_PATTERNS = {
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "time": re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"),
    "date-time": re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?$"),
    "email": re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$"),
    "uri": re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S*$"),
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "ipv4": re.compile(r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"),
    "hostname": re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"),
}


class ContractError(HarnessError):
    """The contract itself is wrong, and the user can fix it."""


def _name(value: Any) -> str:
    """What kind of thing this is, in the words the schema uses."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _shown(value: Any, limit: int = 60) -> str:
    """A short, safe way of showing a value inside a sentence."""

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _same_value_mark(value: Any) -> str:
    """One piece of text that is the same for two values that are the same.

    Numbers need care: 1 and 1.0 are the same number written two ways, so a
    list holding both is holding the same item twice.
    """

    def tidy(item: Any) -> Any:
        if isinstance(item, bool):
            return item
        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, Mapping):
            return {key: tidy(inner) for key, inner in sorted(item.items())}
        if isinstance(item, (list, tuple)):
            return [tidy(inner) for inner in item]
        return item

    try:
        return json.dumps(tidy(value), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def check_schema(
    schema: object,
    where: str = "the contract",
    depth: int = 0,
    root: Mapping[str, Any] | None = None,
) -> None:
    """Read the contract itself and refuse anything this tool cannot enforce."""

    if depth > MAX_DEPTH:
        raise ContractError(f"{where} is nested deeper than {MAX_DEPTH} levels")
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise ContractError(f"{where} must be an object, or true or false")
    unknown = sorted(set(schema) - KNOWN_WORDS)
    if unknown:
        raise ContractError(
            f"{where} uses {unknown[0]}, which this tool cannot enforce. "
            f"Remove it, or the check would pass while that rule was ignored. "
            f"Words this tool understands: {', '.join(sorted(KNOWN_WORDS))}"
        )
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise ContractError(f"{where}.$ref must be text")
        if not reference.startswith("#/"):
            raise ContractError(
                f"{where}.$ref must point inside this same file, such as #/$defs/thing. "
                "Nothing is ever fetched."
            )
        whole = root if root is not None else (schema if isinstance(schema, Mapping) else {})
        landed = _resolve(reference, whole, where)
        if not isinstance(landed, Mapping) and not isinstance(landed, bool):
            raise ContractError(
                f"{where}.$ref points at {reference}, which is not a rule. "
                "A pointer must land on a rule, or nothing there would be checked."
            )
    kinds = schema.get("type")
    if kinds is not None:
        wanted = kinds if isinstance(kinds, list) else [kinds]
        for item in wanted:
            if item not in TYPES:
                raise ContractError(
                    f"{where}.type must be one of: {', '.join(TYPES)}. It says {_shown(item)}"
                )
    for word in ("enum",):
        if word in schema and not isinstance(schema[word], list):
            raise ContractError(f"{where}.{word} must be a list of allowed values")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ContractError(f"{where}.required must be a list of field names")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise ContractError(f"{where}.properties must be an object of field names")
        for field, rule in properties.items():
            check_schema(rule, f"{where}.properties.{field}", depth + 1, root or schema)
    if "additionalProperties" in schema:
        check_schema(schema["additionalProperties"], f"{where}.additionalProperties", depth + 1, root or schema)
    if "items" in schema:
        check_schema(schema["items"], f"{where}.items", depth + 1, root or schema)
    if "not" in schema:
        check_schema(schema["not"], f"{where}.not", depth + 1, root or schema)
    for word in ("anyOf", "allOf", "oneOf"):
        if word in schema:
            group = schema[word]
            if not isinstance(group, list) or not group:
                raise ContractError(f"{where}.{word} must be a list holding at least one rule")
            for index, rule in enumerate(group):
                check_schema(rule, f"{where}.{word}[{index}]", depth + 1, root or schema)
    for word in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if word in schema:
            value = schema[word]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ContractError(f"{where}.{word} must be a number")
            if word == "multipleOf" and value <= 0:
                raise ContractError(f"{where}.multipleOf must be greater than zero")
    for word in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if word in schema:
            value = schema[word]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{where}.{word} must be a whole number, zero or more")
    for word in ("uniqueItems", "nullable"):
        if word in schema and not isinstance(schema[word], bool):
            raise ContractError(f"{where}.{word} must be true or false")
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            raise ContractError(f"{where}.pattern must be text")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ContractError(f"{where}.pattern must be at most {MAX_PATTERN_CHARS} characters")
        if _REPEAT_IN_REPEAT.search(pattern):
            raise ContractError(
                f"{where}.pattern holds a repeat inside a repeat, such as (a+)+. "
                "On some text that never finishes, and nothing could stop it, so the whole "
                "run would hang. Write the pattern without the inner repeat."
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ContractError(f"{where}.pattern is not a usable pattern: {exc}") from exc
    if "format" in schema:
        found = schema["format"]
        if not isinstance(found, str):
            raise ContractError(f"{where}.format must be text")
        if found not in FORMATS:
            raise ContractError(
                f"{where}.format says {found}, which this tool cannot check. "
                f"It knows: {', '.join(FORMATS)}"
            )
    for holder in ("$defs", "definitions"):
        if holder in schema:
            group = schema[holder]
            if not isinstance(group, Mapping):
                raise ContractError(f"{where}.{holder} must be an object of named rules")
            for named, rule in group.items():
                check_schema(rule, f"{where}.{holder}.{named}", depth + 1, root or schema)


def _resolve(reference: str, root: Mapping[str, Any], where: str) -> Any:
    """Follow a #/... pointer inside the same contract."""

    found: Any = root
    for raw in reference.lstrip("#/").split("/"):
        if not raw:
            continue
        step = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(found, Mapping) and step in found:
            found = found[step]
        elif isinstance(found, list) and step.isdigit() and int(step) < len(found):
            found = found[int(step)]
        else:
            raise ContractError(f"{where} points at {reference}, which is not in this contract")
    return found


@dataclass
class _Run:
    root: Mapping[str, Any]
    problems: list[str]
    seen: int = 0


def _fails(value: Any, schema: object, path: str, run: _Run, depth: int) -> None:
    """Add a plain sentence for everything about `value` that breaks `schema`."""

    if len(run.problems) >= MAX_PROBLEMS:
        return
    if depth > MAX_DEPTH:
        # Giving up quietly here is the one thing this checker must never do.
        # A contract that points at itself lets the answer decide how deep the
        # checking goes, so a deep enough answer was never checked at all and
        # the case passed with nothing looked at.
        run.problems.append(
            f"{path} is nested more than {MAX_DEPTH} levels deep, which is deeper than "
            "this contract can be checked. Nothing below that point was looked at."
        )
        return
    if schema is True or schema == {}:
        return
    if schema is False:
        run.problems.append(f"{path} is not allowed here")
        return
    if not isinstance(schema, Mapping):
        # A pointer that lands on a piece of text or a number is not a rule.
        # Reading it as "anything goes" made every answer pass.
        run.problems.append(
            f"{path} is checked against something that is not a rule, so it cannot be checked"
        )
        return
    if "$ref" in schema:
        target = _resolve(str(schema["$ref"]), run.root, path)
        rest = {key: item for key, item in schema.items() if key != "$ref"}
        _fails(value, target, path, run, depth + 1)
        if rest:
            _fails(value, rest, path, run, depth + 1)
        return

    kind = _name(value)
    kinds = schema.get("type")
    if kinds is not None:
        wanted = list(kinds) if isinstance(kinds, list) else [kinds]
        if schema.get("nullable") is True and "null" not in wanted:
            wanted.append("null")
        # A whole number is a fine number. A number with a fraction is not a
        # whole number, and true is never a number, whatever Python thinks.
        matched = kind in wanted or (kind == "integer" and "number" in wanted)
        if not matched and kind == "number" and "integer" in wanted and float(value).is_integer():
            matched = True
        if not matched:
            run.problems.append(
                f"{path} must be {' or '.join(wanted)}, but it holds {_shown(value)}, "
                f"which is {kind}"
            )
            return
    if "const" in schema and value != schema["const"]:
        run.problems.append(f"{path} must be {_shown(schema['const'])}, but it holds {_shown(value)}")
    if "enum" in schema and not any(value == item for item in schema["enum"]):
        allowed = ", ".join(_shown(item, 20) for item in schema["enum"][:8])
        run.problems.append(f"{path} must be one of: {allowed}. It holds {_shown(value)}")
    if isinstance(value, str):
        _string_fails(value, schema, path, run)
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        _number_fails(value, schema, path, run)
    if isinstance(value, Mapping):
        _object_fails(value, schema, path, run, depth)
    if isinstance(value, (list, tuple)):
        _array_fails(list(value), schema, path, run, depth)
    for word, needed in (("allOf", "all"), ("anyOf", "any"), ("oneOf", "one")):
        if word not in schema:
            continue
        group = schema[word]
        passing = []
        for index, rule in enumerate(group):
            branch = _Run(run.root, [], run.seen)
            _fails(value, rule, path, branch, depth + 1)
            if not branch.problems:
                passing.append(index)
            elif needed == "all":
                run.problems.extend(branch.problems)
        if needed == "any" and not passing:
            run.problems.append(
                f"{path} matches none of the {len(group)} shapes it is allowed to have. "
                f"It holds {_shown(value)}"
            )
        if needed == "one" and len(passing) != 1:
            run.problems.append(
                f"{path} must match exactly one of the {len(group)} shapes, but it matches "
                f"{len(passing)}"
            )
    if "not" in schema:
        branch = _Run(run.root, [], run.seen)
        _fails(value, schema["not"], path, branch, depth + 1)
        if not branch.problems:
            run.problems.append(f"{path} has a shape it is not allowed to have")


def _string_fails(value: str, schema: Mapping[str, Any], path: str, run: _Run) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        run.problems.append(
            f"{path} must be at least {schema['minLength']} characters, but it is {len(value)}"
        )
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        run.problems.append(
            f"{path} must be at most {schema['maxLength']} characters, but it is {len(value)}"
        )
    if "pattern" in schema:
        if len(value) > MAX_PATTERN_INPUT:
            run.problems.append(
                f"{path} holds {len(value)} characters, more than the {MAX_PATTERN_INPUT} "
                "that can be tested against a pattern, so its shape was not checked"
            )
        elif not re.search(str(schema["pattern"]), value):
            run.problems.append(f"{path} does not match the pattern {schema['pattern']}")
    found = schema.get("format")
    if found in _FORMAT_PATTERNS and not _FORMAT_PATTERNS[found].match(value):
        run.problems.append(f"{path} is not a {found}: {_shown(value)}")


def _number_fails(value: float, schema: Mapping[str, Any], path: str, run: _Run) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        # Not a number, or larger than any number. Every comparison with one of
        # these is false, so without this it would slip past every limit.
        run.problems.append(
            f"{path} must be a real number, but it holds {_shown(value)}"
        )
        return
    if "minimum" in schema and value < schema["minimum"]:
        run.problems.append(f"{path} must be {schema['minimum']} or more, but it is {value}")
    if "maximum" in schema and value > schema["maximum"]:
        run.problems.append(f"{path} must be {schema['maximum']} or less, but it is {value}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        run.problems.append(f"{path} must be more than {schema['exclusiveMinimum']}, but it is {value}")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        run.problems.append(f"{path} must be less than {schema['exclusiveMaximum']}, but it is {value}")
    if "multipleOf" in schema:
        step = schema["multipleOf"]
        # Whole numbers are compared exactly. Dividing them turns them into the
        # nearest number a computer can hold, and a number bigger than that has
        # no nearest: it stopped the whole check with an error nobody could act
        # on.
        if isinstance(value, int) and isinstance(step, int) and not isinstance(value, bool):
            if step == 0 or value % step != 0:
                run.problems.append(f"{path} must be a multiple of {step}, but it is {value}")
            return
        try:
            share = value / step
        except (ZeroDivisionError, OverflowError):
            run.problems.append(
                f"{path} cannot be checked against a multiple of {_shown(step)}"
            )
            return
        if not math.isfinite(share):
            run.problems.append(
                f"{path} cannot be checked against a multiple of {_shown(step)}"
            )
            return
        if abs(share - round(share)) > 1e-9:
            run.problems.append(f"{path} must be a multiple of {step}, but it is {value}")


def _object_fails(
    value: Mapping[str, Any], schema: Mapping[str, Any], path: str, run: _Run, depth: int
) -> None:
    properties = schema.get("properties") or {}
    for field in schema.get("required", []):
        if field not in value:
            run.problems.append(f"{path}.{field} is missing")
    if "minProperties" in schema and len(value) < schema["minProperties"]:
        run.problems.append(
            f"{path} must hold at least {schema['minProperties']} fields, but it holds {len(value)}"
        )
    if "maxProperties" in schema and len(value) > schema["maxProperties"]:
        run.problems.append(
            f"{path} must hold at most {schema['maxProperties']} fields, but it holds {len(value)}"
        )
    for field, item in value.items():
        if field in properties:
            _fails(item, properties[field], f"{path}.{field}", run, depth + 1)
        elif "additionalProperties" in schema:
            extra = schema["additionalProperties"]
            if extra is False:
                run.problems.append(f"{path}.{field} is a field the contract does not allow")
            else:
                _fails(item, extra, f"{path}.{field}", run, depth + 1)


def _array_fails(
    value: list, schema: Mapping[str, Any], path: str, run: _Run, depth: int
) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        run.problems.append(
            f"{path} must hold at least {schema['minItems']} items, but it holds {len(value)}"
        )
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        run.problems.append(
            f"{path} must hold at most {schema['maxItems']} items, but it holds {len(value)}"
        )
    if schema.get("uniqueItems") is True:
        marks = [_same_value_mark(item) for item in value]
        if len(set(marks)) != len(marks):
            run.problems.append(f"{path} must not hold the same item twice")
    if "items" in schema:
        for index, item in enumerate(value):
            _fails(item, schema["items"], f"{path}[{index}]", run, depth + 1)
            if len(run.problems) >= MAX_PROBLEMS:
                return


def problems(value: Any, schema: object, name: str = "the answer") -> tuple[str, ...]:
    """Every way `value` fails to match `schema`, in plain sentences."""

    check_schema(schema, "the contract")
    run = _Run(root=schema if isinstance(schema, Mapping) else {}, problems=[])
    _fails(value, schema, name, run, 0)
    return tuple(run.problems[:MAX_PROBLEMS])


def matches(value: Any, schema: object) -> bool:
    """True when the answer has the promised shape."""

    return not problems(value, schema)
