"""Microsoft 365 Copilot, which is reached over the web because there is
nothing on the machine to run.

Everything here runs against a stand-in Microsoft on this machine. What cannot
be proved from here is the real thing: that needs somebody with a Copilot
add-on seat, a registered app, and an administrator who has approved the seven
permissions. So what is proved instead is that the harness asks for exactly
what Microsoft's own documents say to ask for, reads back exactly the shape
they say comes back, and says something true and useful for each of the ways
this can be turned down - because those are the mornings somebody will actually
have, and being sent to the wrong person costs an afternoon.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError, ProviderRequest
from our_harness.providers import m365_copilot as m365


class StandingInForMicrosoft(BaseHTTPRequestHandler):
    """Answers the way Microsoft's documents say Microsoft answers."""

    replies: list[tuple[int, dict]] = []
    asked: list[tuple[str, dict]] = []

    def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw.startswith("{") else {
                one.split("=", 1)[0]: one.split("=", 1)[1] for one in raw.split("&") if "=" in one
            }
        except (json.JSONDecodeError, IndexError):
            body = {"raw": raw}
        type(self).asked.append((self.path, body))
        number, said = type(self).replies.pop(0) if type(self).replies else (200, {})
        held = json.dumps(said).encode("utf-8")
        self.send_response(number)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(held)))
        self.end_headers()
        self.wfile.write(held)

    def log_message(self, *args) -> None:  # noqa: A003 - quiet in the test output
        return


class AgainstAStandInMicrosoft(unittest.TestCase):
    """Everything here talks to a stand-in on this machine, never to Microsoft."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StandingInForMicrosoft)
        cls.where = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        StandingInForMicrosoft.replies = []
        StandingInForMicrosoft.asked = []
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        # The sign-in goes into a folder of this test's own, never the real one
        # sitting in somebody's profile.
        patched = mock.patch.dict(
            "os.environ", {"NEXUS_HARNESS_HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        for what, to in (
            ("WHERE_SIGN_IN_HAPPENS", self.where),
            ("WHERE_THE_QUESTIONS_GO", f"{self.where}/beta/copilot"),
        ):
            held = mock.patch.object(m365, what, to)
            held.start()
            self.addCleanup(held.stop)

    def answers(self, *replies) -> None:
        StandingInForMicrosoft.replies = list(replies)

    def a_provider(self, **settings):
        import copy

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "m365-copilot", "model": "", "endpoint": "", "api_key_env": "",
            **settings,
        })
        return m365.M365CopilotProvider(LoadedConfig(data, self.home, [], {}))

    def signed_in(self, **held) -> None:
        m365.keep_the_sign_in({
            "token": "a-token", "renew_with": "a-renewal",
            "runs_out_at": time.time() + 3600, "app": "the-app",
            "organisation": "organizations", "signed_in_at": "2026-08-21T00:00:00Z",
            **held,
        })

    def an_answer(self, text: str) -> dict:
        """What Microsoft's own documents show coming back.

        The question is echoed first and the answer is second, which is the
        trap: read as "the first one" this hands somebody their own question
        back and calls it the answer.
        """

        return {
            "id": "a-conversation", "turnCount": 1,
            "messages": [
                {"id": "1", "text": "what did I ask", "attributions": []},
                {"id": "2", "text": text, "attributions": []},
            ],
        }


class TheSettingsItNeedsAreRealSettingsTests(unittest.TestCase):
    """The settings have to survive being read off a disk.

    Everything else here builds them by hand, which is not how anybody gets
    them, and that is how the three settings this whole thing needs came to be
    undeclared: writing one of them down stopped the settings file loading at
    all. Both the guide and the app's own error message told people to do
    exactly that.
    """

    def a_project(self, providers: dict) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        here = Path(folder.name)
        (here / ".harness").mkdir()
        (here / ".harness" / "config.json").write_text(
            json.dumps({"providers": providers}), encoding="utf-8")
        return here

    def test_the_settings_in_the_guide_are_settings_this_accepts(self) -> None:
        """Word for word out of docs/MICROSOFT_365_COPILOT.md."""

        import copy

        from our_harness.config import validate_config

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"microsoft": {
            "kind": "m365-copilot",
            "model": "",
            "microsoft_app": "the Application (client) ID from step 2",
            "time_zone": "Europe/Oslo",
        }}
        validate_config(data)

    def test_the_tenant_can_be_named_too(self) -> None:
        import copy

        from our_harness.config import validate_config

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"microsoft": {
            "kind": "m365-copilot", "model": "",
            "microsoft_app": "an-app", "microsoft_organisation": "a-tenant"}}
        validate_config(data)

    def test_it_is_read_back_out_the_way_it_was_written(self) -> None:
        from our_harness.config import load_isolated_config

        held = load_isolated_config(self.a_project({}), {"providers": {"microsoft": {
            "kind": "m365-copilot", "model": "",
            "microsoft_app": "an-app", "time_zone": "Europe/Oslo"}}})
        settings = held.get("providers", {})["microsoft"]
        self.assertEqual(settings["microsoft_app"], "an-app")
        self.assertEqual(settings["time_zone"], "Europe/Oslo")

    def test_a_route_of_this_kind_can_be_made(self) -> None:
        """The last step of the guide, which nothing else here reaches."""

        import copy

        from our_harness.providers.base import create_provider

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "m365-copilot", "model": "", "endpoint": "", "api_key_env": "",
            "microsoft_app": "an-app", "time_zone": "Europe/Oslo"})
        made = create_provider(LoadedConfig(data, Path.cwd(), [], {}))
        self.assertEqual(made.app, "an-app")
        self.assertEqual(made.where_they_are, "Europe/Oslo")


class WhereTheSignInIsKeptTests(AgainstAStandInMicrosoft):
    """This is the one thing here worth stealing."""

    def test_the_file_is_made_with_nobody_else_allowed_in(self) -> None:
        """Written first and locked down after, the token sits there readable by
        anybody on the machine for as long as it takes to reach the next line.
        Which is not long, and is long enough."""

        made = []
        real = os.open

        def watch(path, flags, mode=0o777, **rest):
            if str(path).endswith(".part"):
                made.append((flags, mode))
            return real(path, flags, mode, **rest)

        with mock.patch.object(os, "open", watch):
            m365.keep_the_sign_in({"token": "a-token", "renew_with": "a-renewal"})
        self.assertTrue(made, "it did not make the file itself")
        flags, mode = made[-1]
        self.assertEqual(mode, 0o600, "made wide open and narrowed afterwards")
        self.assertTrue(flags & os.O_EXCL, "it would write over whatever was there")

    def test_on_windows_it_asks_for_the_file_to_be_kept_to_this_account(self) -> None:
        """The mode a file is made with is the whole story on Linux and means
        almost nothing on Windows, where a file takes what the folder hands
        down - and that can be a good deal more than one person."""

        asked = []
        with mock.patch.object(os, "name", "nt"), \
             mock.patch.dict("os.environ", {"USERNAME": "somebody"}), \
             mock.patch.object(
                 m365.subprocess, "run", lambda *a, **k: asked.append(a[0]) or None):
            m365.keep_the_sign_in({"token": "a-token"})
        self.assertTrue(asked, "nothing was asked of Windows at all")
        self.assertIn("/inheritance:r", asked[0])

    def test_windows_refusing_does_not_stop_somebody_signing_in(self) -> None:
        """Whoever administers the machine can read it whatever this does.
        Refusing to sign somebody in because their permissions could not be
        narrowed would be worse than the risk."""

        def refuse(*args, **rest):
            raise OSError("no icacls on this machine")

        with mock.patch.object(os, "name", "nt"), \
             mock.patch.dict("os.environ", {"USERNAME": "somebody"}), \
             mock.patch.object(m365.subprocess, "run", refuse):
            m365.keep_the_sign_in({"token": "a-token"})
        self.assertEqual(m365.read_the_sign_in()["token"], "a-token")

    def test_the_token_is_never_handed_back_to_whoever_asked(self) -> None:
        self.answers((200, {
            "access_token": "a-token", "refresh_token": "a-renewal", "expires_in": 3600}))
        said = m365.how_the_sign_in_is_going("the-app", "d")
        self.assertNotIn("a-token", json.dumps(said))
        self.assertNotIn("a-renewal", json.dumps(said))


class ANameFromMicrosoftInAWebAddressTests(AgainstAStandInMicrosoft):
    def test_a_conversation_name_cannot_walk_out_of_its_place(self) -> None:
        """Whatever Microsoft sends back goes into a web address that carries
        the sign-in. Escaped in the usual way, slashes are left alone, and a
        name with slashes in it lands somewhere else entirely - taking the
        sign-in with it."""

        self.signed_in()
        self.answers(
            (201, {"id": "../../../users/somebody-else/sendMail"}),
            (200, self.an_answer("ok")))
        provider = self.a_provider(microsoft_app="the-app")
        provider.complete(ProviderRequest(
            system_prefix="", dynamic_context="",
            messages=[{"role": "user", "content": "hello"}], model="", timeout_seconds=30))
        where = StandingInForMicrosoft.asked[1][0]
        self.assertNotIn("/users/", where)
        self.assertIn("%2F", where, "the slashes have to be escaped, not honoured")


class SigningInTests(AgainstAStandInMicrosoft):
    """A code pasted into a browser, because there is no key and no window."""

    def test_it_asks_for_all_seven_permissions_and_for_staying_signed_in(self) -> None:
        """Microsoft's own note says the Chat API needs every one of them and
        refuses without, so asking for six is a refusal nobody can explain."""

        self.answers((200, {
            "device_code": "d", "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "interval": 5, "expires_in": 900,
        }))
        m365.start_signing_in("the-app")
        _where, body = StandingInForMicrosoft.asked[0]
        asked_for = body["scope"].replace("+", " ").replace("%2F", "/").replace("%3A", ":")
        for wanted in (
            "Sites.Read.All", "Mail.Read", "People.Read.All",
            "OnlineMeetingTranscript.Read.All", "Chat.Read",
            "ChannelMessage.Read.All", "ExternalItem.Read.All", "offline_access",
        ):
            with self.subTest(permission=wanted):
                self.assertIn(wanted, asked_for)

    def test_the_code_and_where_to_paste_it_come_back(self) -> None:
        self.answers((200, {
            "device_code": "d", "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "interval": 5, "expires_in": 900,
        }))
        held = m365.start_signing_in("the-app")
        self.assertEqual(held["code"], "ABCD-EFGH")
        self.assertEqual(held["where"], "https://microsoft.com/devicelogin")
        self.assertEqual(held["waiting_on"], "d")

    def test_with_no_app_written_down_it_says_how_to_make_one(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            m365.start_signing_in("")
        self.assertIn("App registrations", str(caught.exception))

    def test_waiting_for_somebody_to_paste_it_is_not_a_failure(self) -> None:
        self.answers((400, {"error": "authorization_pending"}))
        held = m365.how_the_sign_in_is_going("the-app", "d")
        self.assertEqual((held["done"], held["waiting"]), (False, True))

    def test_being_asked_to_slow_down_is_not_a_failure_either(self) -> None:
        self.answers((400, {"error": "slow_down"}))
        self.assertTrue(m365.how_the_sign_in_is_going("the-app", "d")["waiting"])

    def test_the_sign_in_is_kept_once_the_code_is_pasted(self) -> None:
        self.answers((200, {
            "access_token": "a-token", "refresh_token": "a-renewal", "expires_in": 3600,
        }))
        self.assertTrue(m365.how_the_sign_in_is_going("the-app", "d")["done"])
        held = m365.read_the_sign_in()
        self.assertEqual(held["token"], "a-token")
        self.assertEqual(held["app"], "the-app")

    def test_an_app_microsoft_does_not_know_says_so_and_what_to_do(self) -> None:
        self.answers((400, {
            "error": "invalid_client", "error_description": "AADSTS700016: not found",
        }))
        held = m365.how_the_sign_in_is_going("the-app", "d")
        self.assertFalse(held["waiting"])
        self.assertIn("Allow public client flows", held["why"])
        self.assertIn("AADSTS700016", held["why"], "Microsoft's own words are worth keeping")

    def test_permissions_nobody_approved_points_at_the_administrator(self) -> None:
        self.answers((400, {"error": "consent_required", "error_description": "no consent"}))
        self.assertIn(
            "administrator approves",
            m365.how_the_sign_in_is_going("the-app", "d")["why"])

    def test_a_code_nobody_used_in_time_says_to_start_again(self) -> None:
        self.answers((400, {"error": "expired_token"}))
        self.assertIn("in time", m365.how_the_sign_in_is_going("the-app", "d")["why"])

    def test_the_sign_in_is_kept_with_the_person_not_with_the_project(self) -> None:
        """A project folder is a thing people copy onto shared drives and push
        to GitHub, and this file is the one thing here worth stealing."""

        self.assertNotIn(
            str(Path.cwd()).lower(), str(m365._where_the_sign_in_is()).lower())


class BeingAskedToSlowDownTests(AgainstAStandInMicrosoft):
    def test_it_says_how_much_longer_to_wait(self) -> None:
        """Being asked to slow down and slowing down are two different things.
        Asked again at the old pace, Microsoft stops answering altogether, and
        somebody watches a code they already pasted never be noticed."""

        self.answers((400, {"error": "slow_down"}))
        held = m365.how_the_sign_in_is_going("the-app", "d")
        self.assertTrue(held["waiting"])
        self.assertGreaterEqual(held["wait_longer_by"], 5)

    def test_just_waiting_does_not_ask_for_any_extra_wait(self) -> None:
        self.answers((400, {"error": "authorization_pending"}))
        self.assertEqual(m365.how_the_sign_in_is_going("the-app", "d")["wait_longer_by"], 0)


class StayingSignedInTests(AgainstAStandInMicrosoft):
    def test_a_sign_in_with_time_left_is_used_as_it_is(self) -> None:
        self.signed_in()
        self.assertEqual(m365.a_token_to_use("the-app"), "a-token")
        self.assertEqual(StandingInForMicrosoft.asked, [], "nothing to ask Microsoft")

    def test_one_about_to_run_out_is_renewed_before_it_does(self) -> None:
        """A question can take three minutes. One that starts on a good sign-in
        and finishes on a dead one fails halfway through, for a reason nobody
        could guess at."""

        self.signed_in(runs_out_at=time.time() + 60)
        self.answers((200, {"access_token": "a-newer-token", "expires_in": 3600}))
        self.assertEqual(m365.a_token_to_use("the-app"), "a-newer-token")

    def test_renewing_keeps_the_old_renewal_when_none_comes_back(self) -> None:
        """Microsoft does not always send a new one. Throwing the old one away
        is the difference between staying signed in and being asked again every
        hour."""

        self.signed_in(runs_out_at=time.time() + 60)
        self.answers((200, {"access_token": "a-newer-token", "expires_in": 3600}))
        m365.a_token_to_use("the-app")
        self.assertEqual(m365.read_the_sign_in()["renew_with"], "a-renewal")

    def test_a_renewal_microsoft_refuses_is_forgotten_rather_than_kept(self) -> None:
        """Kept, it fails again every time anybody types, for ever."""

        self.signed_in(runs_out_at=time.time() + 60)
        self.answers((400, {"error": "invalid_grant", "error_description": "gone"}))
        with self.assertRaises(m365.SignInNeeded):
            m365.a_token_to_use("the-app")
        self.assertEqual(m365.read_the_sign_in(), {})

    def test_nobody_signed_in_says_where_to_press(self) -> None:
        with self.assertRaises(m365.SignInNeeded) as caught:
            m365.a_token_to_use("the-app")
        self.assertIn("Your team", str(caught.exception))

    def test_a_sign_in_from_before_anybody_named_an_app_is_not_used(self) -> None:
        """Read as "no app written down, so nothing to check", a sign-in from
        before somebody named one gets used against whatever is named now - and
        Microsoft refuses it in a way nothing here could explain."""

        self.signed_in(app="")
        with self.assertRaises(m365.SignInNeeded):
            m365.a_token_to_use("the-app")

    def test_a_sign_in_for_a_different_app_is_not_used(self) -> None:
        """Somebody changed which app is written down. The old sign-in is for
        the old one and will be refused in a way nothing here could explain."""

        self.signed_in(app="an-older-app")
        with self.assertRaises(m365.SignInNeeded) as caught:
            m365.a_token_to_use("the-app")
        self.assertIn("different registered app", str(caught.exception))


class AskingItSomethingTests(AgainstAStandInMicrosoft):
    def a_question(self, **held) -> ProviderRequest:
        return ProviderRequest(
            system_prefix=held.pop("system", ""),
            dynamic_context="",
            messages=held.pop("messages", [{"role": "user", "content": "what changed?"}]),
            model="",
            timeout_seconds=30,
            **held,
        )

    def test_it_opens_a_conversation_and_then_asks_in_it(self) -> None:
        self.signed_in()
        self.answers(
            (201, {"id": "a-conversation"}),
            (200, self.an_answer("The parser, and the test around it.")))
        said = self.a_provider(microsoft_app="the-app").complete(self.a_question())
        self.assertEqual(said.text, "The parser, and the test around it.")
        where = [one for one, _body in StandingInForMicrosoft.asked]
        self.assertEqual(where, [
            "/beta/copilot/conversations",
            "/beta/copilot/conversations/a-conversation/chat",
        ])

    def test_the_answer_is_the_last_thing_said_and_not_the_first(self) -> None:
        """What comes back holds the question as well - Microsoft echoes it.
        Read as the first one, somebody is handed their own words back."""

        self.signed_in()
        self.answers((201, {"id": "c"}), (200, self.an_answer("the real answer")))
        said = self.a_provider(microsoft_app="the-app").complete(self.a_question())
        self.assertEqual(said.text, "the real answer")

    def test_the_sign_in_goes_on_every_ask(self) -> None:
        self.signed_in()
        self.answers((201, {"id": "c"}), (200, self.an_answer("ok")))
        with mock.patch.object(m365.M365CopilotProvider, "_say", autospec=True) as watched:
            watched.side_effect = [{"id": "c"}, self.an_answer("ok")]
            self.a_provider(microsoft_app="the-app").complete(self.a_question())
        for call in watched.call_args_list:
            self.assertEqual(call.args[3]["Authorization"], "Bearer a-token")

    def test_what_was_said_before_goes_along_as_background(self) -> None:
        """Microsoft keeps the conversation at their end and the harness keeps
        it at this one. Two copies drift the first time anything is started
        again, and then nobody can say which is the real one."""

        self.signed_in()
        self.answers((201, {"id": "c"}), (200, self.an_answer("ok")))
        self.a_provider(microsoft_app="the-app").complete(self.a_question(messages=[
            {"role": "user", "content": "the first thing"},
            {"role": "assistant", "content": "the first answer"},
            {"role": "user", "content": "the newest thing"},
        ]))
        _where, body = StandingInForMicrosoft.asked[1]
        self.assertEqual(body["message"]["text"], "the newest thing")
        background = " ".join(one["text"] for one in body["additionalContext"])
        self.assertIn("the first thing", background)
        self.assertIn("the first answer", background)
        self.assertNotIn("the newest thing", background, "that is the question, not background")

    def test_microsoft_is_told_where_the_person_is(self) -> None:
        """Their documents mark this required, and "tomorrow at nine" means
        nothing without it."""

        self.signed_in()
        self.answers((201, {"id": "c"}), (200, self.an_answer("ok")))
        self.a_provider(
            microsoft_app="the-app", time_zone="Europe/Oslo").complete(self.a_question())
        self.assertEqual(
            StandingInForMicrosoft.asked[1][1]["locationHint"]["timeZone"], "Europe/Oslo")

    def test_its_own_tags_and_footnote_marks_are_taken_out(self) -> None:
        """Copilot writes names inside tags of its own and marks where it got
        something. Useful to a page that draws them, noise to anybody reading."""

        self.signed_in()
        self.answers((201, {"id": "c"}), (200, self.an_answer(
            "<Person>Jo</Person> changed <File>parser.py</File>[^1^].")))
        said = self.a_provider(microsoft_app="the-app").complete(self.a_question())
        self.assertEqual(said.text, "Jo changed parser.py.")

    def test_nothing_at_all_coming_back_is_said_plainly(self) -> None:
        self.signed_in()
        self.answers((201, {"id": "c"}), (200, {"id": "c", "messages": []}))
        with self.assertRaises(HarnessError) as caught:
            self.a_provider(microsoft_app="the-app").complete(self.a_question())
        self.assertIn("answered with nothing", str(caught.exception))


class WhenItIsTurnedDownTests(AgainstAStandInMicrosoft):
    """Three different mornings, and a different person fixes each one."""

    def a_question(self) -> ProviderRequest:
        return ProviderRequest(
            system_prefix="", dynamic_context="",
            messages=[{"role": "user", "content": "hello"}], model="", timeout_seconds=30)

    def refused(self, number: int, said: str = "no") -> str:
        self.signed_in()
        self.answers((number, {"error": {"code": "x", "message": said}}))
        with self.assertRaises(HarnessError) as caught:
            self.a_provider(microsoft_app="the-app").complete(self.a_question())
        return str(caught.exception)

    def test_signed_out_says_to_sign_in_and_nothing_else(self) -> None:
        self.signed_in()
        self.answers((401, {"error": {"code": "InvalidAuthenticationToken"}}))
        with self.assertRaises(m365.SignInNeeded) as caught:
            self.a_provider(microsoft_app="the-app").complete(self.a_question())
        self.assertIn("sign in again", str(caught.exception))

    def test_allowed_in_but_not_allowed_to_ask_names_both_possibilities(self) -> None:
        """The one everybody hits first, and the one where guessing costs most:
        it is either the seat or the permissions, and a different person fixes
        each. Naming one and being wrong sends somebody to the wrong desk."""

        said = self.refused(403, "Copilot license required")
        self.assertIn("add-on seat", said)
        self.assertIn("administrator approves", said)
        self.assertIn("Copilot license required", said, "Microsoft's own words settle it")

    def test_the_address_being_gone_blames_the_preview_and_not_the_person(self) -> None:
        """It runs on Microsoft's preview address, which their own note says can
        change. Blaming the sign-in sends somebody round in circles."""

        self.assertIn("preview address", self.refused(404))

    def test_too_many_questions_says_to_wait(self) -> None:
        self.assertIn("Wait a minute", self.refused(429))

    def test_a_fault_at_their_end_says_so(self) -> None:
        self.assertIn("Microsoft's end", self.refused(503))

    def test_microsofts_own_words_are_kept_but_not_a_whole_page_of_them(self) -> None:
        said = self.refused(403, "x" * 5000)
        self.assertLess(len(said), 1200)


class WhatIsMissingTests(AgainstAStandInMicrosoft):
    """Said before somebody types, not after.

    The board used to mark everything ready and let people find out by typing.
    That is the whole reason this exists.
    """

    def test_no_app_written_down_says_how_to_make_one(self) -> None:
        self.assertIn("App registrations", m365.what_is_missing({}))

    def test_nobody_signed_in_says_which_button(self) -> None:
        self.assertIn(
            "Sign in to Microsoft", m365.what_is_missing({"microsoft_app": "the-app"}))

    def test_signed_in_and_set_up_is_nothing_missing(self) -> None:
        self.signed_in()
        self.assertEqual(m365.what_is_missing({"microsoft_app": "the-app"}), "")

    def test_a_sign_in_that_ran_out_with_no_way_back_says_to_sign_in_again(self) -> None:
        self.signed_in(runs_out_at=time.time() - 10, renew_with="")
        self.assertIn("run out", m365.what_is_missing({"microsoft_app": "the-app"}))


class TheThreeWaysInToTheSignInTests(unittest.TestCase):
    """A real panel, real requests over a real socket, Microsoft standing in.

    The browser check for this hands the screen its answers directly, so it
    shows that the screen draws what it is given and nothing about what the
    panel does with it. This is the part in between, and it had nothing.
    """

    def setUp(self) -> None:
        from our_harness import server
        from our_harness.config import load_config

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        here = Path(self.temporary.name)
        (here / ".harness").mkdir()
        (here / ".harness" / "config.json").write_text("{}", encoding="utf-8")
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), load_config(here))
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        thread = threading.Thread(target=self.panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict) -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        asked = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": self.panel.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(asked, timeout=15) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def microsoft_offers(self, **held):
        return mock.patch.object(m365, "start_signing_in", lambda app, org="": {
            "code": "ABCD-EFGH", "where": "https://microsoft.com/devicelogin",
            "waiting_on": "a-handle", "ask_again_after": 5, "gives_up_at": 0, **held})

    def test_it_gives_back_a_code_and_never_the_handle_behind_it(self) -> None:
        """The handle is Microsoft's name for this attempt. Worth nothing to
        anybody without the code, and still not something to put on a screen."""

        with self.microsoft_offers():
            status, said = self.ask("/api/microsoft/sign-in", {"app": "an-app"})
        self.assertEqual(status, 200)
        self.assertEqual(said["code"], "ABCD-EFGH")
        self.assertNotIn("a-handle", json.dumps(said))

    def test_asking_how_it_is_going_before_starting_says_so(self) -> None:
        status, said = self.ask("/api/microsoft/sign-in/how-it-is-going", {})
        self.assertEqual(status, 400)
        self.assertIn("Press Sign in to Microsoft first", said["error"])

    def test_a_second_window_does_not_quietly_take_the_first_ones_place(self) -> None:
        """Two windows on the same panel both pressed Sign in. The first then
        sat there asking about a code nothing was waiting on any more, showing
        somebody a code that would never be noticed however carefully they
        typed it."""

        with self.microsoft_offers():
            _status, first = self.ask("/api/microsoft/sign-in", {"app": "an-app"})
            _status, second = self.ask("/api/microsoft/sign-in", {"app": "an-app"})
        self.assertNotEqual(first["attempt"], second["attempt"])
        status, said = self.ask(
            "/api/microsoft/sign-in/how-it-is-going", {"attempt": first["attempt"]})
        self.assertEqual(status, 400)
        self.assertIn("replaced by a newer one", said["error"])

    def test_the_newer_window_is_the_one_that_works(self) -> None:
        with self.microsoft_offers():
            self.ask("/api/microsoft/sign-in", {"app": "an-app"})
            _status, second = self.ask("/api/microsoft/sign-in", {"app": "an-app"})
        with mock.patch.object(
                m365, "how_the_sign_in_is_going",
                lambda app, waiting_on, org="": {
                    "done": True, "waiting": False, "why": "", "handle": waiting_on}):
            status, said = self.ask(
                "/api/microsoft/sign-in/how-it-is-going", {"attempt": second["attempt"]})
        self.assertEqual(status, 200)
        self.assertTrue(said["done"])

    def test_the_app_that_was_asked_for_is_the_app_that_is_used(self) -> None:
        """Not whatever the window asking happens to send the second time."""

        used = []
        with self.microsoft_offers():
            _status, held = self.ask("/api/microsoft/sign-in", {"app": "the-right-app"})
        with mock.patch.object(
                m365, "how_the_sign_in_is_going",
                lambda app, waiting_on, org="": used.append(app) or {
                    "done": True, "waiting": False, "why": ""}):
            self.ask("/api/microsoft/sign-in/how-it-is-going", {
                "attempt": held["attempt"], "app": "a-different-app"})
        self.assertEqual(used, ["the-right-app"])

    def test_nobody_without_the_panel_token_gets_in(self) -> None:
        import urllib.error
        import urllib.request

        asked = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/microsoft/sign-in",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(asked, timeout=15)
        with caught.exception as exc:
            self.assertEqual(exc.code, 400)

    def test_signing_out_forgets_the_attempt_as_well_as_the_sign_in(self) -> None:
        """Left behind, the next window to ask gets an answer about a sign-in
        somebody deliberately got rid of."""

        with self.microsoft_offers():
            self.ask("/api/microsoft/sign-in", {"app": "an-app"})
        forgotten = []
        with mock.patch.object(m365, "forget_the_sign_in", lambda: forgotten.append(1)):
            status, said = self.ask("/api/microsoft/sign-out", {})
        self.assertEqual((status, said["signed_out"]), (200, True))
        self.assertEqual(forgotten, [1])
        status, said = self.ask("/api/microsoft/sign-in/how-it-is-going", {})
        self.assertEqual(status, 400)
        self.assertIn("Press Sign in to Microsoft first", said["error"])


if __name__ == "__main__":
    unittest.main()
