"""Live team: agents see their pages, keep windows off the user's screen, and get attached files.

The owner's report behind these: a two-agent Live team "looped 5 times" on a
Three.js game and called it done while it was broken when opened from disk.
The agents never saw the page; their only "check" was Start-Process
index.html, which opened a tab in the user's browser every time. Attached
screenshots could not be sent at all. Everything here runs on temporary
folders and fake sessions: no real CLI, browser or account.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness.agent_runtime import browser_guard
from our_harness.agent_runtime.claude_session import ClaudeSession
from our_harness.agent_runtime.mailbox import Mailbox
from our_harness.agent_runtime.manager import RuntimeManager
from our_harness.agent_runtime.mcp_server import Server
from our_harness.agent_runtime.team import STANDING_RULES, TeamRun
from our_harness.models import HarnessError
from tests.test_agent_runtime_v3 import FAKE_CLAUDE, FakeSession, Fixture, wait_for

SOURCE = Path(__file__).resolve().parents[1] / "src"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class BrowserGuardTests(unittest.TestCase):
    def test_commands_that_open_the_users_browser_or_windows_are_recognised(self):
        opening = [
            'Start-Process "C:\\work\\index.html"',
            'Start-Sleep -Seconds 2; Start-Process "http://localhost:8000/index.html"',
            'Start-Process python -ArgumentList "-m http.server 8000" -WindowStyle Normal',
            "Start-Process python -ArgumentList '-m http.server 8000'",  # a new console window by default
            "start chrome", "cmd /c start http://127.0.0.1:3000", "open index.html", "xdg-open http://a.test",
            "Invoke-Item .\\game\\index.html", "explorer.exe http://localhost:3000",
            'python -c "import webbrowser; webbrowser.open(\'http://x\')"', "npx playwright test --headed",
        ]
        quiet = [
            "Start-Process python -ArgumentList '-m http.server 8000' -WindowStyle Hidden",
            "Start-Process node server.js -NoNewWindow", "npm start", "python -m http.server 8000",
            "curl -I http://localhost:8000/index.html", "Get-Content index.html", "cat index.html", "ls -la",
            'git commit -m "update the index.html file"', "code index.html",
        ]
        for command in opening:
            with self.subTest(command=command):
                self.assertTrue(browser_guard.opens_window(command))
        for command in quiet:
            with self.subTest(command=command):
                self.assertFalse(browser_guard.opens_window(command))

    def test_hidden_denies_with_the_alternative_visible_allows_and_bad_settings_mean_hidden(self):
        call = {"tool_name": "PowerShell", "tool_input": {"command": 'Start-Process "http://localhost:8000"'}}
        denied = browser_guard.decide(call, "hidden")["hookSpecificOutput"]
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("check_page", denied["permissionDecisionReason"])
        self.assertIsNone(browser_guard.decide(call, "visible"))
        self.assertIsNone(browser_guard.decide({"tool_input": {"command": "npm test"}}, "hidden"))
        self.assertIsNone(browser_guard.decide({"tool_input": "not a dict"}, "hidden"))
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            self.assertEqual(browser_guard.read_mode(settings), "hidden")  # Missing file.
            settings.write_text("{broken", encoding="utf-8")
            self.assertEqual(browser_guard.read_mode(settings), "hidden")
            settings.write_text('{"browser": "sideways"}', encoding="utf-8")
            self.assertEqual(browser_guard.read_mode(settings), "hidden")
            settings.write_text('{"browser": "visible"}', encoding="utf-8")
            self.assertEqual(browser_guard.read_mode(settings), "visible")

    def test_the_hook_command_runs_as_claude_code_calls_it(self):
        with tempfile.TemporaryDirectory(prefix="guard with space ") as folder:
            settings = Path(folder) / "settings.json"
            settings.write_text('{"browser": "hidden"}', encoding="utf-8")
            hooks = browser_guard.hook_settings(settings, sys.executable)["hooks"]["PreToolUse"][0]
            self.assertEqual(hooks["matcher"], "Bash|PowerShell")
            command = hooks["hooks"][0]["command"]
            self.assertNotIn("\\", command, "forward slashes work in both bash and cmd")
            env = {**os.environ, "PYTHONPATH": str(SOURCE)}
            call = json.dumps({"tool_name": "Bash", "tool_input": {"command": "open http://localhost:5173"}})
            done = subprocess.run(command, shell=True, input=call, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            settings.write_text('{"browser": "visible"}', encoding="utf-8")  # A switch applies to the next call.
            done = subprocess.run(command, shell=True, input=call, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual((done.returncode, done.stdout), (0, ""))
            done = subprocess.run(command, shell=True, input="not json", capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual((done.returncode, done.stdout), (0, ""), "a malformed call never breaks the tool")


class ClaudeArgvTests(Fixture):
    def test_claude_gets_the_guard_and_may_read_attachments(self):
        script = self.script("fake_claude.py", FAKE_CLAUDE)
        record = self.root / "argv.json"
        settings = self.root / "team settings.json"
        spec = self.spec("claude-cli", script, extra_dirs=[str(self.root / "attachments")],
                         browser_settings=str(settings))
        session = ClaudeSession(spec, self.events.append, executable=sys.executable,
                                env={**os.environ, "FAKE_RECORD": str(record)})
        self.addCleanup(session.close)
        session.start()
        session.send("hi")
        self.assertTrue(wait_for(lambda: session.turns_completed == 1))
        argv = json.loads(record.read_text())
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.root / "attachments"))
        hooks = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("our_harness.agent_runtime.browser_guard", command)
        self.assertIn(str(settings).replace("\\", "/"), command)

    @unittest.skipUnless(os.name == "nt", "cmd.exe launchers exist only on Windows")
    def test_quotes_in_the_rules_survive_an_npm_style_cmd_launcher(self):
        # npm installs Claude as claude.cmd. cmd.exe re-reads its arguments, and a
        # double quote in the rules used to drop the nexus tools and the guard.
        script = self.script("fake_claude.py", FAKE_CLAUDE)
        launcher = self.root / "claude.cmd"
        launcher.write_text(f'@ECHO off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        record = self.root / "argv.json"
        rules = 'Say "loop 5 times" & use a|b <name> (for example) 100% ^done'
        spec = self.spec("claude-cli", script, instructions=rules, state_dir=str(self.root / "state"),
                         browser_settings=str(self.root / "settings.json"),
                         mcp={"command": ["py", "-m", "srv", "--cwd", str(self.root)], "env": {"PYTHONPATH": "x"}})
        spec.command = None
        session = ClaudeSession(spec, self.events.append, executable=str(launcher),
                                env={**os.environ, "FAKE_RECORD": str(record)})
        self.addCleanup(session.close)
        session.start()
        session.send("hi")
        self.assertTrue(wait_for(lambda: session.turns_completed == 1), session.last_error())
        argv = json.loads(record.read_text())
        self.assertEqual(Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8"), rules)
        mcp = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(mcp["mcpServers"]["nexus"]["args"][-1], str(self.root))
        self.assertTrue(Path(argv[argv.index("--settings") + 1]).is_file())
        self.assertTrue(all(Path(one).parent == self.root / "state" for one in
                            (argv[argv.index(flag) + 1] for flag in ("--append-system-prompt-file", "--mcp-config", "--settings"))))

    def test_without_a_team_setting_nothing_is_added(self):
        script = self.script("fake_claude.py", FAKE_CLAUDE)
        record = self.root / "argv.json"
        session = ClaudeSession(self.spec("claude-cli", script), self.events.append, executable=sys.executable,
                                env={**os.environ, "FAKE_RECORD": str(record)})
        self.addCleanup(session.close)
        session.start()
        session.send("hi")
        self.assertTrue(wait_for(lambda: session.turns_completed == 1))
        argv = json.loads(record.read_text())
        self.assertNotIn("--settings", argv)
        self.assertNotIn("--add-dir", argv)


class CheckPageToolTests(Fixture):
    def server(self, mode):
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"browser": mode}), encoding="utf-8")
        (self.root / "site").mkdir()
        (self.root / "site" / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
        return Server(Mailbox(self.root / "team.sqlite3"), "t", "a", [], cwd=str(self.root / "site"),
                      settings=str(settings))

    def fake_preview(self, calls):
        def preview(root, page, *, wait_ms, headless):
            calls.append({"root": Path(root), "page": page, "wait_ms": wait_ms, "headless": headless})
            shots = Path(root) / ".harness" / "previews"
            shots.mkdir(parents=True, exist_ok=True)
            (shots / "index-file.png").write_bytes(PNG)
            (shots / "index-http.png").write_bytes(PNG)
            return {"verdict": "Opened from disk (how the user opens it): PROBLEM - 1 script errors.",
                    "pages": [{"mode": "file", "screenshot": ".harness/previews/index-file.png", "errors": ["CORS"]},
                              {"mode": "http", "screenshot": ".harness/previews/index-http.png", "errors": []}]}
        return preview

    def test_the_agent_gets_the_verdict_and_sees_both_screenshots_in_a_hidden_browser(self):
        server = self.server("hidden")
        listed = [one["name"] for one in server.handle({"id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("check_page", listed)
        calls = []
        with mock.patch("our_harness.web_preview.preview", self.fake_preview(calls)):
            reply = server.handle({"id": 2, "method": "tools/call", "params": {"name": "check_page", "arguments": {}}})
        result = reply["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(calls[0]["page"], "index.html")
        self.assertTrue(calls[0]["headless"])
        report = json.loads(result["content"][0]["text"])
        self.assertIn("PROBLEM", report["verdict"])
        self.assertTrue(Path(report["pages"][0]["screenshot"]).is_absolute())
        images = [one for one in result["content"] if one["type"] == "image"]
        self.assertEqual(len(images), 2)
        self.assertEqual(base64.b64decode(images[0]["data"]), PNG)
        self.assertEqual(images[0]["mimeType"], "image/png")

    def test_visible_mode_shows_the_browser_and_absolute_pages_outside_the_folder_work(self):
        server = self.server("visible")
        elsewhere = self.root / "other" / "game.html"
        elsewhere.parent.mkdir()
        elsewhere.write_text("x", encoding="utf-8")
        calls = []
        with mock.patch("our_harness.web_preview.preview", self.fake_preview(calls)):
            server.handle({"id": 3, "method": "tools/call", "params": {"name": "check_page",
                                                                        "arguments": {"page": str(elsewhere), "wait_ms": "soon"}}})
        self.assertFalse(calls[0]["headless"])
        self.assertEqual((calls[0]["root"], calls[0]["page"]), (elsewhere.parent.resolve(), "game.html"))
        self.assertEqual(calls[0]["wait_ms"], 2500, "a wrong wait is forgiven, not an error")

    def test_a_missing_browser_is_an_error_the_agent_can_read(self):
        server = self.server("hidden")
        with mock.patch("our_harness.web_preview.preview", side_effect=HarnessError("bundled browser is not available")):
            reply = server.handle({"id": 4, "method": "tools/call", "params": {"name": "check_page", "arguments": {}}})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("not available", reply["result"]["content"][0]["text"])


class TeamTests(Fixture):
    def team(self, agents=None, **fields):
        sessions = {}

        def factory(spec, on_event, approver):
            sessions[spec.agent_id] = FakeSession(spec, on_event, approver)
            return sessions[spec.agent_id]
        agents = agents or [{"id": "codex", "name": "GPT Codex", "kind": "codex-cli"},
                            {"id": "claude", "name": "Claude", "kind": "claude-cli"}]
        team = TeamRun(team_id="team1", name="T", goal="g", project=str(self.root), agents=agents, lead="codex",
                       access="full", mode="shared", root=self.root / "runtime", session_factory=factory, **fields)
        self.addCleanup(team.close)
        team.start()
        return team, sessions

    def test_the_rules_ask_for_real_checks_and_the_briefing_splits_work_before_editing(self):
        for words in ("check_page", "A round without a fresh look does not count", "double-clicked",
                      "do not edit it", "No hype, no emoji", "Start-Process"):
            self.assertIn(words, STANDING_RULES)
        team, sessions = self.team()
        team.kickoff("Make a game. Loop 5 times.")
        lead, member = sessions["codex"].received[0], sessions["claude"].received[0]
        self.assertIn("parts that do not edit the same files", lead)
        self.assertIn("check the combined result yourself", lead)
        self.assertIn("GPT Codex leads and will send you your part", member)
        self.assertIn("do not start editing", member)
        self.assertIn("Browser checks: hidden", member)
        spec = sessions["claude"].spec
        self.assertEqual(spec.instructions, STANDING_RULES)
        mcp = spec.mcp["command"]
        self.assertEqual(mcp[mcp.index("--cwd") + 1], str(self.root.resolve()))
        self.assertEqual(Path(mcp[mcp.index("--settings") + 1]), team.settings_path())
        self.assertEqual(spec.browser_settings, str(team.settings_path()))
        self.assertTrue(Path(spec.extra_dirs[0]).is_dir())

    def test_a_solo_agent_is_not_told_to_wait_for_a_lead(self):
        team, sessions = self.team(agents=[{"id": "codex", "name": "GPT Codex", "kind": "codex-cli"}])
        team.kickoff("Fix it")
        self.assertIn("You work alone", sessions["codex"].received[0])
        self.assertNotIn("do not start editing", sessions["codex"].received[0])

    def test_switching_browser_checks_applies_at_once_and_tells_each_agent_once(self):
        team, sessions = self.team()
        self.assertEqual(json.loads(team.settings_path().read_text())["browser"], "hidden")
        team.set_browser("visible")
        self.assertEqual(browser_guard.read_mode(team.settings_path()), "visible")
        self.assertEqual(team.snapshot()["browser"], "visible")
        self.assertEqual(json.loads(team.record_path().read_text())["browser"], "visible")
        team.say("next step", "claude")
        self.assertTrue(sessions["claude"].received[-1].startswith("[Nexus: Browser checks are now visible"))
        self.assertTrue(sessions["claude"].received[-1].endswith("next step"))
        team.say("and another", "claude")
        self.assertEqual(sessions["claude"].received[-1], "and another")
        team.mailbox.send("team1", "claude", "codex", "done with mine")  # Teammate delivery carries it too.
        self.assertTrue(wait_for(lambda: len(sessions["codex"].received) == 1))
        self.assertIn("Browser checks are now visible", sessions["codex"].received[0])
        with self.assertRaises(ValueError):
            team.set_browser("sideways")
        self.assertTrue(any("Browser checks are now visible" in str(e.get("text")) for e in team.events()))

    def test_attached_files_are_saved_with_safe_names_and_listed_for_the_agents(self):
        team, _ = self.team()
        saved = team.attach([{"name": "../../evil name?.png", "type": "image/png",
                              "data": "data:image/png;base64," + base64.b64encode(PNG).decode()},
                             {"name": "notes.txt", "type": "text/plain", "data": base64.b64encode(b"hello").decode()}])
        self.assertEqual(len(saved), 2)
        first = Path(saved[0]["path"])
        self.assertEqual(first.parent, team.root / "attachments")
        self.assertTrue(first.name.endswith("evil name_.png"))
        self.assertEqual(first.read_bytes(), PNG)
        text = team.with_attachments("Make it look like this", saved)
        self.assertTrue(text.startswith("Make it look like this\n\nThe user attached 2 files."))
        self.assertIn(str(first), text)
        self.assertIn("Please look at the attached files.", team.with_attachments("", saved[:1]))
        self.assertEqual(team.with_attachments("plain", []), "plain")
        with self.assertRaisesRegex(ValueError, "at most 10"):
            team.attach([{"name": "x", "data": ""}] * 11)
        with mock.patch("our_harness.agent_runtime.team.MOST_ATTACHMENT_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "larger than"):
                team.attach([{"name": "x", "data": base64.b64encode(b"four").decode()}])


class ManagerTests(Fixture):
    def manager(self):
        board = {"agents": [{"id": "codex", "name": "GPT Codex", "who": "codex"},
                            {"id": "claude", "name": "Claude", "who": "claude"}]}
        config = {"providers": {"codex": {"kind": "codex-cli"}, "claude": {"kind": "claude-cli"}}}
        created = {}

        def factory(spec, on_event, approver):
            created[spec.agent_id] = FakeSession(spec, on_event, approver)
            return created[spec.agent_id]
        manager = RuntimeManager(root=self.root / "v3", board=lambda: board, config=lambda: config,
                                 project_root=lambda: self.root, session_factory=factory)
        self.addCleanup(manager.close_all)
        return manager, created

    def file(self, name="shot.png", data=PNG):
        return {"name": name, "type": "image/png", "data": base64.b64encode(data).decode()}

    def test_a_goal_can_carry_pictures_and_messages_can_be_files_only(self):
        manager, created = self.manager()
        snapshot = manager.create({"agents": ["codex", "claude"], "goal": "Make it look like this",
                                   "attachments": [self.file()]})
        team_id = snapshot["team_id"]
        self.assertTrue(wait_for(lambda: created.get("claude") and created["claude"].received))
        briefing = created["claude"].received[0]
        self.assertIn("Make it look like this\n\nThe user attached 1 file.", briefing)
        path = briefing.split("\n- ")[1].split(" (")[0]
        self.assertEqual(Path(path).read_bytes(), PNG)
        manager.say(team_id, "", "codex", [self.file("second.png")])
        self.assertTrue(wait_for(lambda: any("second.png" in one for one in created["codex"].received[1:])))
        with self.assertRaisesRegex(HarnessError, "Type a message"):
            manager.say(team_id, "  ", "", [])
        with self.assertRaisesRegex(HarnessError, "at most"):
            manager.say(team_id, "x", "", [self.file()] * 11)
        # Only files, no words, still starts a team.
        other = manager.create({"agents": ["codex"], "attachments": [self.file()]})
        self.assertEqual(other["name"], "Team")

    def test_the_browser_choice_is_remembered_for_new_and_reopened_teams(self):
        manager, created = self.manager()
        self.assertEqual(manager.settings()["browser"], "hidden")
        first = manager.create({"agents": ["codex"], "goal": "A"})
        self.assertTrue(wait_for(lambda: manager.team(first["team_id"]).state == "running"))
        self.assertEqual(manager.set_browser("visible", first["team_id"])["browser"], "visible")
        self.assertEqual(manager.team(first["team_id"]).browser, "visible")
        second = manager.create({"agents": ["codex"], "goal": "B"})
        self.assertEqual(second["browser"], "visible")
        third = manager.create({"agents": ["codex"], "goal": "C", "browser": "hidden"})
        self.assertEqual(third["browser"], "hidden")
        with self.assertRaisesRegex(HarnessError, "hidden or visible"):
            manager.set_browser("loud")
        # Restart: the standing choice and each team's own choice survive.
        manager.close_all()
        restarted = RuntimeManager(root=self.root / "v3", board=manager.board, config=manager.config,
                                   project_root=lambda: self.root, session_factory=manager.session_factory)
        self.addCleanup(restarted.close_all)
        self.assertEqual(restarted.settings()["browser"], "visible")
        again = restarted.reopen(third["team_id"], "carry on", "", [self.file("later.png")])
        self.assertEqual(again["browser"], "hidden")
        self.assertTrue(wait_for(lambda: any("later.png" in one for one in created["codex"].received)))
        # A team saved before browser checks existed reopens with the standing choice.
        record = self.root / "v3" / first["team_id"] / "team.json"
        value = json.loads(record.read_text())
        value.pop("browser")
        record.write_text(json.dumps(value))
        self.assertEqual(restarted.reopen(first["team_id"])["browser"], "visible")
        # An unknown settings schema is not trusted.
        (self.root / "v3" / "settings.json").write_text('{"schema_version": 9, "browser": "visible"}')
        self.assertEqual(restarted.settings()["browser"], "hidden")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from tests.test_team_server import PanelTestCase

        class Panel(PanelTestCase):
            def runTest(self):  # pragma: no cover - harness only
                pass
        temporary = tempfile.TemporaryDirectory(prefix="portable-v3-quality-")
        self.addCleanup(temporary.cleanup)
        runtime = mock.patch.dict(os.environ, {"OUR_HARNESS_SWARM_RUN_DIR": str(Path(temporary.name) / "runtime")})
        runtime.start()
        self.addCleanup(runtime.stop)
        self.panel = Panel()
        self.panel.setUp()
        self.addCleanup(self.panel.doCleanups)
        manager = self.panel.panel.live_teams
        manager.board = lambda: {"agents": [{"id": "codex", "name": "GPT Codex", "who": "codex"}]}
        manager.config = lambda: {"providers": {"codex": {"kind": "codex-cli"}}}
        self.sessions = {}
        manager.session_factory = lambda spec, on_event, approver: self.sessions.setdefault(
            spec.agent_id, FakeSession(spec, on_event, approver))

    def test_attachments_and_browser_checks_over_http(self):
        status, listed = self.panel.ask("/api/live-team")
        self.assertEqual((status, listed["settings"]["browser"]), (200, "hidden"))
        picture = {"name": "ref.png", "type": "image/png", "data": base64.b64encode(PNG).decode()}
        status, created = self.panel.ask("/api/live-team", {"action": "create", "agents": ["codex"], "goal": "Like this",
                                                            "attachments": [picture]})
        self.assertEqual(status, 200, created)
        team = created["team"]["team_id"]
        self.assertTrue(wait_for(lambda: self.sessions.get("codex") and self.sessions["codex"].received))
        self.assertIn("ref.png", self.sessions["codex"].received[0])
        status, said = self.panel.ask("/api/live-team", {"action": "say", "team": team, "text": "",
                                                         "attachments": [picture]})
        self.assertEqual(status, 200, said)
        status, changed = self.panel.ask("/api/live-team", {"action": "browser", "team": team, "mode": "visible"})
        self.assertEqual((status, changed["settings"]["browser"]), (200, "visible"))
        self.assertEqual(self.panel.ask(f"/api/live-team/team?team={team}&after=0")[1]["team"]["browser"], "visible")
        self.assertEqual(self.panel.ask("/api/live-team", {"action": "browser", "mode": "x"})[0], 400)
        self.assertEqual(self.panel.ask("/api/live-team", {"action": "say", "team": team, "text": "x",
                                                           "attachments": [picture] * 11})[0], 400)
        self.panel.ask("/api/live-team", {"action": "close", "team": team})


if __name__ == "__main__":
    unittest.main()
