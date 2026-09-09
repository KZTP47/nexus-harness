"""UTF-8 file pages whose metadata and continuation fit the output envelope.

Opening and confining the file remain the caller's responsibility. A cursor is
read-only continuation data, not authority to open a path or execute a tool.
"""

from __future__ import annotations

import base64
from bisect import bisect_left, bisect_right
import hashlib
import json
from typing import Any

from .models import HarnessError
from .document_text import DOCX_TEXT_VERSION, extract_docx_text, is_docx


READ_FILE_SCHEMA_VERSION = 1


class FileReadOutputLimit(HarnessError):
    """The remaining output allowance cannot carry a useful complete page."""


READ_FILE_DESCRIPTION = (
    "Read UTF-8 text or extract Word .docx text from a project-relative regular file. "
    "DOCX line ranges refer to extracted paragraphs/tables/notes, not rendered pages or images. max_bytes is a desired "
    "content cap; Nexus reduces large requests to fit its output allowance. "
    "The result is a complete JSON page. If next_cursor is present, repeat the "
    "original path, start_line and end_line with that cursor to read the next "
    "page, including the rest of a long line. Use an empty or null cursor for "
    "the first page."
)
READ_FILE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
        "end_line": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
        "max_bytes": {
            "type": "integer", "minimum": 1,
            "description": (
                "Desired UTF-8 content bytes, for example 32000. Larger positive "
                "values are safely reduced to the current output allowance."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "Copy next_cursor from the preceding page and keep its original "
                "path/start_line/end_line. Empty or null starts a new read."
            ),
        },
    },
    # Existing provider replies remain valid. Strict providers close this at
    # serialization and can use either documented first-page placeholder.
    "required": ["path", "start_line", "end_line", "max_bytes"],
    "additionalProperties": False,
}


def validate_read_file_arguments(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise HarnessError("read_file arguments must be an object")
    missing = sorted(set(READ_FILE_INPUT_SCHEMA["required"]) - set(arguments))
    extra = sorted(set(arguments) - set(READ_FILE_INPUT_SCHEMA["properties"]))
    if missing:
        raise HarnessError("read_file arguments are missing fields: " + ", ".join(missing))
    if extra:
        raise HarnessError("read_file arguments contain unknown fields: " + ", ".join(extra))
    path = arguments["path"]
    if not isinstance(path, str) or not path.strip():
        raise HarnessError("read_file path must be a non-empty project-relative string")
    for name in ("start_line", "end_line"):
        value = arguments[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000_000:
            raise HarnessError(f"read_file {name} must be an integer from 1 through 10000000")
    if arguments["end_line"] < arguments["start_line"]:
        raise HarnessError("read_file end_line must be at least start_line")
    requested = arguments["max_bytes"]
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise HarnessError(
            "read_file max_bytes must be a positive integer, for example 32000; "
            "Nexus safely reduces large requests to its current output allowance"
        )
    cursor = arguments.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise HarnessError("read_file cursor must be a next_cursor string, empty string, or null")
    return dict(arguments)


def _canonical(value: object) -> bytes:
    # AgentToolSession uses this same serialization before it accounts bytes.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _compact_digest(value: str) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(value)).decode("ascii").rstrip("=")


def _cursor(contract: str, selection: str, offset: int) -> str:
    return f"r{READ_FILE_SCHEMA_VERSION}.{_compact_digest(contract)}.{_compact_digest(selection)}.{offset}"


def read_file_page(
    raw: bytes,
    arguments: object,
    *,
    output_limit: int,
    configured_output_limit: int,
    max_file_bytes: int,
) -> dict[str, Any]:
    """Return an exact page that fits ``output_limit`` after JSON serialization.

    ``output_limit`` includes metadata and may decrease as a run consumes its
    budget. The two configured limits belong to the stable cursor contract;
    changing them invalidates a saved cursor instead of reusing obsolete state.
    Insufficient space for metadata plus a character is an explicit error. No
    successful page can silently lose its continuation to outer truncation.
    """

    value = validate_read_file_arguments(arguments)
    if not isinstance(raw, bytes):
        raise HarnessError("read_file requires stable raw file bytes")
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        for limit in (output_limit, configured_output_limit, max_file_bytes)
    ) or configured_output_limit < 1 or max_file_bytes < 1:
        raise HarnessError("read_file output configuration is invalid")
    if len(raw) > max_file_bytes:
        raise HarnessError("read_file target exceeds the configured project.max_file_bytes")
    output_limit = min(output_limit, configured_output_limit)
    document = is_docx(value["path"])
    try:
        text = extract_docx_text(raw) if document else raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HarnessError(
            "read_file target is not valid UTF-8 text; Nexus did not replace or corrupt bytes"
        ) from exc

    relative = value["path"].replace("\\", "/")
    start_line = value["start_line"]
    end_line = value["end_line"]
    lines = text.splitlines(keepends=True)
    selected_lines = lines[start_line - 1:end_line]
    selected = "".join(selected_lines)
    selected_bytes = selected.encode("utf-8")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    contract = _digest({
        "schema_version": READ_FILE_SCHEMA_VERSION,
        "encoding": "utf-8", "offset_unit": "selected_range_utf8_bytes",
        "configured_output_limit": configured_output_limit,
        "max_file_bytes": max_file_bytes,
        **({"docx_text_version": DOCX_TEXT_VERSION} if document else {}),
    })
    selection = _digest({
        "path": relative, "start_line": start_line, "end_line": end_line,
        "source_sha256": source_sha256,
    })
    offset = 0
    cursor = value.get("cursor") or ""
    if cursor:
        prefix = _cursor(contract, selection, 0).rsplit(".", 1)[0] + "."
        if len(cursor) > 128 or not cursor.startswith(prefix):
            raise HarnessError(
                "read_file cursor is invalid or stale: the file, requested path/range, "
                "or read configuration changed. Restart this read with an empty cursor."
            )
        suffix = cursor[len(prefix):]
        if not suffix or not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 20:
            raise HarnessError("read_file cursor offset is invalid; restart with an empty cursor")
        offset = int(suffix)
        if offset > len(selected_bytes):
            raise HarnessError("read_file cursor is past the selected range; restart with an empty cursor")
    try:
        remaining = selected_bytes[offset:].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HarnessError("read_file cursor splits a UTF-8 character; restart with an empty cursor") from exc

    line_ends: list[int] = []
    line_byte_count = 0
    for line in selected_lines:
        line_byte_count += len(line.encode("utf-8"))
        line_ends.append(line_byte_count)
    page_start_line = start_line + bisect_right(line_ends, offset) if remaining else start_line
    desired_bytes = min(value["max_bytes"], output_limit)

    def page(content: str) -> dict[str, Any]:
        next_offset = offset + len(content.encode("utf-8"))
        has_more = next_offset < len(selected_bytes)
        actual_end = start_line + bisect_left(line_ends, next_offset) if content else min(end_line, len(lines))
        return {
            "schema_version": READ_FILE_SCHEMA_VERSION,
            "contract_fingerprint_sha256": contract,
            "path": relative,
            "start_line": page_start_line,
            "end_line": actual_end,
            "requested_start_line": start_line,
            "requested_end_line": end_line,
            "total_lines": len(lines),
            "sha256": source_sha256,
            "content": content,
            **({"format": "docx", "extraction_version": DOCX_TEXT_VERSION} if document else {}),
            "truncated": has_more,
            "byte_offset": offset,
            "next_byte_offset": next_offset,
            "next_cursor": _cursor(contract, selection, next_offset) if has_more else None,
        }

    # The completed page has no cursor and may fit even when the immediately
    # shorter page needs one. Check it before binary-searching partial pages.
    complete = page(remaining)
    if len(remaining.encode("utf-8")) <= desired_bytes and len(_canonical(complete)) <= output_limit:
        return complete

    # Partial pages have a monotone serialized size: both source bytes and JSON
    # escapes count, while cursor offsets only grow. Search character boundaries
    # so CRLF, long lines, non-BMP characters, quotes and backslashes are exact.
    low, high = 0, max(0, len(remaining) - 1)
    best: dict[str, Any] | None = None
    while low <= high:
        middle = (low + high) // 2
        content = remaining[:middle]
        candidate = page(content)
        if len(content.encode("utf-8")) <= desired_bytes and len(_canonical(candidate)) <= output_limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if best is None or (remaining and not best["content"]):
        if remaining and len(remaining[0].encode("utf-8")) > value["max_bytes"]:
            raise HarnessError(
                "read_file max_bytes cannot fit the next UTF-8 character; "
                "retry with max_bytes of at least 4"
            )
        raise FileReadOutputLimit(
            "read_file has insufficient output allowance for a complete page and its "
            "continuation. No file text was returned. The remaining tool-output budget "
            "or workflow.max_tool_output_bytes must allow the page metadata."
        )
    return best
