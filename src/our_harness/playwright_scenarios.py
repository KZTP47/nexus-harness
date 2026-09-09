"""Compile literal local browser steps without evaluating project JavaScript."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _literal_tokens(source: str) -> tuple[str, dict[str, str]]:
    # Tokenize before matching calls: regex backtracking must never turn two
    # different JS strings (or statements) into one selector. Templates without
    # interpolation are strings too. Reject expressions instead of evaluating.
    values: dict[str, str] = {}
    output = []
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
            output.append(" ")
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated comment")
            index = end + 2
            output.append(" ")
            continue
        quote = source[index]
        if quote not in "'\"`":
            if source.startswith("NEXUS_LITERAL_", index):
                raise ValueError("Reserved compiler token")
            output.append(quote)
            index += 1
            continue
        index += 1
        chars = []
        while index < len(source) and source[index] != quote:
            char = source[index]
            if quote == "`" and source.startswith("${", index):
                raise ValueError("Dynamic template")
            if char in "\r\n" and quote != "`":
                raise ValueError("Newline in quoted string")
            index += 1
            if char == "\\":
                if index >= len(source):
                    raise ValueError("Incomplete escape")
                char = source[index]
                index += 1
                if char in "xu":
                    count = 2 if char == "x" else 4
                    digits = source[index:index + count]
                    if len(digits) != count or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise ValueError("Unsupported Unicode escape")
                    char = chr(int(digits, 16))
                    index += count
                elif char in "\r\n":
                    if char == "\r" and source[index:index + 1] == "\n":
                        index += 1
                    continue
                elif char.isdigit():
                    raise ValueError("Unsupported numeric escape")
                else:
                    char = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}.get(char, char)
            chars.append(char)
        if index >= len(source):
            raise ValueError("Unterminated string")
        index += 1
        token = f"NEXUS_LITERAL_{len(values)}"
        values[token] = "".join(chars).encode("utf-16", "surrogatepass").decode("utf-16")
        output.append(token)
    return "".join(output), values


def local_browser_scenario(source: str) -> dict[str, Any] | None:
    """Keep every supported assertion/action in order, or refuse the suite."""
    try:
        code, values = _literal_tokens(source)
    except (ValueError, UnicodeError):
        return None
    if re.search(r"\b(?:eval|Function|child_process|exec|spawn|setContent|if|for|while|switch|try)\b", code):
        return None
    literal = r"NEXUS_LITERAL_\d+"
    target = rf"page\s*\.\s*locator\s*\(\s*({literal})\s*\)"
    patterns = [
        ("goto", re.compile(rf"await\s+page\s*\.\s*goto\s*\(\s*({literal})\s*\)")),
        ("action", re.compile(rf"await\s+{target}\s*\.\s*(click|fill)\s*\(\s*({literal})?\s*\)")),
        ("assert", re.compile(rf"await\s+expect\s*\(\s*{target}\s*\)\s*\.\s*toHave(Text|Value|Attribute|Count)\s*\(\s*({literal}|\d+)(?:\s*,\s*({literal}))?\s*\)")),
    ]
    events = sorted((match.start(), match.end(), kind, match)
                    for kind, pattern in patterns for match in pattern.finditer(code))
    if not events or sum(kind == "goto" for _, _, kind, _ in events) != 1 or events[0][2] != "goto":
        return None
    # Unsupported awaited operations and browser calls cannot disappear from
    # a passing subset. Local probes support one straight-line test per file.
    remainder = code
    for start, end, _, _ in reversed(events):
        remainder = remainder[:start] + " " * (end - start) + remainder[end:]
    if re.search(r"\bawait\b|\bexpect\s*\(|\bpage\s*\.|\btest\s*\.\s*(?:skip|fixme|only|each|describe)\b", remainder) \
            or len(re.findall(r"\btest\s*\(", remainder)) > 1:
        return None
    imports = rf"(?:const|let|var)\s*\{{\s*(?:test\s*,\s*expect|expect\s*,\s*test)\s*\}}\s*=\s*require\s*\(\s*{literal}\s*\)\s*;?"
    remainder = re.sub(imports, "", remainder)
    remainder = re.sub(rf"import\s*\{{\s*(?:test\s*,\s*expect|expect\s*,\s*test)\s*\}}\s*from\s*{literal}\s*;?", "", remainder)
    wrapper = rf"test\s*\(\s*{literal}\s*,\s*async\s*\(\s*\{{\s*page\s*\}}\s*\)\s*=>\s*\{{[;\s]*\}}\s*\)\s*;?"
    if not re.fullmatch(rf"\s*(?:{wrapper}|[;\s]*)\s*", remainder):
        return None
    route = values[events[0][3].group(1)].strip()
    if not route or "://" in route or "\\" in route or ".." in route.split("/") or route.startswith("//"):
        return None
    steps = []
    for _, _, kind, match in events[1:]:
        selector = values[match.group(1)]
        if kind == "action":
            action, value = match.group(2, 3)
            if (action == "fill") != bool(value):
                return None
            steps.append({"action": action, "selector": selector,
                          **({"value": values[value]} if value else {})})
        else:
            matcher, first, second = match.group(2, 3, 4)
            if (matcher == "Attribute") != bool(second):
                return None
            if (matcher == "Count") != first.isdigit():
                return None
            steps.append({"action": "assert", "selector": selector, "kind": matcher.lower(),
                          "value": int(first) if matcher == "Count" else values[second or first],
                          **({"attribute": values[first]} if second else {})})
    assertions = [one for one in steps if one["action"] == "assert"]
    if not assertions:
        return None
    scenario = {"schema_version": 2, "route": "/" + route.lstrip("/"), "steps": steps,
                "actions": [one for one in steps if one["action"] != "assert"],
                "expected": {key: value for key, value in assertions[-1].items() if key != "action"}}
    scenario["scenario_digest"] = hashlib.sha256(json.dumps(
        scenario, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return scenario
