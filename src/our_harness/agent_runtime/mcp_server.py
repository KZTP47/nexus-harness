"""The Nexus MCP server every v3 agent gets: team messages, tasks, leases, questions.

Started by each agent's own CLI as a stdio MCP server, with the command line
TeamRun.start builds (this module, plus --db, --team, --agent and --roster).

It reads and writes the team's mailbox, and check_page lets an agent see its
page in a hidden browser (web_preview). The Nexus server watches the
mailbox and pushes new messages into the recipients' running sessions, so an
agent never has to poll. Everything here is advisory to the agents: nothing in
it can grant permissions, and a teammate's message is labelled as such.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

from .mailbox import CLOSURE_REASONS, TASK_STATES, Mailbox, dumps

PROTOCOL = "2025-06-18"
ASK_TIMEOUT_SECONDS = 30 * 60
# Screenshots larger than this are left as a path to open instead of an image.
IMAGE_LIMIT = 4_000_000

TOOLS: list[dict[str, Any]] = [
    {"name": "send_message", "description": "Send a message to a teammate (by id or name) or to everyone with to='*'. "
     "It is delivered into their running session right away; you do not need to wait for it.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["to", "text"]}},
    {"name": "read_messages", "description": "Read the team messages to or from you, oldest first.",
     "inputSchema": {"type": "object", "properties": {"after": {"type": "integer"}}}},
    {"name": "list_team", "description": "Who is on the team, their role, and which one leads.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_task", "description": "Add a task to the shared team board, optionally assigned to a teammate.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "detail": {"type": "string"},
                                                      "owner": {"type": "string"}}, "required": ["title"]}},
    {"name": "update_task", "description": "Change a task's state. Closing it (done/cancelled) needs closure_reason: "
     + ", ".join(CLOSURE_REASONS) + "; handed_off/blocked_on/escalated also need target.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "state": {"type": "string", "enum": list(TASK_STATES)},
         "closure_reason": {"type": "string", "enum": list(CLOSURE_REASONS)}, "target": {"type": "string"},
         "note": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task_id", "state"]}},
    {"name": "list_tasks", "description": "The shared team task board.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "reserve_paths", "description": "Tell the team which files or folders you are about to edit (glob "
     "patterns). Returns anyone already working there. Advisory: it never blocks you.",
     "inputSchema": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                     "required": ["paths"]}},
    {"name": "release_paths", "description": "Release files you reserved (all of yours when paths is omitted).",
     "inputSchema": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "ask_user", "description": "Ask the user a question and wait for the answer. Use only for decisions "
     "only the user can make.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "report_result", "description": "Report your outcome to the user: status done, blocked or needs_input, "
     "with a short summary of what you did and where the results are.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["done", "blocked", "needs_input"]},
                                                      "summary": {"type": "string"}}, "required": ["status", "summary"]}},
    {"name": "check_page", "description": "Look at a web page or browser game the way the user will, without "
     "opening anything on their screen (the browser stays hidden unless the user chose visible browser checks). "
     "It opens the page straight from disk (double-clicking index.html) and from a local server, waits, and "
     "returns both screenshots for you to look at, script errors, files that failed to load and a one-line "
     "verdict. Use it after every change to a page, and before telling anyone that it works or looks right.",
     "inputSchema": {"type": "object", "properties": {
         "page": {"type": "string", "description": "The .html file, relative to your working folder (default index.html)."},
         "wait_ms": {"type": "integer", "description": "How long to let the page run before the screenshot (default 2500)."}}}},
    {"name": "approve_action", "description": "Internal: Nexus asks the user to approve a tool use.",
     "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"}},
                     "required": ["tool_name"]}},
]


class Server:
    def __init__(self, mailbox: Mailbox, team: str, agent: str, roster: list[dict[str, Any]],
                 cwd: str = "", settings: str = ""):
        self.mailbox = mailbox
        self.team = team
        self.agent = agent
        self.roster = roster
        self.cwd = cwd
        self.settings = settings

    def check_page(self, args: dict[str, Any]) -> dict[str, Any]:
        """The page as the user will see it, with the screenshots attached as images."""
        from .. import web_preview
        from .browser_guard import read_mode

        root = Path(self.cwd or ".").resolve()
        page = Path(str(args.get("page") or "index.html").strip() or "index.html")
        if page.is_absolute():
            page = page.resolve()
            try:
                page = page.relative_to(root)
            except ValueError:
                root, page = page.parent, Path(page.name)
        try:
            wait_ms = int(args.get("wait_ms") or web_preview.DEFAULT_WAIT_MS)
        except (TypeError, ValueError):
            wait_ms = web_preview.DEFAULT_WAIT_MS
        mode = read_mode(self.settings)
        result = web_preview.preview(root, page.as_posix(), wait_ms=wait_ms, headless=mode != "visible")
        content: list[dict[str, Any]] = []
        for one in result.get("pages") or []:
            shot = one.get("screenshot")
            if not shot:
                continue
            path = root / shot
            one["screenshot"] = str(path)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) <= IMAGE_LIMIT:
                content.append({"type": "text", "text": f"Screenshot, opened {'from disk' if one.get('mode') == 'file' else 'from a local server'}:"})
                content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"})
        result["browser"] = f"{mode} (the user's choice)"
        result["how_to_look"] = ("The screenshots are attached. If you cannot see them, open the screenshot paths "
                                 "with your image-reading tool.")
        return {"_content": [{"type": "text", "text": dumps(result)}, *content]}

    def _resolve(self, who: str) -> str:
        wanted = str(who or "").strip()
        if wanted in ("*", "all", "everyone", "team"):
            return "*"
        for one in self.roster:
            if wanted.casefold() in (str(one.get("id", "")).casefold(), str(one.get("name", "")).casefold()):
                return str(one["id"])
        raise ValueError(f"No teammate called {wanted!r}. Use list_team to see who is here.")

    def _wait_answer(self, question_id: str) -> str | None:
        deadline = time.monotonic() + ASK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            held = self.mailbox.question(question_id)
            if held and held.get("answer") is not None:
                return str(held["answer"])
            time.sleep(0.5)
        return None

    def call(self, name: str, args: dict[str, Any]) -> Any:
        box, team, me = self.mailbox, self.team, self.agent
        if name == "send_message":
            row = box.send(team, me, self._resolve(args.get("to", "*")), str(args.get("text") or ""))
            return {"sent": True, "seq": row["seq"], "to": row["recipient"]}
        if name == "read_messages":
            return box.messages(team, for_agent=me, after=int(args.get("after") or 0))
        if name == "list_team":
            return {"you": me, "team": self.roster}
        if name == "create_task":
            owner = self._resolve(args["owner"]) if args.get("owner") else ""
            return box.create_task(team, me, str(args.get("title") or ""), str(args.get("detail") or ""), owner)
        if name == "update_task":
            owner = self._resolve(args["owner"]) if args.get("owner") else None
            return box.update_task(team, me, str(args.get("task_id") or ""), str(args.get("state") or ""),
                                   reason=str(args.get("closure_reason") or ""), target=str(args.get("target") or ""),
                                   note=str(args.get("note") or ""), owner=owner)
        if name == "list_tasks":
            return box.tasks(team)
        if name == "reserve_paths":
            return box.reserve(team, me, list(args.get("paths") or []))
        if name == "release_paths":
            return {"released": box.release(team, me, list(args.get("paths") or []) or None)}
        if name == "ask_user":
            question = box.ask(team, me, "question", str(args.get("question") or ""))
            answer = self._wait_answer(question)
            return {"answer": answer} if answer is not None else {"answer": None, "note": "No answer yet; continue with your best judgement."}
        if name == "report_result":
            box.report(team, me, str(args.get("status") or "done"), str(args.get("summary") or ""))
            return {"reported": True}
        if name == "check_page":
            return self.check_page(args)
        if name == "approve_action":
            text = json.dumps({"tool": args.get("tool_name"), "input": args.get("input")}, ensure_ascii=False)[:4000]
            question = box.ask(team, me, "approval", text)
            answer = self._wait_answer(question)
            if answer in ("accept", "acceptForSession", "allow", "yes"):
                return {"behavior": "allow", "updatedInput": args.get("input") or {}}
            return {"behavior": "deny", "message": "The user did not approve this action."}
        raise ValueError(f"Unknown tool {name}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        identity = message.get("id")
        if identity is None:
            return None  # notifications (initialized, cancelled)
        if method == "initialize":
            result = {"protocolVersion": (message.get("params") or {}).get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "nexus", "version": "3.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            try:
                value = self.call(str(params.get("name") or ""), dict(params.get("arguments") or {}))
                if isinstance(value, dict) and isinstance(value.get("_content"), list):
                    result = {"content": value["_content"], "isError": False}
                else:
                    text = value if isinstance(value, str) else dumps(value)
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": identity, "result": result}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--roster", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--settings", default="")
    options = parser.parse_args(argv)
    roster = []
    if options.roster:
        try:
            with open(options.roster, encoding="utf-8") as stream:
                roster = json.load(stream)
        except (OSError, ValueError):
            roster = []
    server = Server(Mailbox(options.db), options.team, options.agent, roster, cwd=options.cwd,
                    settings=options.settings)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = server.handle(message) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


if __name__ == "__main__":
    main()
