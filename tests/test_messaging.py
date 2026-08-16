from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from our_harness.agent_tools import (
    TEAM_CAPABILITY,
    TEAM_TOOL_NAMES,
    AgentToolSession,
)
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.graphs import GRAPH_AGENT_CAPABILITIES
from our_harness.memory import MemoryStore
from our_harness.redaction import CredentialRedactor
from our_harness.messaging import EVERYONE, AgentMessage, MessageBoard, summarize, transcript
from our_harness.models import HarnessError


class FakeDeadline:
    def check(self, _label: str) -> None:
        return None

    def remaining_seconds(self, _label: str) -> float:
        return 600.0


class BoardTests(unittest.TestCase):
    def board(self, **limits: object) -> MessageBoard:
        return MessageBoard(["planner", "coder", "reviewer"], **limits)

    def test_a_note_reaches_the_agent_it_names(self) -> None:
        board = self.board()
        board.post("planner", "coder", "Watch the cache", "It keys on the file name.")
        self.assertEqual([item.subject for item in board.inbox("coder")], ["Watch the cache"])
        self.assertEqual(board.inbox("reviewer"), ())

    def test_a_note_to_everyone_reaches_all_but_the_writer(self) -> None:
        board = self.board()
        board.post("coder", EVERYONE, "Done", "I changed the key.")
        self.assertEqual(len(board.inbox("planner")), 1)
        self.assertEqual(len(board.inbox("reviewer")), 1)
        self.assertEqual(board.inbox("coder"), ())

    def test_since_returns_only_what_is_new(self) -> None:
        board = self.board()
        first = board.post("planner", "coder", "One", "a")
        board.post("planner", "coder", "Two", "b")
        found = board.inbox("coder", since=first.sequence)
        self.assertEqual([item.subject for item in found], ["Two"])
        self.assertEqual(board.waiting("coder", first.sequence), 1)
        self.assertEqual(board.waiting("coder", 0), 2)

    def test_the_limit_caps_one_read_without_losing_the_rest(self) -> None:
        board = self.board()
        for index in range(5):
            board.post("planner", "coder", f"Note {index}", "body")
        found = board.inbox("coder", limit=2)
        self.assertEqual(len(found), 2)
        self.assertEqual(board.waiting("coder", found[-1].sequence), 3)

    def test_writing_to_a_stranger_names_the_real_agents(self) -> None:
        board = self.board()
        with self.assertRaises(HarnessError) as caught:
            board.post("planner", "nobody", "Hello", "text")
        message = str(caught.exception)
        self.assertIn("no agent named nobody", message)
        self.assertIn("coder, planner, reviewer", message)
        self.assertIn(EVERYONE, message)

    def test_an_agent_cannot_write_to_itself(self) -> None:
        with self.assertRaises(HarnessError):
            self.board().post("coder", "coder", "Note to self", "text")

    def test_a_stranger_cannot_write_or_read(self) -> None:
        board = self.board()
        with self.assertRaises(HarnessError):
            board.post("outsider", "coder", "Hello", "text")
        with self.assertRaises(HarnessError):
            board.inbox("outsider")

    def test_empty_and_oversized_notes_are_refused(self) -> None:
        board = self.board(max_body_chars=20)
        for subject, body in (("", "text"), ("Subject", ""), ("Subject", "x" * 21)):
            with self.subTest(subject=subject), self.assertRaises(HarnessError):
                board.post("planner", "coder", subject, body)

    def test_the_board_refuses_a_note_past_its_count_limit(self) -> None:
        board = self.board(max_messages=2)
        board.post("planner", "coder", "One", "a")
        board.post("planner", "coder", "Two", "b")
        with self.assertRaises(HarnessError) as caught:
            board.post("planner", "coder", "Three", "c")
        self.assertIn("already written 2 notes", str(caught.exception))
        self.assertEqual(len(board), 2)

    def test_the_board_refuses_a_note_past_its_size_limit(self) -> None:
        board = self.board(max_total_chars=40)
        board.post("planner", "coder", "Subject", "x" * 20)
        with self.assertRaises(HarnessError) as caught:
            board.post("planner", "coder", "Subject", "y" * 20)
        self.assertIn("of 40 characters", str(caught.exception))
        self.assertEqual(len(board), 1)

    def test_no_agent_may_be_called_everyone(self) -> None:
        with self.assertRaises(HarnessError):
            MessageBoard(["planner", EVERYONE])

    def test_parallel_writers_all_get_their_own_number(self) -> None:
        board = MessageBoard([f"agent{index}" for index in range(8)], max_messages=200)
        errors: list[Exception] = []

        def write(index: int) -> None:
            try:
                for round_number in range(10):
                    board.post(f"agent{index}", EVERYONE, f"n{index}-{round_number}", "body")
            except Exception as exc:  # pragma: no cover - only on a real race
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        numbers = [item.sequence for item in board.conversation()]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(len(set(numbers)), 80)

    def test_a_board_survives_a_save_and_restore(self) -> None:
        board = self.board()
        board.post("planner", "coder", "One", "a")
        board.post("coder", EVERYONE, "Two", "b")
        again = MessageBoard.restore(json.loads(json.dumps(board.snapshot())))
        self.assertEqual(again.snapshot(), board.snapshot())
        self.assertEqual(again.last_sequence, 2)
        third = again.post("reviewer", "coder", "Three", "c")
        self.assertEqual(third.sequence, 3)

    def test_a_damaged_snapshot_is_refused(self) -> None:
        board = self.board()
        board.post("planner", "coder", "One", "a")
        good = board.snapshot()
        for change in (
            {"schema_version": 99},
            {"messages": "not a list"},
            {"participants": "not a list"},
            {"sequence": 0},
        ):
            with self.subTest(change=change), self.assertRaises(HarnessError):
                MessageBoard.restore({**good, **change})

    def test_out_of_order_stored_messages_are_refused(self) -> None:
        board = self.board()
        board.post("planner", "coder", "One", "a")
        board.post("planner", "coder", "Two", "b")
        broken = board.snapshot()
        broken["messages"][1]["sequence"] = 1
        with self.assertRaises(HarnessError):
            MessageBoard.restore(broken)

    def test_a_snapshot_over_the_limits_is_refused(self) -> None:
        board = self.board()
        board.post("planner", "coder", "One", "a")
        board.post("planner", "coder", "Two", "b")
        with self.assertRaises(HarnessError):
            MessageBoard.restore(board.snapshot(), max_messages=1)

    def test_summarize_and_transcript_read_plainly(self) -> None:
        board = self.board()
        board.post("planner", "coder", "Watch the cache", "body")
        board.post("coder", EVERYONE, "Fixed", "body")
        text = summarize(board.conversation())
        self.assertIn("planner to coder: Watch the cache", text)
        self.assertIn("coder to everyone: Fixed", text)
        self.assertEqual(summarize(board.conversation(), limit=1).splitlines()[-1], "and 1 more")
        records = transcript(board.conversation())
        self.assertEqual(records[0]["from"], "planner")
        records[0]["subject"] = "changed"
        self.assertEqual(board.conversation()[0].subject, "Watch the cache")


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["memory"]["enabled"] = False
        self.config = LoadedConfig(data, self.root, [], {})
        self.memory = MemoryStore(self.config)
        self.events: list[tuple[str, str, dict]] = []
        self.session = AgentToolSession(
            self.config,
            self.memory,
            FakeDeadline(),
            lambda kind, node, payload: self.events.append((kind, node, payload)),
        )
        self.board = MessageBoard(["planner", "coder", "reviewer"])
        self.addCleanup(self.memory.close)
        self.addCleanup(self.temporary.cleanup)

    def call(self, node: str, name: str, arguments: dict, call_id: str = "") -> dict:
        result = self.session.execute(node, call_id or f"{node}-{name}-{self.session.calls}", name, arguments)
        return result["content"]

    def content(self, raw: str) -> dict:
        return json.loads(raw)

    def test_the_tools_appear_only_with_a_board_and_the_capability(self) -> None:
        names = {item["name"] for item in self.session.definitions("planner", {"workspace.read"})}
        self.assertFalse(names & TEAM_TOOL_NAMES)
        self.session.attach_message_board(self.board)
        without = {item["name"] for item in self.session.definitions("planner", {"workspace.read"})}
        self.assertFalse(without & TEAM_TOOL_NAMES)
        with_capability = {
            item["name"]
            for item in self.session.definitions("planner", {"workspace.read", TEAM_CAPABILITY})
        }
        self.assertEqual(with_capability & TEAM_TOOL_NAMES, set(TEAM_TOOL_NAMES))

    def test_every_agent_kind_is_allowed_to_talk(self) -> None:
        for kind, allowed in GRAPH_AGENT_CAPABILITIES.items():
            with self.subTest(kind=kind):
                self.assertIn(TEAM_CAPABILITY, allowed)

    def test_a_note_sent_by_one_agent_is_read_by_another(self) -> None:
        self.session.attach_message_board(self.board)
        sent = self.content(self.call("planner", "send_message", {
            "to": "coder", "subject": "Watch the cache", "body": "It keys on the file name.",
        }))
        self.assertTrue(sent["delivered"])
        self.assertEqual(sent["sequence"], 1)
        read = self.content(self.call("coder", "read_messages", {"since": 0, "max_results": 10}))
        self.assertEqual(read["messages"][0]["subject"], "Watch the cache")
        self.assertEqual(read["messages"][0]["from"], "planner")
        self.assertEqual(read["last_sequence"], 1)
        self.assertEqual(read["still_waiting"], 0)
        self.assertIn(EVERYONE, read["agents"])

    def test_a_second_read_with_the_same_arguments_sees_new_notes(self) -> None:
        """The tool cache must not hide a note that arrived in between."""

        self.session.attach_message_board(self.board)
        first = self.content(self.call("coder", "read_messages", {"since": 0, "max_results": 10}, "read-1"))
        self.assertEqual(first["messages"], [])
        self.call("planner", "send_message", {"to": "coder", "subject": "New", "body": "text"})
        second = self.content(self.call("coder", "read_messages", {"since": 0, "max_results": 10}, "read-2"))
        self.assertEqual([item["subject"] for item in second["messages"]], ["New"])

    def test_still_waiting_tells_the_reader_there_is_more(self) -> None:
        self.session.attach_message_board(self.board)
        for index in range(4):
            self.call("planner", "send_message", {"to": "coder", "subject": f"N{index}", "body": "b"})
        read = self.content(self.call("coder", "read_messages", {"since": 0, "max_results": 2}))
        self.assertEqual(len(read["messages"]), 2)
        self.assertEqual(read["still_waiting"], 2)

    def test_sending_without_a_board_is_refused(self) -> None:
        answer = self.content(self.call("planner", "send_message", {"to": "coder", "subject": "s", "body": "b"}))
        self.assertIn("no message board", answer["error"])

    def test_a_bad_recipient_comes_back_as_a_readable_error(self) -> None:
        self.session.attach_message_board(self.board)
        answer = self.content(self.call("planner", "send_message", {"to": "ghost", "subject": "s", "body": "b"}))
        self.assertIn("no agent named ghost", answer["error"])

    def test_unknown_fields_are_refused(self) -> None:
        self.session.attach_message_board(self.board)
        answer = self.content(self.call("planner", "send_message", {
            "to": "coder", "subject": "s", "body": "b", "run": "rm -rf /",
        }))
        self.assertIn("error", answer)

    def test_every_note_shows_up_as_a_run_event(self) -> None:
        self.session.attach_message_board(self.board)
        self.call("planner", "send_message", {"to": "coder", "subject": "Watch out", "body": "text"})
        self.call("coder", "read_messages", {"since": 0, "max_results": 5})
        kinds = [kind for kind, _node, _payload in self.events]
        self.assertIn("agent_message", kinds)
        self.assertIn("agent_message_read", kinds)
        sent = next(payload for kind, _node, payload in self.events if kind == "agent_message")
        self.assertEqual(sent["from"], "planner")
        self.assertEqual(sent["to"], "coder")
        self.assertEqual(sent["subject"], "Watch out")
        self.assertNotIn("body", sent)

    def test_a_note_is_never_marked_as_a_project_change(self) -> None:
        self.session.attach_message_board(self.board)
        result = self.session.execute("planner", "call-1", "send_message", {
            "to": "coder", "subject": "s", "body": "b",
        })
        self.assertTrue(result["provenance"]["read_only"])
        self.assertTrue(result["provenance"]["untrusted_data"])


class RunWiringTests(unittest.TestCase):
    def test_the_board_holds_exactly_the_agents_in_the_graph(self) -> None:
        from our_harness.workflow import _message_board

        graph = {
            "schema_version": 2,
            "name": "demo",
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "planner", "type": "planner"},
                {"id": "coder", "type": "coder"},
                {"id": "checks", "type": "tool"},
                {"id": "reviewer", "type": "evaluator"},
                {"id": "end", "type": "end"},
            ],
            "edges": [],
        }
        board = _message_board(graph)
        self.assertEqual(board.participants, frozenset({"planner", "coder", "reviewer"}))

    def test_a_snapshot_from_another_graph_is_not_reused(self) -> None:
        from our_harness.workflow import _message_board

        graph = {
            "schema_version": 2, "name": "demo", "entry": "start",
            "nodes": [{"id": "planner", "type": "planner"}, {"id": "coder", "type": "coder"}],
            "edges": [],
        }
        stale = MessageBoard(["planner", "someone-else"])
        stale.post("planner", "someone-else", "Old", "note")
        fresh = _message_board(graph, stale.snapshot())
        self.assertEqual(fresh.participants, frozenset({"planner", "coder"}))
        self.assertEqual(len(fresh), 0)

    def test_a_matching_snapshot_keeps_the_conversation(self) -> None:
        from our_harness.workflow import _message_board

        graph = {
            "schema_version": 2, "name": "demo", "entry": "start",
            "nodes": [{"id": "planner", "type": "planner"}, {"id": "coder", "type": "coder"}],
            "edges": [],
        }
        board = _message_board(graph)
        board.post("planner", "coder", "Keep me", "note")
        resumed = _message_board(graph, board.snapshot())
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed.conversation()[0].subject, "Keep me")


class DurableTests(unittest.TestCase):
    """Notes must still be readable after the run that wrote them has finished."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def test_notes_are_kept_per_run_and_read_back_in_order(self) -> None:
        with MemoryStore(self.config) as memory:
            first = memory.start_run("first")
            second = memory.start_run("second")
            memory.append_event(first, "agent_message", "planner", {
                "sequence": 1, "from": "planner", "to": "coder", "subject": "Watch the cache",
            })
            memory.append_event(first, "tool_result", "coder", {"name": "read_messages"})
            memory.append_event(second, "agent_message", "coder", {
                "sequence": 1, "from": "coder", "to": EVERYONE, "subject": "Done",
            })
            everything = memory.agent_conversation()
            self.assertEqual([item["subject"] for item in everything], ["Watch the cache", "Done"])
            self.assertEqual([item["to"] for item in everything], ["coder", EVERYONE])
            self.assertEqual([item["subject"] for item in memory.agent_conversation(first)], ["Watch the cache"])
            self.assertEqual([item["subject"] for item in memory.agent_conversation("", 1)], ["Done"])

    def test_a_damaged_event_is_skipped_rather_than_breaking_the_list(self) -> None:
        with MemoryStore(self.config) as memory:
            run = memory.start_run("demo")
            memory.append_event(run, "agent_message", "planner", {
                "sequence": 1, "from": "planner", "to": "coder", "subject": "Good",
            })
            memory.connection.execute(
                "UPDATE events SET payload_json='not json' WHERE kind='agent_message'"
            )
            self.assertEqual(memory.agent_conversation(run), [])

    def test_the_control_panel_serves_the_notes(self) -> None:
        import http.client
        import threading

        from our_harness.server import HarnessHTTPServer

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        config = LoadedConfig(data, self.root, [], {})
        with MemoryStore(config) as memory:
            run = memory.start_run("demo")
            memory.append_event(run, "agent_message", "planner", {
                "sequence": 1, "from": "planner", "to": "coder", "subject": "Watch the cache",
            })
        server = HarnessHTTPServer(("127.0.0.1", 0), config)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            connection.request("GET", "/api/team", headers={
                "Host": f"127.0.0.1:{server.server_port}", "X-Harness-Token": server.token,
            })
            answer = connection.getresponse()
            body = json.loads(answer.read())
            self.assertEqual(answer.status, 200)
            self.assertEqual([item["subject"] for item in body["notes"]], ["Watch the cache"])

            connection.request("GET", "/api/team", headers={"Host": f"127.0.0.1:{server.server_port}"})
            self.assertEqual(connection.getresponse().status, 400)
        finally:
            connection.close()


class TimelineTests(unittest.TestCase):
    """The history view needs a step list a person can read."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def test_each_visit_to_a_node_becomes_its_own_step(self) -> None:
        with MemoryStore(self.config) as memory:
            run = memory.start_run("Set VALUE to 2")
            for kind, node in (
                ("node_start", "planner"), ("success", "planner"),
                ("node_start", "coder"), ("failure", "coder"),
                ("node_start", "coder"), ("success", "coder"),
                ("node_start", "reviewer"), ("success", "reviewer"),
            ):
                memory.append_event(run, kind, node, {})
            timeline = memory.run_timeline()
        self.assertEqual(len(timeline), 1)
        steps = timeline[0]["steps"]
        self.assertEqual([step["node"] for step in steps], ["planner", "coder", "coder", "reviewer"])
        self.assertEqual([step["result"] for step in steps], ["passed", "failed", "passed", "passed"])
        self.assertEqual(timeline[0]["task"], "Set VALUE to 2")

    def test_newest_runs_come_first_and_the_count_is_capped(self) -> None:
        with MemoryStore(self.config) as memory:
            for index in range(4):
                run = memory.start_run(f"task {index}")
                memory.append_event(run, "node_start", "planner", {})
            timeline = memory.run_timeline(limit=2)
        self.assertEqual(len(timeline), 2)
        self.assertEqual([item["task"] for item in timeline], ["task 3", "task 2"])

    def test_a_run_with_no_events_still_appears(self) -> None:
        with MemoryStore(self.config) as memory:
            memory.start_run("nothing happened")
            timeline = memory.run_timeline()
        self.assertEqual(timeline[0]["steps"], [])
        self.assertEqual(timeline[0]["task"], "nothing happened")

    def test_durations_are_never_negative(self) -> None:
        with MemoryStore(self.config) as memory:
            run = memory.start_run("demo")
            memory.append_event(run, "node_start", "planner", {})
            memory.connection.execute("UPDATE runs SET updated_at=0 WHERE id=?", (run,))
            timeline = memory.run_timeline()
        self.assertGreaterEqual(timeline[0]["steps"][0]["duration_ms"], 0)
        self.assertGreaterEqual(timeline[0]["duration_ms"], 0)

    def test_the_control_panel_serves_the_timeline(self) -> None:
        import http.client
        import threading

        from our_harness.server import HarnessHTTPServer

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        config = LoadedConfig(data, self.root, [], {})
        with MemoryStore(config) as memory:
            run = memory.start_run("Set VALUE to 2")
            memory.append_event(run, "node_start", "planner", {})
        server = HarnessHTTPServer(("127.0.0.1", 0), config)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            connection.request("GET", "/api/timeline", headers={
                "Host": f"127.0.0.1:{server.server_port}", "X-Harness-Token": server.token,
            })
            answer = connection.getresponse()
            body = json.loads(answer.read())
            self.assertEqual(answer.status, 200)
            self.assertEqual(body["runs"][0]["task"], "Set VALUE to 2")

            connection.request("GET", "/api/timeline", headers={"Host": f"127.0.0.1:{server.server_port}"})
            self.assertEqual(connection.getresponse().status, 400)
        finally:
            connection.close()


class EndToEndTests(unittest.TestCase):
    """One real run in which the planner writes a note and the coder reads it."""

    def test_two_agents_talk_during_a_real_run(self) -> None:
        from our_harness.config import load_config
        from our_harness.workflow import HarnessApplication

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")

            plan = {
                "summary": "Set value",
                "requirement_ledger": [
                    {
                        "id": "R1", "requirement": "value is 2", "category": "behavior",
                        "counterexample": "value() should return 2",
                    },
                ],
                "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": [],
            }
            note = {
                "action": "tool",
                "tool": {
                    "call_id": "planner-note-1",
                    "name": "send_message",
                    "arguments": {
                        "to": "coder",
                        "subject": "VALUE is read by the report",
                        "body": "Keep the public name VALUE; the report reads it by that name.",
                    },
                },
            }
            read = {
                "action": "tool",
                "tool": {
                    "call_id": "coder-read-1",
                    "name": "read_messages",
                    "arguments": {"since": 0, "max_results": 5},
                },
            }
            source_hash = __import__("hashlib").sha256(b"VALUE = 1\n").hexdigest()
            proposal = {
                "summary": "Set value",
                "changes": [{
                    "path": "value.py", "baseline_sha256": source_hash,
                    "content": "VALUE = 2\n", "delete": False, "reason": "requested value",
                }],
                "commands": [],
                "review": {
                    "verdict": "PASS",
                    "findings": [{
                        "requirement_id": "R1",
                        "file": "value.py",
                        "code_path": "VALUE assignment",
                        "counterexample_result": "R1 observes VALUE equal to 2",
                    }],
                },
                "memory": [],
            }
            def role_of(format_name: str) -> str:
                if "planner" in format_name:
                    return "planner"
                if "coder" in format_name:
                    return "coder"
                return "other"

            class TalkingProvider:
                """Answers by role: each agent writes or reads one note, then answers."""

                def __init__(self) -> None:
                    self.calls: dict[str, int] = {}
                    self.formats: list[str] = []
                    self.prompts: list[str] = []

                def complete(self, _request):
                    raise AssertionError("The workflow must use the streaming boundary")

                def stream(self, request):
                    name = request.response_format.name if request.response_format else ""
                    self.formats.append(name)
                    parts = [str(request.system_prefix), str(request.dynamic_context)]
                    parts.extend(str(item.get("content", "")) for item in request.messages)
                    role = role_of(name)
                    self.prompts.append((role, "\n".join(parts)))
                    turn = self.calls.get(role, 0)
                    self.calls[role] = turn + 1
                    if role == "planner":
                        answer = note if turn == 0 else {"action": "final", "result": plan}
                    elif role == "coder":
                        answer = read if turn == 0 else {"action": "final", "result": proposal}
                    else:
                        answer = {"action": "final", "result": {}}
                    body = json.dumps(answer)
                    yield {"type": "text_delta", "text": body}
                    yield {"type": "done", "finish_reason": "stop"}

            provider = TalkingProvider()
            events: list[tuple[str, dict]] = []
            with HarnessApplication(load_config(root), lambda event: events.append((event.get("kind", ""), event))) as app:
                app.provider = provider
                app.run_task("Set VALUE to 2", dry_run=True)

            sent = [payload for kind, payload in events if kind == "agent_message"]
            self.assertTrue(sent, "the planner's note never reached the run log")
            self.assertEqual(sent[0]["payload"]["from"], "planner")
            self.assertEqual(sent[0]["payload"]["to"], "coder")
            self.assertEqual(sent[0]["payload"]["subject"], "VALUE is read by the report")

            delivered = [payload for kind, payload in events if kind == "agent_message_read"]
            self.assertTrue(delivered, "the coder never read its notes")
            self.assertEqual(delivered[0]["payload"]["delivered"], 1)

            # The board, not the fake provider, is what carried the words across.
            # The planner sent this text; the coder never had it in any prompt,
            # and the provider never writes it into a read result.
            results = [
                payload for kind, payload in events
                if kind == "tool_result" and payload.get("payload", {}).get("name") == "read_messages"
            ]
            self.assertTrue(results)
            answer = json.loads(results[0]["payload"]["content"])
            self.assertEqual(len(answer["messages"]), 1)
            carried = answer["messages"][0]
            self.assertEqual(carried["from"], "planner")
            self.assertEqual(carried["to"], "coder")
            self.assertEqual(carried["subject"], "VALUE is read by the report")
            self.assertEqual(
                carried["body"], "Keep the public name VALUE; the report reads it by that name."
            )
            self.assertEqual(carried["sequence"], 1)
            self.assertEqual(answer["still_waiting"], 0)

            # Nothing handed the planner's words to the coder. Its first prompt,
            # before it called any tool, does not hold them. They appear only
            # afterwards, inside the tool transcript the board answered with.
            coder_prompts = [text for role, text in provider.prompts if role == "coder"]
            self.assertTrue(coder_prompts)
            self.assertNotIn(
                "Keep the public name VALUE", coder_prompts[0],
                "the note reached the coder through its prompt, not through the board",
            )
            self.assertIn("Keep the public name VALUE", coder_prompts[-1])
            # Note content is labelled as data the agent must not obey.
            self.assertIn("TOOL TRANSCRIPT (UNTRUSTED DATA)", coder_prompts[-1])

    def test_a_secret_in_a_note_never_reaches_the_other_agent(self) -> None:
        board = MessageBoard(["planner", "coder"], redact=CredentialRedactor().text)
        message = board.post(
            "planner", "coder", "Use this key",
            "The token is sk-abcdefghijklmnopqrstuvwxyz012345 and the password: hunter2",
        )
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", message.body)
        self.assertNotIn("hunter2", message.body)
        self.assertIn("REDACTED", message.body)
        read = board.inbox("coder")[0]
        self.assertEqual(read.body, message.body)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", json.dumps(board.snapshot()))

    def test_a_note_that_is_only_a_secret_is_refused_rather_than_stored_empty(self) -> None:
        board = MessageBoard(["planner", "coder"], redact=lambda _value: "")
        with self.assertRaises(HarnessError):
            board.post("planner", "coder", "Subject", "sk-secret")


if __name__ == "__main__":
    unittest.main()
