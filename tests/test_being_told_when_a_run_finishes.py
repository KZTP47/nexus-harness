"""Being told when a run finishes.

This is the one part of the harness that cannot work on its own. Slack, Discord,
Telegram, Teams and email all want something somebody has to go and get, and no
amount of care here changes that. So most of what is tested is the honesty: it
says which ways are ready and which are waiting, it names the variable to set
and where to get what goes in it, and it refuses clearly rather than failing in
a way somebody has to guess about.

The rest is what has to hold when it does work: the secret is never written into
a file, never sent anywhere it was not meant to go, and never repeated back in a
message, a log or a run's own record.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from our_harness import tell_somebody as telling
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class TellingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.sent: list[tuple[str, dict]] = []

    def a_way(self, **changes) -> telling.Way:
        return telling.save(self.config, {
            "name": "Our room",
            "kind": "webhook",
            "secret_in": "A_PRETEND_KEY",
            **changes,
        })

    def with_the_key(self, value: str = "https://example.invalid/hook", name="A_PRETEND_KEY"):
        return mock.patch.dict(os.environ, {name: value})

    def catching(self):
        def post(address, body):
            self.sent.append((address, body))
            return "ok"

        return post


class EveryOneOfThemNeedsSomething(TellingTestCase):
    def test_it_says_so_before_anything_else(self) -> None:
        for kind in telling.THE_KINDS:
            with self.subTest(kind=kind.kind):
                self.assertTrue(kind.secret_is, "it says what the key is")
                self.assertTrue(kind.usually_called.isupper(), "and what to call it")
                self.assertGreater(
                    len(kind.where_to_get_one), 40, "and where to go and get one"
                )

    def test_with_no_key_it_says_which_one_and_where_to_get_it(self) -> None:
        one = self.a_way()
        said = telling.why_it_cannot_be_used(one)
        self.assertIn("A_PRETEND_KEY", said)
        self.assertIn("cannot work without something you go and get", said)
        self.assertIn("JSON", said, "and what the far end will receive")

    def test_with_the_key_there_is_nothing_to_say(self) -> None:
        one = self.a_way()
        with self.with_the_key():
            self.assertEqual(telling.why_it_cannot_be_used(one), "")
            self.assertTrue(telling.is_the_key_there(one))

    def test_a_key_that_is_only_spaces_is_no_key(self) -> None:
        one = self.a_way()
        with self.with_the_key("   "):
            self.assertFalse(telling.is_the_key_there(one))

    def test_the_listing_says_which_are_ready(self) -> None:
        self.a_way()
        self.a_way(name="Another room", secret_in="A_KEY_NOBODY_SET")
        with self.with_the_key():
            stands = {one["name"]: one for one in telling.how_it_stands(self.config)}
        self.assertTrue(stands["Our room"]["ready"])
        self.assertFalse(stands["Another room"]["ready"])
        self.assertIn("A_KEY_NOBODY_SET", stands["Another room"]["why_not"])


class TheSecretIsNeverWrittenDown(TellingTestCase):
    def test_only_the_name_of_a_variable_is_saved(self) -> None:
        self.a_way()
        with self.with_the_key("https://hooks.example.invalid/T00/B11/xyzzy"):
            written = (telling.folder(self.config) / "our-room.json").read_text(
                encoding="utf-8"
            )
        self.assertIn("A_PRETEND_KEY", written)
        self.assertNotIn("xyzzy", written)
        self.assertNotIn("hooks.example.invalid", written)

    def test_pasting_the_secret_into_the_box_is_refused(self) -> None:
        """Somebody does this, because it is the box next to the word webhook.
        Saved, the address goes into a file meant to be committed."""

        for pasted in (
            "https://hooks.slack.com/services/T00/B11/xyzzy",
            "xoxb-1234-5678-abcdefgh",
            "smtp.example.com:587",
            "somebody@example.com",
        ):
            with self.subTest(pasted=pasted):
                with self.assertRaises(telling.TellingError) as caught:
                    self.a_way(secret_in=pasted)
                self.assertIn("looks like the secret itself", str(caught.exception))

    def test_a_name_with_nothing_wrong_with_it_is_kept(self) -> None:
        for good in ("SLACK_WEBHOOK", "MY_KEY_2", "_private"):
            with self.subTest(name=good):
                self.assertEqual(self.a_way(secret_in=good).secret_in, good)

    def test_no_name_at_all_takes_the_usual_one(self) -> None:
        self.assertEqual(
            telling.read_it({"name": "x", "kind": "slack"}).secret_in, "SLACK_WEBHOOK"
        )


class WhatItRefuses(TellingTestCase):
    def test_a_kind_nobody_offers(self) -> None:
        with self.assertRaises(telling.TellingError) as caught:
            telling.read_it({"name": "x", "kind": "carrier pigeon"})
        self.assertIn("slack", str(caught.exception), "it lists the real ones")

    def a_mail_one(self, **changes) -> dict:
        return {
            "name": "By mail", "kind": "email", "secret_in": "MAIL_PASSWORD",
            "server_in": "MAIL_SERVER", "to": "them@example.com",
            "sent_from": "us@example.com", **changes,
        }

    def test_email_without_the_things_email_needs(self) -> None:
        for missing in ("to", "sent_from"):
            with self.subTest(missing=missing):
                with self.assertRaises(telling.TellingError):
                    telling.read_it(self.a_mail_one(**{missing: ""}))

    def test_the_mail_server_is_the_name_of_a_variable_too(self) -> None:
        """Kept in the file, this was the one thing that was not a secret and
        still decided where a secret went."""

        with self.assertRaises(telling.TellingError) as caught:
            telling.read_it(self.a_mail_one(server_in="smtp.example.com:587"))
        self.assertIn("looks like the secret itself", str(caught.exception))

    def test_no_mail_server_variable_takes_the_usual_one(self) -> None:
        self.assertEqual(
            telling.read_it(self.a_mail_one(server_in="")).server_in, "MAIL_SERVER"
        )

    def test_a_mail_server_with_a_port_that_is_not_a_port(self) -> None:
        one = telling.read_it(self.a_mail_one())
        for bad in ("smtp.example.com:soon", "smtp.example.com:99999", ":587"):
            with self.subTest(server=bad):
                with mock.patch.dict(os.environ, {"MAIL_SERVER": bad}):
                    with self.assertRaises(telling.TellingError):
                        telling._the_mail_server(one)

    def test_a_mail_server_nobody_has_set_says_why_it_is_read_that_way(self) -> None:
        one = telling.read_it(self.a_mail_one())
        with mock.patch.dict(os.environ, {"MAIL_SERVER": ""}):
            said = telling.why_it_cannot_be_used(one)
        self.assertIn("MAIL_SERVER", said)
        self.assertIn("decides where your password goes", said)

    def test_a_mail_server_that_is_set_is_read_from_this_machine(self) -> None:
        one = telling.read_it(self.a_mail_one())
        with mock.patch.dict(os.environ, {"MAIL_SERVER": "smtp.example.com:2525"}):
            self.assertEqual(telling._the_mail_server(one), ("smtp.example.com", 2525))
        with mock.patch.dict(os.environ, {"MAIL_SERVER": "smtp.example.com"}):
            self.assertEqual(telling._the_mail_server(one), ("smtp.example.com", 587))

    def test_a_name_that_could_reach_outside_the_project(self) -> None:
        for bad in ("../secrets", "a/b", "a\\b", ""):
            with self.subTest(name=bad):
                with self.assertRaises(telling.TellingError):
                    self.a_way(name=bad)

    def test_two_names_that_come_to_one_file(self) -> None:
        self.a_way(name="Our Room")
        with self.assertRaises(telling.TellingError) as caught:
            self.a_way(name="our room")
        self.assertIn("Our Room", str(caught.exception))


class WhereItIsWillingToSend(TellingTestCase):
    def test_plain_http_to_somewhere_else_is_refused(self) -> None:
        """A webhook address read on the way is your run reports read on the
        way."""

        one = self.a_way()
        with self.with_the_key("http://example.invalid/hook"):
            with self.assertRaises(telling.TellingError) as caught:
                telling.tell_them(self.config, one, "hello", "there", post=self.catching())
        self.assertIn("https", str(caught.exception))

    def test_plain_http_to_this_very_machine_is_allowed(self) -> None:
        one = self.a_way()
        with self.with_the_key("http://127.0.0.1:9/hook"):
            telling.tell_them(self.config, one, "hello", "there", post=self.catching())
        self.assertTrue(self.sent)

    def test_an_address_with_a_name_and_password_in_it_is_refused(self) -> None:
        one = self.a_way()
        with self.with_the_key("https://me:secret@example.invalid/hook"):
            with self.assertRaises(telling.TellingError) as caught:
                telling.tell_them(self.config, one, "hello", "there", post=self.catching())
        self.assertIn("name and password", str(caught.exception))

    def test_something_that_is_not_an_address_at_all(self) -> None:
        one = self.a_way()
        with self.with_the_key("not an address"):
            with self.assertRaises(telling.TellingError):
                telling.tell_them(self.config, one, "hello", "there", post=self.catching())


class WhatGoesOut(TellingTestCase):
    A_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"

    def test_a_key_the_failing_step_printed_does_not_go_with_it(self) -> None:
        """A chat room is a very public place for that to land."""

        one = self.a_way()
        with self.with_the_key():
            telling.tell_them(
                self.config, one, "Nightly did not pass",
                f"it failed: Authorization: Bearer {self.A_KEY}",
                post=self.catching(),
            )
        self.assertNotIn(self.A_KEY, json.dumps(self.sent))
        self.assertIn("it failed", json.dumps(self.sent))

    def test_a_very_long_report_is_visibly_shortened_with_its_full_run_reference(self) -> None:
        one = self.a_way()
        original = "x" * 50_000
        reference = "Nexus → Visual test automation → run abc123 (pipeline-run:abc123)"
        with self.with_the_key():
            said = telling.tell_them(
                self.config, one, "Nightly", original,
                full_result_reference=reference, post=self.catching(),
            )
        _address, body = self.sent[-1]
        self.assertLessEqual(len(body["body"]), telling.MOST_LETTERS)
        self.assertIn("Shortened notification", body["body"])
        self.assertIn("50,000 characters", body["body"])
        self.assertIn("pipeline-run:abc123", body["body"])
        self.assertTrue(said.truncated)
        self.assertEqual(said.original_characters, len(original))
        self.assertEqual(said.full_result_reference, reference)

    def test_a_long_report_without_a_persisted_full_result_is_refused(self) -> None:
        one = self.a_way()
        with self.with_the_key(), self.assertRaises(telling.TellingError) as caught:
            telling.tell_them(
                self.config, one, "Nightly", "x" * 50_000, post=self.catching()
            )
        self.assertIn("did not silently cut it", str(caught.exception))
        self.assertIn("provide its reference", str(caught.exception))
        self.assertEqual(self.sent, [])

    def test_discord_has_no_second_unmarked_slice(self) -> None:
        one = telling.read_it({
            "name": "x", "kind": "discord", "secret_in": "A_PRETEND_KEY",
        })
        reference = "pipeline-run:discord123"
        with self.with_the_key():
            said = telling.tell_them(
                self.config, one, "Nightly", "d" * 50_000,
                full_result_reference=reference, post=self.catching(),
            )
        _address, payload = self.sent[-1]
        self.assertLessEqual(len(payload["content"]), telling.DISCORD_MESSAGE_LETTERS)
        self.assertIn("Shortened notification", payload["content"])
        self.assertIn(reference, payload["content"])
        self.assertTrue(said.truncated)

    def test_a_heading_over_the_disclosed_limit_is_rejected_not_sliced(self) -> None:
        one = self.a_way()
        with self.with_the_key(), self.assertRaises(telling.TellingError) as caught:
            telling.tell_them(
                self.config, one, "h" * (telling.MOST_HEADING_LETTERS + 1),
                "short body", post=self.catching(),
            )
        self.assertIn("did not truncate", str(caught.exception))
        self.assertEqual(self.sent, [])

    def test_each_kind_is_sent_the_shape_it_expects(self) -> None:
        for kind, holds in (
            ("slack", "text"), ("discord", "content"), ("teams", "text"),
            ("webhook", "body"),
        ):
            with self.subTest(kind=kind):
                self.sent.clear()
                one = telling.read_it({
                    "name": "x", "kind": kind, "secret_in": "A_PRETEND_KEY",
                })
                with self.with_the_key():
                    telling.tell_them(
                        self.config, one, "Nightly", "one step failed",
                        post=self.catching(),
                    )
                _address, body = self.sent[-1]
                self.assertIn(holds, body)
                self.assertIn("one step failed", json.dumps(body))

    def test_telegram_puts_its_token_in_the_address_and_nowhere_else(self) -> None:
        """That is how Telegram is built. It is also why nothing here ever
        writes an address into a message, a log, or a run's own record."""

        one = telling.read_it({
            "name": "x", "kind": "telegram", "secret_in": "A_PRETEND_KEY", "to": "12345",
        })
        with self.with_the_key("1234:AAtoken"):
            telling.tell_them(self.config, one, "Nightly", "failed", post=self.catching())
        address, body = self.sent[-1]
        self.assertIn("AAtoken", address)
        self.assertNotIn("AAtoken", json.dumps(body))
        self.assertEqual(body["chat_id"], "12345")

    def test_one_that_is_turned_off_is_not_told(self) -> None:
        one = self.a_way(turned_on=False)
        with self.with_the_key():
            said = telling.tell_them(self.config, one, "x", "y", post=self.catching())
        self.assertFalse(said.sent)
        self.assertEqual(self.sent, [])


class WhenTheFarEndIsNotThere(TellingTestCase):
    def test_it_says_so_without_saying_the_address(self) -> None:
        """This line goes into a run's own record, and for Telegram the address
        holds the token."""

        one = telling.read_it({
            "name": "x", "kind": "telegram", "secret_in": "A_PRETEND_KEY", "to": "1",
        })

        def falls_over(address, body):
            raise OSError("could not reach https://api.telegram.org/bot1234:AAtoken/x")

        with self.with_the_key("1234:AAtoken"):
            said = telling.tell_them(
                self.config, one, "Nightly", "failed", post=falls_over
            )
        self.assertFalse(said.sent)
        self.assertNotIn("AAtoken", said.note)
        self.assertIn("could not be reached", said.note)

    def test_being_pointed_somewhere_else_is_not_followed(self) -> None:
        said = telling._NoGoingElsewhere()
        with self.assertRaises(telling.TellingError) as caught:
            said.redirect_request(None, None, 302, "moved", {}, "https://elsewhere.invalid")
        self.assertIn("somewhere else", str(caught.exception))


class TellingEverybody(TellingTestCase):
    def test_only_when_it_did_not_pass(self) -> None:
        """A run that went fine is not news, and something that tells you every
        night is something you stop reading by the end of the week."""

        self.a_way()
        with self.with_the_key():
            passed = telling.tell_everybody(
                self.config, "Nightly", "all good", passed=True,
                only_when_it_fails=True, post=self.catching(),
            )
            failed = telling.tell_everybody(
                self.config, "Nightly", "one failed", passed=False,
                only_when_it_fails=True, post=self.catching(),
            )
        self.assertEqual(passed, [])
        self.assertEqual([one.sent for one in failed], [True])

    def test_one_that_cannot_be_used_does_not_stop_the_others(self) -> None:
        self.a_way(name="Ready one")
        self.a_way(name="Waiting one", secret_in="A_KEY_NOBODY_SET")
        with mock.patch.dict(os.environ, {"A_PRETEND_KEY": "https://example.invalid/h"}):
            said = telling.tell_everybody(
                self.config, "Nightly", "failed", passed=False, post=self.catching()
            )
        by_name = {one.name: one for one in said}
        self.assertTrue(by_name["Ready one"].sent)
        self.assertFalse(by_name["Waiting one"].sent)
        self.assertIn("A_KEY_NOBODY_SET", by_name["Waiting one"].note)


class ThroughARealPipe(TellingTestCase):
    """Proved against a web server that really answers, on this machine, with
    no key from anybody - because a feature nobody here can run is a feature
    nobody here can check."""

    def setUp(self) -> None:
        super().setUp()
        self.arrived: list[dict] = []
        arrived = self.arrived

        class Listening(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the name this asks for
                said = self.rfile.read(int(self.headers["Content-Length"]))
                arrived.append(json.loads(said))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_):  # noqa: N802 - quiet
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Listening)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def test_it_really_arrives(self) -> None:
        one = self.a_way()
        with self.with_the_key(self.address):
            said = telling.tell_them(
                self.config, one, "Nightly did not pass", "1 step did not pass",
                passed=False,
            )
        self.assertTrue(said.sent, said.note)
        self.assertEqual(self.arrived[-1]["heading"], "Nightly did not pass")
        self.assertIs(self.arrived[-1]["passed"], False)

    def test_a_key_in_the_report_does_not_arrive_with_it(self) -> None:
        one = self.a_way()
        with self.with_the_key(self.address):
            telling.tell_them(
                self.config, one, "Nightly",
                "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345",
                passed=False,
            )
        self.assertNotIn(
            "sk-abcdefghijklmnopqrstuvwxyz012345", json.dumps(self.arrived)
        )


class TheKeyForThisVeryThingIsCleanedToo(TellingTestCase):
    """Guessed from the name alone, a webhook address was not recognised as a
    secret: "webhook" is not one of the words the cleaner looks for, and an
    address does not look like a token.

    So a check that printed its own Slack address had it sent out, in plain
    text, to Slack. These are not guessed now - they are read from what
    somebody set up here.
    """

    A_REAL_LOOKING_ONE = "https://hooks.slack.invalid/services/T00/B11/thisIsTheSecret"

    def test_the_address_it_is_sent_to_does_not_go_in_the_message(self) -> None:
        telling.save(self.config, {
            "name": "Our room", "kind": "slack", "secret_in": "A_PRETEND_SLACK",
        })
        one = telling.every_one(self.config)[0]
        with mock.patch.dict(os.environ, {"A_PRETEND_SLACK": self.A_REAL_LOOKING_ONE}):
            telling.tell_them(
                self.config, one, "Nightly did not pass",
                f"the check failed while posting to {self.A_REAL_LOOKING_ONE}",
                post=self.catching(),
            )
        _address, body = self.sent[-1]
        self.assertNotIn("thisIsTheSecret", json.dumps(body))
        self.assertIn("the check failed while posting to", json.dumps(body))

    def test_another_way_s_key_is_cleaned_out_as_well(self) -> None:
        """One failing report can quote a different one's key."""

        telling.save(self.config, {
            "name": "Our room", "kind": "webhook", "secret_in": "A_PRETEND_KEY",
        })
        telling.save(self.config, {
            "name": "Somewhere else", "kind": "slack", "secret_in": "A_PRETEND_SLACK",
        })
        one = [w for w in telling.every_one(self.config) if w.name == "Our room"][0]
        with mock.patch.dict(os.environ, {
            "A_PRETEND_KEY": "https://example.invalid/hook",
            "A_PRETEND_SLACK": self.A_REAL_LOOKING_ONE,
        }):
            telling.tell_them(
                self.config, one, "Nightly",
                f"it printed {self.A_REAL_LOOKING_ONE} on the way out",
                post=self.catching(),
            )
        self.assertNotIn("thisIsTheSecret", json.dumps(self.sent))

    def test_the_cleaner_finds_the_names_on_its_own(self) -> None:
        from our_harness.redaction import CredentialRedactor

        telling.save(self.config, {
            "name": "Our room", "kind": "slack", "secret_in": "A_PRETEND_SLACK",
        })
        with mock.patch.dict(os.environ, {"A_PRETEND_SLACK": self.A_REAL_LOOKING_ONE}):
            said = CredentialRedactor(self.config).text(
                f"we posted to {self.A_REAL_LOOKING_ONE} and it broke"
            )
        self.assertNotIn("thisIsTheSecret", said)


class NothingHereHoldsUpARun(TellingTestCase):
    """The timeout on a socket does not cover looking a name up, because the
    name is looked up before there is a socket to put a timeout on.

    A hostname pointing at a nameserver that never answers hung the whole
    nightly run - not the message, the run - and the lock it held meant every
    firing after it stood aside saying the last one was still going.
    """

    def test_something_that_never_answers_is_given_up_on(self) -> None:
        import time

        one = self.a_way()
        stop = threading.Event()
        self.addCleanup(stop.set)

        def never_answers(address, body):
            stop.wait(60)

        with mock.patch.object(telling, "LONGEST_WAIT", 0.2),                 mock.patch.object(telling, "A_LITTLE_LONGER", 0.1):
            with self.with_the_key():
                began = time.monotonic()
                said = telling.tell_them(
                    self.config, one, "Nightly", "failed", post=never_answers
                )
                took = time.monotonic() - began
        self.assertFalse(said.sent)
        self.assertIn("stopped waiting", said.note)
        self.assertLess(took, 30, "it did not wait for the far end")

    def test_something_that_answers_in_time_is_not_given_up_on(self) -> None:
        one = self.a_way()
        with self.with_the_key():
            said = telling.tell_them(
                self.config, one, "Nightly", "failed", post=self.catching()
            )
        self.assertTrue(said.sent, said.note)

    def test_what_went_wrong_still_reaches_the_caller(self) -> None:
        """Given up on is not the same as swallowed."""

        one = self.a_way()

        def falls_over(address, body):
            raise OSError("no route")

        with self.with_the_key():
            said = telling.tell_them(
                self.config, one, "Nightly", "failed", post=falls_over
            )
        self.assertFalse(said.sent)
        self.assertIn("could not be reached", said.note)


class OnlyWhatThatKindUses(TellingTestCase):
    def test_a_kind_with_no_mail_server_does_not_keep_one(self) -> None:
        """The panel fills that box in for email. Left in place when somebody
        changed their mind, a Slack room spent the rest of its life
        complaining about a mail server nobody had set."""

        one = telling.read_it({
            "name": "Our room", "kind": "slack", "secret_in": "A_PRETEND_SLACK",
            "server_in": "MAIL_SERVER",
        })
        self.assertEqual(one.server_in, "")
        with mock.patch.dict(os.environ, {"A_PRETEND_SLACK": "https://x.invalid/h"}):
            self.assertEqual(telling.why_it_cannot_be_used(one), "")

    def test_email_still_keeps_the_one_it_needs(self) -> None:
        one = telling.read_it({
            "name": "By mail", "kind": "email", "secret_in": "MAIL_PASSWORD",
            "server_in": "OUR_MAIL_SERVER", "to": "them@example.com",
            "sent_from": "us@example.com",
        })
        self.assertEqual(one.server_in, "OUR_MAIL_SERVER")


class TheKeyInUseIsHiddenWhateverElseFails(TellingTestCase):
    """Found by looking in a folder, the looking can quietly find none.

    The folder was briefly a file rather than a folder - something a
    half-finished write or a syncing tool can do on its own - and the very key
    this exists to hide went out in plain text, with the message still saying
    "Told Our room." Nothing said the protection had switched itself off.
    """

    A_REAL_LOOKING_ONE = "https://hooks.slack.invalid/services/T00/B11/thisIsTheSecret"

    def test_it_is_hidden_even_when_the_folder_cannot_be_read(self) -> None:
        telling.save(self.config, {
            "name": "Our room", "kind": "slack", "secret_in": "A_PRETEND_SLACK",
        })
        one = telling.every_one(self.config)[0]
        # And now the folder is not a folder any more.
        where = telling.folder(self.config)
        for path in where.iterdir():
            path.unlink()
        where.rmdir()
        where.write_text("not a folder at all", encoding="utf-8")

        with mock.patch.dict(os.environ, {"A_PRETEND_SLACK": self.A_REAL_LOOKING_ONE}):
            telling.tell_them(
                self.config, one, "Nightly",
                f"it printed {self.A_REAL_LOOKING_ONE} on the way out",
                post=self.catching(),
            )
        # In the message, which is what goes into the room. The address itself
        # is the webhook, and posting to it is the whole point.
        _address, body = self.sent[-1]
        self.assertNotIn("thisIsTheSecret", json.dumps(body))
        self.assertIn("it printed", json.dumps(body))

    def test_the_names_it_hands_over_start_with_its_own(self) -> None:
        one = telling.read_it({
            "name": "By mail", "kind": "email", "secret_in": "MAIL_PASSWORD",
            "server_in": "MAIL_SERVER", "to": "a@b.example", "sent_from": "c@d.example",
        })
        names = telling._every_key_name(self.config, one)
        self.assertIn("MAIL_PASSWORD", names)
        self.assertIn("MAIL_SERVER", names)

    def test_a_redactor_can_be_told_a_name_by_hand(self) -> None:
        from our_harness.redaction import CredentialRedactor

        with mock.patch.dict(os.environ, {"A_PRETEND_SLACK": self.A_REAL_LOOKING_ONE}):
            said = CredentialRedactor(None, also_hide=["A_PRETEND_SLACK"]).text(
                f"we posted to {self.A_REAL_LOOKING_ONE}"
            )
        self.assertNotIn("thisIsTheSecret", said)


class StuckSendsAreCounted(TellingTestCase):
    """A thread waiting on a name that never resolves cannot be killed - Python
    has no safe way - so it is left. One bad address hit every night was one
    thread a night, for ever, with nothing watching."""

    def setUp(self) -> None:
        super().setUp()
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        # Whatever this test leaves stuck, the next one starts clean.
        was = telling._how_many_stuck
        self.addCleanup(lambda: setattr(telling, "_how_many_stuck", was))

    def test_past_the_limit_it_stops_starting_more(self) -> None:
        one = self.a_way()

        def never_answers(address, body):
            self.stop.wait(120)

        said = []
        with mock.patch.object(telling, "LONGEST_WAIT", 0.05),                 mock.patch.object(telling, "A_LITTLE_LONGER", 0.05):
            with self.with_the_key():
                for _ in range(telling.STUCK_ONES_ALLOWED + 2):
                    said.append(
                        telling.tell_them(
                            self.config, one, "Nightly", "failed", post=never_answers
                        )
                    )
        self.assertTrue(all(not sent.sent for sent in said))
        self.assertIn("already stuck", said[-1].note)
        self.assertIn("harness tell list", said[-1].note, "and what to do about it")
        self.assertLessEqual(telling._how_many_stuck, telling.STUCK_ONES_ALLOWED)

    def test_one_that_comes_back_late_stops_being_counted(self) -> None:
        one = self.a_way()
        letting_go = threading.Event()

        def slow(address, body):
            letting_go.wait(10)

        before = telling._how_many_stuck
        with mock.patch.object(telling, "LONGEST_WAIT", 0.05),                 mock.patch.object(telling, "A_LITTLE_LONGER", 0.05):
            with self.with_the_key():
                telling.tell_them(self.config, one, "Nightly", "failed", post=slow)
        self.assertEqual(telling._how_many_stuck, before + 1)
        letting_go.set()
        for _ in range(200):
            if telling._how_many_stuck == before:
                break
            time.sleep(0.02)
        self.assertEqual(telling._how_many_stuck, before)


class OnlySoManyOfThem(TellingTestCase):
    def test_past_the_limit_it_says_to_take_one_off(self) -> None:
        """Every one of these is read whenever anything anywhere is cleaned."""

        for number in range(telling.MOST_WAYS):
            self.a_way(name=f"Room {number}")
        with self.assertRaises(telling.TellingError) as caught:
            self.a_way(name="One too many")
        self.assertIn("Take one off", str(caught.exception))

    def test_saving_one_that_is_already_there_is_not_one_too_many(self) -> None:
        for number in range(telling.MOST_WAYS):
            self.a_way(name=f"Room {number}")
        self.assertTrue(self.a_way(name="Room 0", secret_in="A_DIFFERENT_KEY"))


class LookingForTheNamesIsNotSlow(TellingTestCase):
    def test_a_folder_full_of_them_does_not_cost_a_second_every_time(self) -> None:
        """Read fresh every single time, three thousand of them put a second
        onto every cleaning anywhere in the harness - and cleaning happens on
        paths that have nothing to do with telling anybody anything."""

        from our_harness import redaction

        where = telling.folder(self.config)
        where.mkdir(parents=True, exist_ok=True)
        for number in range(600):
            (where / f"room-{number}.json").write_text(
                json.dumps({"name": f"Room {number}", "kind": "webhook",
                            "secret_in": f"KEY_{number}"}),
                encoding="utf-8",
            )
        redaction._names_last_looked.clear()
        began = time.monotonic()
        for _ in range(20):
            redaction.CredentialRedactor(self.config)
        took = time.monotonic() - began
        self.assertLess(took, 5.0, "twenty cleanings should not take five seconds")

    def test_it_reads_no_more_than_it_said_it_would(self) -> None:
        from our_harness import redaction

        where = telling.folder(self.config)
        where.mkdir(parents=True, exist_ok=True)
        for number in range(redaction.MOST_TELLING_FILES + 50):
            (where / f"room-{number}.json").write_text(
                json.dumps({"name": f"Room {number}", "kind": "webhook",
                            "secret_in": f"KEY_{number}"}),
                encoding="utf-8",
            )
        redaction._names_last_looked.clear()
        found = redaction._names_of_keys_for_telling_somebody(self.config)
        self.assertLessEqual(len(found), redaction.MOST_TELLING_FILES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
