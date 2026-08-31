"""The team through the panel, not through the library.

The library tests drive the team directly. That leaves the part between the
button and the file untested, and that is where the expensive mistakes live: a
team saved under a name that was never checked, a rename that leaves two teams
where there was one, a saved team that has since been removed taking the whole
view down with it.
"""

from __future__ import annotations

import copy
import json
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import seats as seats_lab
from our_harness import server, team
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


def a_seat(kind: str, route: str, label: str, *, ready: bool) -> seats_lab.Seat:
    return seats_lab.Seat(
        kind=kind, label=label, route=route, command=route,
        found_at=f"C:/tools/{route}.exe" if ready else "",
        version="1.2.3" if ready else "", ready=ready,
        why_not="" if ready else "It is not on this machine.",
    )


BOTH = seats_lab.Look(
    seats=[
        a_seat("claude-cli", "claude", "Claude command line", ready=True),
        a_seat("copilot-cli", "copilot", "GitHub Copilot command line", ready=True),
    ],
    settings_file=".harness/config.local.json",
)


class PanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        threading.Thread(target=self.panel.serve_forever, daemon=True).start()
        self.addCleanup(self.panel.shutdown)
        self.config = config
        looking = mock.patch.object(seats_lab, "look", return_value=BOTH)
        looking.start()
        self.addCleanup(looking.stop)

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "X-Harness-Token": self.panel.token},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ReadingTheTeamTests(PanelTestCase):
    def test_opening_the_view_says_who_is_here_and_offers_a_team(self) -> None:
        status, said = self.ask("/api/who-is-on-it")
        self.assertEqual(status, 200)
        self.assertEqual(said["who"]["how_many_ready"], 2)
        self.assertTrue(said["starting_team"]["nodes"])
        self.assertTrue(said["jobs"])

    def test_opening_the_view_writes_nothing(self) -> None:
        # Looking at a tab must never leave files behind in somebody's project.
        self.ask("/api/who-is-on-it")
        self.assertFalse(list((self.root / ".harness").glob("workflows/*.json")))

    def test_opening_the_view_supplies_explicit_native_provider_choices(self) -> None:
        status, said = self.ask("/api/who-is-on-it")
        self.assertEqual(status, 200)
        choices = {one["kind"]: one for one in said["model_providers"]}
        self.assertEqual(
            set(choices), {"openai", "anthropic", "gemini", "ollama", "openai-compatible"}
        )
        self.assertEqual(choices["openai"]["default_endpoint"], "https://api.openai.com/v1")
        self.assertEqual(choices["anthropic"]["default_key_name"], "ANTHROPIC_API_KEY")
        self.assertEqual(choices["gemini"]["ways_in"], ["with-a-key"])
        self.assertEqual(choices["ollama"]["ways_in"], ["on-this-machine"])

    def test_a_team_that_has_gone_does_not_take_the_view_with_it(self) -> None:
        status, said = self.ask("/api/who-is-on-it?name=never-saved")
        self.assertEqual(status, 200, "the whole view still comes back")
        self.assertIsNone(said["open"])
        self.assertEqual(said["gone"], "never-saved")
        self.assertTrue(said["starting_team"]["nodes"], "and there is still something to show")


class WritingTheTeamTests(PanelTestCase):
    def starting(self) -> dict:
        _status, said = self.ask("/api/who-is-on-it")
        return said["starting_team"]

    def test_a_team_is_saved_and_read_back(self) -> None:
        status, said = self.ask("/api/who-is-on-it/save", {"name": "Two seats", "team": self.starting()})
        self.assertEqual(status, 200)
        self.assertEqual(said["team"]["name"], "Two seats")
        self.assertEqual([one["name"] for one in said["teams"]], ["Two seats"])
        _status, opened = self.ask("/api/who-is-on-it?name=Two%20seats")
        self.assertEqual(opened["open"]["name"], "Two seats")
        self.assertTrue(opened["open"]["plain"]["hand_overs"])

    def test_changing_the_name_moves_it_rather_than_copying_it(self) -> None:
        self.ask("/api/who-is-on-it/save", {"name": "Frist name", "team": self.starting()})
        status, said = self.ask("/api/who-is-on-it/save", {
            "name": "First name", "team": self.starting(), "was": "Frist name",
        })
        self.assertEqual(status, 200)
        self.assertEqual([one["name"] for one in said["teams"]], ["First name"])

    def test_a_team_that_names_a_tool_nobody_has_is_refused(self) -> None:
        graph = self.starting()
        graph["nodes"][2]["config"] = {"provider_route": "made-up"}
        status, said = self.ask("/api/who-is-on-it/save", {"name": "Broken", "team": graph})
        self.assertEqual(status, 400)
        self.assertIn("cannot be saved", said["error"])
        _status, whole = self.ask("/api/who-is-on-it")
        self.assertEqual(whole["teams"], [])

    def test_checking_a_team_saves_nothing(self) -> None:
        graph = self.starting()
        status, said = self.ask("/api/who-is-on-it/check", {"team": graph})
        self.assertEqual(status, 200)
        self.assertEqual(said["problems"], [])
        self.assertTrue(said["plain"]["hand_overs"])
        _status, whole = self.ask("/api/who-is-on-it")
        self.assertEqual(whole["teams"], [], "checking wrote a team down")

    def test_removing_a_team_removes_it(self) -> None:
        self.ask("/api/who-is-on-it/save", {"name": "Going", "team": self.starting()})
        status, said = self.ask("/api/who-is-on-it/remove", {"name": "Going"})
        self.assertEqual(status, 200)
        self.assertEqual(said["teams"], [])
        status, _said = self.ask("/api/who-is-on-it/remove", {"name": "Going"})
        self.assertEqual(status, 400, "and says so the second time")

    def test_blank_customary_provider_values_are_saved_and_visible_after_reload(self) -> None:
        status, done = self.ask("/api/who-is-on-it/add-a-model", {"model": {
            "route": "new_anthropic",
            "way_in": "with-a-key",
            "provider": "anthropic",
            "model": "fixture-claude",
            "endpoint": "",
            "key_name": "",
        }})
        self.assertEqual(status, 200)
        self.assertEqual(done["provider"], "anthropic")
        self.assertEqual(done["endpoint"], "https://api.anthropic.com/v1")
        self.assertEqual(done["key_name"], "ANTHROPIC_API_KEY")

        saved = json.loads(
            (self.root / ".harness" / "config.local.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["providers"]["new_anthropic"], {
            "kind": "anthropic",
            "model": "fixture-claude",
            "endpoint": "https://api.anthropic.com/v1",
            "api_key_env": "ANTHROPIC_API_KEY",
        })
        status, reopened = self.ask("/api/who-is-on-it")
        self.assertEqual(status, 200)
        member = next(
            one for one in reopened["who"]["members"] if one["route"] == "new_anthropic"
        )
        self.assertEqual(member["kind"], "anthropic")
        self.assertEqual(member["version"], "fixture-claude")

    def test_everything_here_needs_the_token(self) -> None:
        for path, body in (
            ("/api/who-is-on-it", None),
            ("/api/who-is-on-it/save", {"name": "x", "team": {}}),
            ("/api/who-is-on-it/remove", {"name": "x"}),
            ("/api/who-is-on-it/add-a-model", {"model": {}}),
            ("/api/who-is-on-it/check", {"team": {}}),
        ):
            with self.subTest(path=path):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}{path}",
                    data=json.dumps(body).encode("utf-8") if body is not None else None,
                    headers={"Content-Type": "application/json"},
                    method="POST" if body is not None else "GET",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(caught.exception.code, 400)


class TheTabAndTheModuleAgreeTests(unittest.TestCase):
    """The panel and the library have to mean the same things by the same words."""

    def setUp(self) -> None:
        self.panel = (Path(__file__).resolve().parents[1] / "src/our_harness/ui/app.js").read_text(
            encoding="utf-8"
        )
        self.page = (Path(__file__).resolve().parents[1] / "src/our_harness/ui/index.html").read_text(
            encoding="utf-8"
        )

    def test_the_tab_is_in_the_page(self) -> None:
        self.assertIn('data-view="team"', self.page)
        self.assertIn('data-view-panel="team"', self.page)

    def test_every_endpoint_the_panel_calls_is_one_the_server_has(self) -> None:
        served = (Path(__file__).resolve().parents[1] / "src/our_harness/server.py").read_text(
            encoding="utf-8"
        )
        for path in (
            "/api/who-is-on-it", "/api/who-is-on-it/save", "/api/who-is-on-it/remove",
            "/api/who-is-on-it/add-a-model", "/api/who-is-on-it/check",
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.panel)
                self.assertIn(f'"{path}"', served)

    def test_no_team_request_holds_the_lock_the_checks_need(self) -> None:
        # Every team request looks at the machine, which runs each assistant's
        # own tool, and one waiting on a sign-in can sit there for the best part
        # of a minute. Holding the suite's lock through that would stop every
        # change to the checks for a job that has nothing to do with them.
        served = (Path(__file__).resolve().parents[1] / "src/our_harness/server.py").read_text(
            encoding="utf-8"
        )
        for path in ("/api/who-is-on-it", "/api/who-is-on-it/save",
                     "/api/who-is-on-it/remove", "/api/who-is-on-it/check"):
            with self.subTest(path=path):
                after = served.split(f'"{path}":')[1][:900]
                # The next lock it takes is the seats one, not the suite one.
                taken = [word for word in ("seats_lock", "suite_lock") if word in after]
                self.assertEqual(taken[:1], ["seats_lock"], f"{path} takes {taken}")

    def test_setting_the_team_up_asks_for_exactly_what_the_seats_view_asks_for(self) -> None:
        # No check presses this button, because the seats view already presses
        # the same request and puts it back. That is only safe while the two
        # really are the same request, which is what this holds down.
        after = self.panel.split("async function setUpTheTeam()")[1][:600]
        self.assertIn('request("/api/seats/setup"', after)
        self.assertNotIn("/api/who-is-on-it/setup", self.panel)

    def test_the_panel_only_offers_jobs_the_harness_knows(self) -> None:
        # The list comes from the server, so a job the panel could show and the
        # harness could not run cannot happen. This is the guard on that.
        self.assertIn("said.jobs", self.panel)
        self.assertNotIn('teamJobs = [{', self.panel)

    def test_the_add_model_window_uses_server_owned_provider_defaults(self) -> None:
        self.assertIn('id="teamModelProvider"', self.page)
        self.assertIn('id="teamModelEndpointHelp"', self.page)
        self.assertIn('id="teamModelKeyHelp"', self.page)
        self.assertIn("teamModelProviders = said.model_providers || []", self.panel)
        self.assertIn("one.ways_in.includes(selectedWay)", self.panel)
        self.assertIn('provider: $("teamModelProvider").value', self.panel)
        self.assertIn('provider?.default_endpoint || ""', self.panel)
        self.assertIn('provider?.default_key_name || ""', self.panel)


if __name__ == "__main__":
    unittest.main()
