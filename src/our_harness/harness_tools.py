"""Nexus-owned tools adapted from the Claw Code tool surface.

These inspect or prepare proposals; actual mutations retain the existing
staged/goal transaction and native-provider owners. See CLAW_TOOL_ADOPTION.md.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import uuid
import json
import os
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit, parse_qs
from html.parser import HTMLParser

from .bounded_file_read import READ_FILE_INPUT_SCHEMA
from .execution import CommandRunner
from .ignore_policy import _glob_regex, IgnorePolicy
from .config import LoadedConfig
from .safety import confined_path
from .mcp import MCPClient, configured_server
from .models import HarnessError
from . import cancellation
from .public_web import fetch_public

CONTRACT = "nexus-harness-toolbox/v2"
TEXT = {"type": "string", "maxLength": 4096}
PATH = {"type": "string", "maxLength": 240}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
RESOURCE_TOOLS = frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})
RESOURCE_PAGE_LIMIT = 64
RESOURCE_SNAPSHOT_BYTES = 1_000_000
RESOURCE_TOTAL_BYTES = 2_000_000
RESOURCE_SNAPSHOT_LIMIT = 8


def _resource_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class _ResourceDeadline:
    def __init__(self, parent):
        self.parent = parent
        self.expires = time.monotonic() + parent.remaining_seconds("before MCP resource collection", 15)

    def remaining_seconds(self, operation, cap=None):
        cancellation.checkpoint()
        remaining = min(self.expires - time.monotonic(), self.parent.remaining_seconds(operation))
        if remaining <= 0:
            raise HarnessError("MCP resource collection exceeded its shared deadline")
        return remaining if cap is None else min(remaining, cap)

    def check(self, operation):
        self.remaining_seconds(operation)



def definition(name, description, properties, required=()):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}


TOOL_DEFINITIONS = [
    definition("tool_config", "Inspect non-secret Nexus tool limits, execution mode, and configured MCP server names. Does not change permissions or settings.", {}),
    definition("sleep", "Wait briefly at a cancellable Nexus tool boundary. Use durable scheduling for long waits.",
               {"seconds": {"type": "integer", "minimum": 0, "maximum": 5}}, ["seconds"]),
    definition("mcp_status", "Connect to a configured MCP server and report actual handshake/tool availability. Uses existing configured authentication; never invents an OAuth login.", {"server": TEXT}, ["server"]),
    definition("call_mcp_tool", "Call a configured, allowlisted, read-only MCP tool from any Nexus provider route. arguments_json is a JSON object encoded as text so strict response schemas preserve arbitrary MCP argument fields.",
               {"server": TEXT, "tool": TEXT, "arguments_json": {"type": "string", "maxLength": 8000}}, ["server", "tool", "arguments_json"]),
    definition("glob_search", "Find visible project files by glob without an index. ** includes nested directories. Results report scan limits.",
               {"pattern": TEXT, "path": PATH, "max_results": LIMIT}, ["pattern"]),
    definition("grep_search", "Search live visible UTF-8 files with literal text or a bounded regex. Returns paths and one-based lines; no index required.",
               {"pattern": TEXT, "path": PATH, "glob": TEXT, "regex": {"type": "boolean"},
                "ignore_case": {"type": "boolean"}, "max_results": LIMIT}, ["pattern"]),
    definition("git_status", "Inspect working-tree and branch status without changing Git state.", {}),
    definition("git_diff", "Inspect a bounded diff; external diff drivers and text conversion are disabled.",
               {"path": PATH, "staged": {"type": "boolean"}, "commit": TEXT}),
    definition("git_log", "Inspect bounded commit history, optionally for one visible path.", {"path": PATH, "max_results": LIMIT}),
    definition("git_show", "Read commit metadata or one visible file at a commit.", {"commit": TEXT, "path": PATH}, ["commit"]),
    definition("git_blame", "Read blame for a visible project file and bounded line range.",
               {"path": PATH, "start_line": {"type": "integer", "minimum": 1, "maximum": 10000000},
                "end_line": {"type": "integer", "minimum": 1, "maximum": 10000000}}, ["path"]),
    definition("tool_search", "Discover available Nexus tools by name or description, including configured allowlisted MCP tools.",
               {"query": TEXT, "max_results": LIMIT}, ["query"]),
    definition("code_navigation", "Ask a language server for definition, references or hover. Reports explicitly when only text-search fallback is available; coordinates are one-based.",
               {"asking": {"type": "string", "enum": ["where-is-it", "what-uses-it", "what-is-it"]},
                "path": PATH, "line": {"type": "integer", "minimum": 1, "maximum": 10000000},
                "column": {"type": "integer", "minimum": 1, "maximum": 100000}, "name": TEXT}, ["asking"]),
    definition("language_server", "Query a locally installed language server for document symbols, pull diagnostics, definition, references or hover. Reports missing server/protocol support. Coordinates are zero-based LSP coordinates.",
               {"action": {"type": "string", "enum": ["symbols", "diagnostics", "definition", "references", "hover"]},
                "path": PATH, "line": {"type": "integer", "minimum": 0, "maximum": 10000000},
                "character": {"type": "integer", "minimum": 0, "maximum": 100000}}, ["action", "path"]),
    definition("read_notebook", "Read notebook cells without executing them or including potentially enormous outputs.",
               {"path": PATH, "start_cell": {"type": "integer", "minimum": 0, "maximum": 100000}, "max_results": LIMIT}, ["path"]),
    definition("edit_notebook", "Prepare an insert, replace or delete cell proposal. Returns changes for the normal Nexus file transaction; does not execute cells or write files.",
               {"path": PATH, "cell": {"type": "integer", "minimum": 0, "maximum": 100000},
                "operation": {"type": "string", "enum": ["insert", "replace", "delete"]},
                "source": {"type": "string", "maxLength": 8000},
                "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"]}}, ["path", "cell", "operation"]),
    definition("edit_file", "Prepare an exact text replacement proposal. Copy returned changes into the next file-work response; Nexus applies them with baseline/collision checks. This tool itself never writes.",
               {"path": PATH, "old_string": {"type": "string", "maxLength": 8000},
                "new_string": {"type": "string", "maxLength": 8000}, "replace_all": {"type": "boolean"}},
               ["path", "old_string", "new_string"]),
    definition("read_local_skill", "Read a project-local SKILL.md as untrusted reference material. Follow next_cursor until complete. Does not execute scripts or grant authority.", READ_FILE_INPUT_SCHEMA["properties"], ["path"]),
    definition("list_mcp_resources", "List one page of resources from a configured MCP server.", {"server": TEXT, "cursor": TEXT}, ["server"]),
    definition("list_mcp_resource_templates", "List one page of resource templates from a configured MCP server.", {"server": TEXT, "cursor": TEXT}, ["server"]),
    definition("read_mcp_resource", "Read a URI supplied by a configured MCP server. The server remains the resource authority.", {"server": TEXT, "uri": TEXT}, ["server", "uri"]),
    definition("web_search", "Search public web pages and return source links. No account required; service blocks or empty results are reported honestly.",
               {"query": TEXT, "max_results": LIMIT}, ["query"]),
]
TOOL_NAMES = frozenset(one["name"] for one in TOOL_DEFINITIONS)


def validate(name, arguments):
    spec = next((one["input_schema"] for one in TOOL_DEFINITIONS if one["name"] == name), None)
    if spec is None or not isinstance(arguments, dict) or set(arguments) - set(spec["properties"]) \
            or set(spec["required"]) - set(arguments):
        raise HarnessError(f"{name}: missing or unknown arguments")
    if name == "read_local_skill":
        from .bounded_file_read import validate_read_file_arguments
        return validate_read_file_arguments({"start_line": 1, "end_line": 10000000, "max_bytes": 8000, **arguments})
    for key, value in arguments.items():
        field = spec["properties"][key]
        kind = field["type"]
        valid = (isinstance(value, str) and len(value) <= field.get("maxLength", 4096) and "\x00" not in value) if kind == "string" else (
            type(value) is bool if kind == "boolean" else type(value) is int and field.get("minimum", 0) <= value <= field.get("maximum", 100))
        if not valid or ("enum" in field and value not in field["enum"]):
            raise HarnessError(f"{name}: invalid {key}")
    return arguments


class SearchLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", "").split():
            self.active = {"url": attrs.get("href", ""), "title": ""}

    def handle_data(self, data):
        if self.active is not None:
            self.active["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            self.links.append(self.active)
            self.active = None


class HarnessTools:
    def __init__(self, session):
        self.session = session
        self.config = session.config
        self.root = session.root

    def _files(self, arguments):
        relative = arguments.get("path") or "."
        start = self.session._workspace_path(relative)
        files, truncated = [], False
        if start.is_file():
            return [start.relative_to(self.root).as_posix()], False
        for count, path in enumerate(self.session.ignore_policy.walk_files()):
            self.session.deadline.check("during live file search")
            if count >= 20000:
                truncated = True
                break
            if path.is_relative_to(start):
                files.append(path.relative_to(self.root).as_posix())
        return sorted(files), truncated

    def _run(self, argv, *, stdin=None, seconds=10, root=None):
        timeout = self.session.deadline.remaining_seconds("before Nexus tool process", seconds)
        config = self.config if root is None else LoadedConfig(copy.deepcopy(self.config.data), root, list(self.config.sources), dict(self.config.provenance), copy.deepcopy(self.config.trusted_floor))
        result = CommandRunner(config).run(argv, timeout=timeout, stdin_text=stdin,
            max_output_bytes=min(10000, self.session.per_call_bytes),
            environment_overrides={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"})
        if result.timed_out:
            raise HarnessError("Nexus tool process exceeded its time limit")
        if result.exit_code:
            raise HarnessError("Nexus tool process failed: " + result.stderr[:2000])
        return result

    def execute(self, name, arguments):
        args = validate(name, arguments)
        maximum = args.get("max_results", 30)
        if name == "call_mcp_tool":
            try:
                parsed = json.loads(args["arguments_json"])
            except ValueError as exc:
                raise HarnessError("MCP arguments_json must encode a JSON object") from exc
            if not isinstance(parsed, dict):
                raise HarnessError("MCP arguments_json must encode a JSON object")
            return self.session._mcp_call({"server": args["server"], "tool": args["tool"], "arguments": parsed})
        if name == "tool_config":
            return {"contract": CONTRACT, "execution_mode": self.config.get("execution.mode"),
                    "max_file_bytes": self.config.get("project.max_file_bytes"),
                    "max_calls": self.session.max_calls, "output_bytes_per_call": self.session.per_call_bytes,
                    "mcp_servers": [one.get("name") for one in self.config.get("mcp.servers", [])]}
        if name == "sleep":
            started = time.monotonic()
            while time.monotonic() - started < args["seconds"]:
                self.session.deadline.check("during tool wait")
                cancellation.checkpoint()
                time.sleep(min(0.05, max(0, args["seconds"] - (time.monotonic() - started))))
            return {"elapsed_seconds": round(time.monotonic() - started, 3)}
        if name == "mcp_status":
            server = configured_server(self.config, args["server"])
            with MCPClient(server, deadline=self.session.deadline, timeout=self.session.deadline.remaining_seconds("before MCP status", 15),
                           max_response_bytes=min(10000, self.session.per_call_bytes)) as client:
                names = [one.get("name") for one in client.list_tools() if one.get("name") in server.get("allowed_tools", [])]
                return {"server": args["server"], "connected": True, "allowed_tools_available": names,
                        "protocol_version": client.protocol_version}
        if name in {"glob_search", "grep_search"}:
            files, truncated = self._files(args)
            pattern = args.get("pattern", "")
            if not pattern:
                raise HarnessError("Search pattern must not be empty")
            glob_pattern = pattern if name == "glob_search" else args.get("glob") or "**"
            try:
                glob = _glob_regex(glob_pattern)
            except re.error as exc:
                raise HarnessError("Invalid file glob pattern") from exc
            files = [path for path in files if glob.fullmatch(path) or ("/" not in glob_pattern and glob.fullmatch(Path(path).name))]
            if name == "glob_search":
                return {"files": files[:maximum], "truncated": truncated or len(files) > maximum}
            records, size, skipped = [], 0, 0
            for path in files:
                self.session.deadline.check("during live content search")
                try:
                    raw = self.session._stable_regular_bytes(path)
                    if b"\x00" in raw:
                        skipped += 1
                        continue
                    content = raw.decode("utf-8")
                except (HarnessError, UnicodeError, OSError):
                    skipped += 1
                    continue
                size += len(raw)
                if size > 8_000_000:
                    truncated = True
                    break
                records.append([path, content])
            # Isolate arbitrary regex evaluation in a time-limited, contained
            # process. A pathological project/user pattern cannot wedge Nexus.
            program = """import json,re,sys
a=json.load(sys.stdin)
p=re.compile(a['pattern'] if a['regex'] else re.escape(a['pattern']),re.I if a['ignore_case'] else 0)
matches=[]
for path,content in a['records']:
 for line,text in enumerate(content.splitlines(),1):
  if p.search(text): matches.append({'path':path,'line':line,'text':text[:400]})
  if len(matches)>a['maximum']: break
 if len(matches)>a['maximum']: break
print(json.dumps({'matches':matches[:a['maximum']], 'truncated':len(matches)>a['maximum']}))
"""
            result = self._run([sys.executable, "-I", "-c", program], seconds=5, stdin=json.dumps({
                "pattern": pattern, "regex": args.get("regex", False), "ignore_case": args.get("ignore_case", False),
                "records": records, "maximum": min(maximum, 15)}))
            try:
                value = json.loads(result.stdout)
            except ValueError as exc:
                raise HarnessError("Search output exceeded the bounded response; narrow the pattern") from exc
            return {**value, "truncated": truncated or value["truncated"], "skipped_files": skipped}
        if name.startswith("git_"):
            return self._git(name, args)
        if name == "tool_search":
            words = args["query"].casefold().split()
            definitions = self.session.definitions()
            matches = [one for one in definitions if not words or any(word in (one["name"] + " " + one["description"]).casefold() for word in words)]
            mcp = [{"server": server["name"], "tools": server.get("allowed_tools", [])}
                   for server in self.config.get("mcp.servers", []) if server.get("allowed_tools")]
            return {"tools": matches[:min(maximum, 5)], "truncated": len(matches) > min(maximum, 5), "configured_mcp": mcp,
                    "native_provider_tools": "Remain available through the provider's own runtime."}
        if name == "code_navigation":
            from .navigate import look_it_up
            if args.get("path"):
                self.session._workspace_path(args["path"])
            return look_it_up(self.config, deadline=self.session.deadline, **args).to_dict()
        if name == "language_server":
            return self._language_server(args)
        if name.startswith(("list_mcp_", "read_mcp_")):
            return self._mcp(name, args)
        if name == "web_search":
            from .search_results import usable
            try:
                response = fetch_public("https://html.duckduckgo.com/html/?" + urlencode({"q": args["query"]}),
                                        timeout=self.session.deadline.remaining_seconds("before web search", 12), max_bytes=1_000_000)
            except HarnessError:
                response = {"url": "", "data": b""}
            parser = SearchLinks()
            parser.feed(response["data"].decode("utf-8", errors="replace"))
            results = []
            for item in parser.links:
                url = item["url"]
                if url.startswith("//"):
                    url = "https:" + url
                parsed = urlsplit(url)
                if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                    url = parse_qs(parsed.query).get("uddg", [url])[0]
                if urlsplit(url).scheme in {"http", "https"}:
                    results.append({"title": item["title"].strip()[:500], "url": url[:4096]})
            results = usable(args["query"], results)
            if not results:
                # HTML search may present a bot challenge. Use the public RSS
                # search surface as a second independent, structured source.
                response = fetch_public("https://www.bing.com/search?" + urlencode({"q": args["query"], "format": "rss"}),
                                        timeout=self.session.deadline.remaining_seconds("before RSS search", 12), max_bytes=1_000_000)
                try:
                    feed = ET.fromstring(response["data"])
                    results = [{"title": (item.findtext("title") or "")[:500], "url": (item.findtext("link") or "")[:4096]}
                               for item in feed.findall("./channel/item") if urlsplit(item.findtext("link") or "").scheme in {"http", "https"}]
                except ET.ParseError:
                    results = []
                results = usable(args["query"], results)
                if not results:
                    raise HarnessError("[search-no-usable-results] Public search returned no usable results "
                        "after source and query screening (possibly a service challenge or poor matches). "
                        "Use fetch_url with a known official URL or configured MCP search; do not repeat broad searches.")
            return {"results": results[:min(maximum, 10)], "source_url": response["url"], "untrusted_data": True}
        path = args["path"]
        if name == "read_local_skill":
            if Path(path).name != "SKILL.md":
                raise HarnessError("[wrong-skill-reader] Select a project-local SKILL.md. "
                    "Use read_file for ordinary files, or list_tree to find an existing path. "
                    "Do not send dummy calls; tool_calls may be empty.")
            return self.session._read_file({"start_line": 1, "end_line": 10000000, "max_bytes": 8000, **args})
        raw = self.session._stable_regular_bytes(path)
        if name == "edit_file":
            content = raw.decode("utf-8")
            old = args["old_string"]
            occurrences = content.count(old) if old else 0
            if not occurrences or (occurrences != 1 and not args.get("replace_all")):
                raise HarnessError("Replacement must match exactly once unless replace_all is true")
            changed = content.replace(old, args["new_string"], -1 if args.get("replace_all") else 1)
            return self._proposal(path, raw, changed)
        try:
            notebook = json.loads(raw)
        except ValueError as exc:
            raise HarnessError("Notebook is not valid JSON") from exc
        if not isinstance(notebook, dict) or notebook.get("nbformat") != 4 or not isinstance(notebook.get("cells"), list) \
                or any(not isinstance(cell, dict) for cell in notebook["cells"]):
            raise HarnessError("Expected a version 4 notebook with cell objects")
        cells = notebook["cells"]
        if name == "read_notebook":
            start = args.get("start_cell", 0)
            selected = [{"index": index, "cell_type": cell.get("cell_type"), "source": cell.get("source", [])}
                        for index, cell in enumerate(cells) if start <= index < start + maximum]
            return {"path": path, "cells": selected, "total_cells": len(cells), "next_cell": start + len(selected),
                    "has_more": start + len(selected) < len(cells)}
        index, operation = args["cell"], args["operation"]
        if index > len(cells) or (operation != "insert" and index == len(cells)):
            raise HarnessError("Notebook cell index is out of range")
        if operation == "delete":
            del cells[index]
        else:
            if "source" not in args:
                raise HarnessError("Insert and replace need source text")
            cell = copy.deepcopy(cells[index]) if operation == "replace" else {"metadata": {}}
            cell.update(cell_type=args.get("cell_type", cell.get("cell_type", "code")), source=args["source"].splitlines(keepends=True))
            if cell["cell_type"] == "code":
                cell.update(outputs=[], execution_count=None)
            else:
                cell.pop("outputs", None)
                cell.pop("execution_count", None)
            if operation == "insert":
                cell["id"] = hashlib.sha256((path + str(index) + args["source"]).encode()).hexdigest()[:12]
                cells.insert(index, cell)
            else:
                cells[index] = cell
        return self._proposal(path, raw, json.dumps(notebook, ensure_ascii=False, indent=1) + "\n")

    def _proposal(self, path, raw, content):
        if len(content.encode("utf-8")) > min(8000, self.session.per_call_bytes // 2):
            raise HarnessError("Edited file is too large for a complete proposal; use native or staged file tools")
        return {"applied": False, "baseline_sha256": hashlib.sha256(raw).hexdigest(),
                "changes": [{"path": path, "content": content, "reason": "Apply the requested exact edit"}],
                "next_step": "Submit changes through the normal Nexus file-work response; the tool has not applied them."}

    def _git(self, name, args):
        root = self.session.git_root
        if not (root / ".git").exists():
            raise HarnessError("The selected project has no Git repository at its root; Nexus will not inspect an unrelated parent repository")
        path = args.get("path")
        if path:
            IgnorePolicy(root, set(self.config.get("project.ignore", []))).require_visible(path)
            confined_path(root, path, allow_missing=name != "git_blame")
        commit = args.get("commit", "HEAD")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./~^@{}+-]{0,199}", commit):
            raise HarnessError("Use a plain Git revision, not an option, range, or object path")
        prefix = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false"]
        if name == "git_status":
            command = ["status", "--short", "--branch", "--untracked-files=normal"]
        elif name == "git_log":
            command = ["log", "--format=%h %s", f"-n{args.get('max_results', 20)}"]
        elif name == "git_diff":
            command = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
            if args.get("staged"):
                command.append("--cached")
            if args.get("commit"):
                command.append(commit)
            if not path:
                # Listing paths/statistics does not expose ignored file contents.
                command.append("--stat")
        elif name == "git_show":
            command = ["show", "--no-ext-diff", "--no-textconv", "--no-color"]
            command += [f"{commit}:{path}"] if path else ["--no-patch", "--format=fuller", commit]
            path = None
        else:
            start = args.get("start_line", 1)
            end = args.get("end_line", start + 49)
            if end < start or end - start > 100:
                raise HarnessError("Blame line range must contain at most 101 lines")
            command = ["blame", "--no-textconv", "-L", f"{start},{end}"]
        if path:
            command += ["--", path]
        result = self._run(prefix + command, root=root)
        return {"tool": name, "stdout": result.stdout, "stderr": result.stderr,
                "truncated": result.output_truncated, "exit_code": result.exit_code,
                "source": "selected_project_repository", "private_candidate_changes": "Use read_file/glob_search in the private working copy for unpublished edits."}

    def _resource_binding(self, name, server):
        return hashlib.sha256(json.dumps(["nexus-resource-snapshot:v1", name, server], sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _resource_cursor(self, snapshot_id, offset):
        body = f"nexus-resource-v1:{snapshot_id}:{offset}"
        signature = hmac.new(self.session._mcp_resource_secret, body.encode(), hashlib.sha256).hexdigest()
        return body + ":" + signature

    def _resource_snapshot(self, cursor, binding):
        try:
            prefix, snapshot_id, offset_text, signature = cursor.split(":")
            offset = int(offset_text)
            expected = self._resource_cursor(snapshot_id, offset)
            snapshot = self.session._mcp_resource_snapshots[snapshot_id]
            if prefix != "nexus-resource-v1" or not hmac.compare_digest(cursor, expected) or snapshot["binding"] != binding or not 0 <= offset < len(snapshot["items"]):
                raise ValueError()
            return snapshot_id, snapshot, offset
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HarnessError("MCP resource cursor is stale or foreign. Start a new listing without a cursor; server cursors and cursors from another session/configuration cannot be resumed.") from None

    def validate_resource_result(self, name, args, value):
        if name != "read_mcp_resource" and args.get("cursor"):
            self._resource_snapshot(args["cursor"], self._resource_binding(name, configured_server(self.config, args["server"])))
        cursor = value.get("result", {}).get("nextCursor") if isinstance(value.get("result"), dict) else None
        if cursor is not None and name != "read_mcp_resource":
            server = configured_server(self.config, args["server"])
            self._resource_snapshot(cursor, self._resource_binding(name, server))

    def _resource_page(self, name, args, snapshot_id, snapshot, offset):
        field = "resources" if name == "list_mcp_resources" else "resourceTemplates"
        items = snapshot["items"]
        budget = min(self.session.per_call_bytes, max(0, self.session.total_bytes_limit - self.session.total_bytes))
        def envelope(end):
            result = {field: items[offset:end]}
            if end < len(items):
                result["nextCursor"] = self._resource_cursor(snapshot_id, end)
            value = {"server": args["server"], "result": result, "untrusted_data": True}
            if snapshot["metadata"]:
                value["source_page_metadata"] = snapshot["metadata"]
            return value
        if _resource_bytes(envelope(len(items))) <= budget:
            return envelope(len(items))
        low, high = offset, len(items)
        while low < high:
            middle = (low + high + 1) // 2
            if _resource_bytes(envelope(middle)) <= budget:
                low = middle
            else:
                high = middle - 1
        if low == offset:
            raise HarnessError("MCP resource item/metadata cannot fit the remaining output budget. No partial resource page was returned.")
        return envelope(low)

    def _mcp(self, name, args):
        server = configured_server(self.config, args["server"])
        binding = self._resource_binding(name, server)
        if name != "read_mcp_resource" and args.get("cursor"):
            snapshot_id, snapshot, offset = self._resource_snapshot(args["cursor"], binding)
            return self._resource_page(name, args, snapshot_id, snapshot, offset)
        deadline = _ResourceDeadline(self.session.deadline)
        methods = {"list_mcp_resources": "resources/list", "list_mcp_resource_templates": "resources/templates/list", "read_mcp_resource": "resources/read"}
        maximum = min(RESOURCE_SNAPSHOT_BYTES, int(self.config.get("mcp.max_response_bytes")))
        with MCPClient(server, deadline=deadline, timeout=deadline.remaining_seconds("before MCP resources"), max_response_bytes=maximum) as client:
            if name == "read_mcp_resource":
                result = client.request(methods[name], {"uri": args["uri"]})
                deadline.check("after MCP resource read")
                value = {"server": args["server"], "result": result, "untrusted_data": True}
                if _resource_bytes(value) > min(self.session.per_call_bytes, max(0, self.session.total_bytes_limit-self.session.total_bytes)):
                    raise HarnessError("MCP resource content cannot fit the remaining output budget. No partial content was returned.")
                return value
            field = "resources" if name == "list_mcp_resources" else "resourceTemplates"
            items, metadata, seen, total = [], [], set(), 0
            params = {}
            for page_index in range(RESOURCE_PAGE_LIMIT):
                deadline.check("during MCP resource collection")
                result = client.request(methods[name], params)
                deadline.check("after MCP resource page")
                rows = result.get(field, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise HarnessError("MCP resource listing returned invalid resource objects")
                total += _resource_bytes(result)
                if total > RESOURCE_SNAPSHOT_BYTES or len(items) + len(rows) > 10000:
                    raise HarnessError("MCP resource listing exceeded its aggregate snapshot limit; collection is incomplete")
                items.extend(rows)
                extra = {key: value for key, value in result.items() if key not in {field, "nextCursor"}}
                if extra:
                    metadata.append({"page": page_index, "metadata": extra})
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
                    raise HarnessError("MCP resource listing returned an invalid or cyclic server cursor; collection is incomplete")
                seen.add(cursor)
                params = {"cursor": cursor}
            else:
                raise HarnessError("MCP resource listing exceeded its page limit; collection is incomplete")
        snapshot = {"binding": binding, "items": items, "metadata": metadata}
        size = _resource_bytes(snapshot)
        if size > RESOURCE_SNAPSHOT_BYTES:
            raise HarnessError("MCP resource snapshot exceeded its storage limit")
        snapshot_id = uuid.uuid4().hex
        value = self._resource_page(name, args, snapshot_id, snapshot, 0)
        if value["result"].get("nextCursor"):
            held = self.session._mcp_resource_snapshots
            if len(held) >= RESOURCE_SNAPSHOT_LIMIT or sum(one["size"] for one in held.values()) + size > RESOURCE_TOTAL_BYTES:
                raise HarnessError("MCP resource snapshot storage is full. Start a new tool session to list more resources.")
            held[snapshot_id] = {**snapshot, "size": size}
        return value

    def _language_server(self, args):
        from . import navigate
        path = self.session._workspace_path(args["path"])
        content = self.session._stable_regular_bytes(args["path"]).decode("utf-8")
        chosen = navigate._server_for(path)
        if chosen is None:
            return {"available": False, "reason": "Install the language server for this file type; code_navigation offers labelled text-search fallback."}
        label, argv = chosen
        talking = navigate._Talking(argv, self.root, deadline=self.session.deadline)
        methods = {"symbols": "textDocument/documentSymbol", "diagnostics": "textDocument/diagnostic",
                   "definition": "textDocument/definition", "references": "textDocument/references", "hover": "textDocument/hover"}
        try:
            talking.ask("initialize", {"processId": os.getpid(), "rootUri": self.root.as_uri(),
                                       "capabilities": {"textDocument": {"diagnostic": {}}}},
                        self.session.deadline.remaining_seconds("starting language server", 30))
            talking.tell("initialized", {})
            talking.tell("textDocument/didOpen", {"textDocument": {"uri": path.as_uri(), "languageId": path.suffix.lstrip("."), "version": 1, "text": content}})
            params = {"textDocument": {"uri": path.as_uri()}}
            if args["action"] in {"definition", "references", "hover"}:
                params["position"] = {"line": args.get("line", 0), "character": args.get("character", 0)}
            if args["action"] == "references":
                params["context"] = {"includeDeclaration": True}
            result = talking.ask(methods[args["action"]], params,
                                 self.session.deadline.remaining_seconds("querying language server", 15))
            return {"available": True, "server": label, "action": args["action"], "result": result, "untrusted_data": True}
        finally:
            talking.stop()
