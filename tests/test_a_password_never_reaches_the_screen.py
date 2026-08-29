"""A password written into an address, and the three places it used to get out.

Somebody pointing at an internal gateway writes the name and password into the
address, because that is how a lot of gateways are reached. Every named route
was checked for that; the plain one - the setting a person makes before they
know routes exist - was not. So it went into the settings file, and from there
onto the screen the first time anything went wrong, because the failure it
caused was not the shape of failure anything caught.

Three layers, and all three are needed:

  - the settings refuse it,
  - a request that cannot be made says so as a sentence, with the address taken
    out of it,
  - and whatever still gets past both is redacted on its way to the screen.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from our_harness import chat, server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig, validate_config
from our_harness.models import HarnessError

A_PASSWORD = "supersecretpassword"
WITH_A_PASSWORD = f"https://someone:{A_PASSWORD}@gateway.example/v1"


class TheSettingsRefuseIt(unittest.TestCase):
    def a_config(self, **changes) -> dict:
        held = copy.deepcopy(DEFAULT_CONFIG)
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(held.get(key), dict):
                held[key].update(value)
            else:
                held[key] = value
        return held

    def test_the_plain_address_is_refused(self) -> None:
        """The one a person sets without knowing routes exist."""

        with self.assertRaises(HarnessError) as caught:
            validate_config(self.a_config(provider={
                "name": "openai-compatible", "model": "m",
                "endpoint": WITH_A_PASSWORD, "api_key_env": "",
            }))
        said = str(caught.exception)
        self.assertIn("name and password", said)
        self.assertIn("api_key_env", said, "and it says what to do instead")

    def test_a_named_route_is_refused_in_the_same_words(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            validate_config(self.a_config(providers={"gw": {
                "kind": "openai-compatible", "model": "m", "endpoint": WITH_A_PASSWORD,
            }}))
        self.assertIn("name and password", str(caught.exception))

    def test_a_query_and_a_hash_part_are_refused_too(self) -> None:
        for address in ("https://gateway.example/v1?key=abc",
                        "https://gateway.example/v1#key"):
            with self.subTest(address=address):
                with self.assertRaises(HarnessError):
                    validate_config(self.a_config(provider={
                        "name": "openai-compatible", "model": "m",
                        "endpoint": address, "api_key_env": "",
                    }))

    def test_an_ordinary_address_is_still_allowed(self) -> None:
        validate_config(self.a_config(provider={
            "name": "openai-compatible", "model": "m",
            "endpoint": "https://gateway.example/v1", "api_key_env": "GATEWAY_KEY",
        }))


class AndIfOneGetsPastAnyway(unittest.TestCase):
    """Belt and braces: the settings are one layer, not the only one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"one": {"kind": "claude-cli", "model": "a"}}
        self.config = LoadedConfig(data, self.root, [], {})
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        threading.Thread(target=self.panel.serve_forever, daemon=True).start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Harness-Token": self.panel.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_a_failure_nothing_expected_is_still_said_without_the_password(self) -> None:
        """The last resort. Anything reaching it has been through no redactor."""

        class Bursts:
            def complete(self, request):
                # Not a HarnessError, so nothing on the way up catches it - the
                # exact shape an address the machine cannot take apart makes.
                raise ValueError(f"nonnumeric port: '{A_PASSWORD}@gateway.example'")

        with mock.patch.object(chat, "create_provider", lambda config: Bursts()):
            status, said = self.ask("/api/chat/say", {"who": "one", "text": "hello"})
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertNotIn(A_PASSWORD, said["error"], said["error"])

    def test_a_key_in_a_failure_nothing_expected_is_taken_out_too(self) -> None:
        class Bursts:
            def complete(self, request):
                raise ValueError("rejected: sk-abcdef0123456789abcdef01")

        with mock.patch.object(chat, "create_provider", lambda config: Bursts()):
            _status, said = self.ask("/api/chat/say", {"who": "one", "text": "hello"})
        self.assertNotIn("sk-abcdef0123456789abcdef01", said["error"], said["error"])


class TellingAPageFromASentence(unittest.TestCase):
    """Cutting angle brackets out of ordinary words loses the useful part."""

    def test_ordinary_words_are_left_exactly_as_they_were(self) -> None:
        for words in (
            # A tag named twice, once opening and once closing, which is what a
            # local model's own template complains about.
            "Chat template error: expected </div> after <div> in system prompt",
            "xml was <note><to>you</to></note> and it failed",
            "git diff --stat <head>..<branch> failed: no such ref",
            "retry after <2026-08-18T10:00:00Z> or check <head> of queue",
            "bad request: expected <head> element before <body>",
            "Error: type mismatch, expected List<Item> but got Array<int>",
            "bash: <stdin>: syntax error near unexpected token",
            "use <name> to set it",
        ):
            with self.subTest(words=words):
                self.assertEqual(chat._without_markup(words), words)

    def test_it_never_cuts_words_out_of_what_it_does_not_understand(self) -> None:
        """The rule underneath all of this, said once.

        Either there is a heading to lift out of a page, or the whole thing is
        plainly a document, or the words come back exactly as they came.
        """

        for words in (
            "expected </p> after <p>",
            "<custom-tag>value</custom-tag> was refused",
            "closing </span> without <span>",
        ):
            with self.subTest(words=words):
                self.assertEqual(chat._without_markup(words), words)

    def test_a_whole_page_comes_back_as_one_line_with_every_word(self) -> None:
        """Every word, not the heading. The heading says "Error response";
        the sentence under it says what actually went wrong."""

        said = chat._without_markup(
            "Provider HTTP 501: <!DOCTYPE HTML><html><head><title>Error response"
            "</title></head><body><h1>Error response</h1>"
            "<p>Message: Unsupported method.</p></body></html>"
        )
        self.assertNotIn("<", said)
        self.assertIn("Provider HTTP 501", said, "what came before the page is kept")
        self.assertIn("Unsupported method", said, "and the useful sentence survives")

    def test_a_page_keeps_its_words_and_drops_its_stylesheet(self) -> None:
        said = chat._without_markup(
            "<html><head><style>body{color:red}</style></head>"
            "<body><p>Access denied by policy</p></body></html>"
        )
        self.assertEqual(said, "Access denied by policy")

    def test_somebody_elses_tag_is_not_a_web_page(self) -> None:
        """A tag has to be `html` and stop there.

        A tag of somebody's own has a dash in it - that is the rule for one -
        and a namespace has a colon. Reading either as the start of a page
        threw away everything the tag was carrying.
        """

        for words in (
            '<html-status data-code="429" data-detail="rate limited, retry in 30s">'
            "Service temporarily degraded</html-status>",
            "<html:body>something</html:body>",
            "<htmlish>not a page</htmlish>",
        ):
            with self.subTest(words=words[:40]):
                self.assertEqual(chat._without_markup(words), words)

    def test_a_real_page_is_still_read_as_one(self) -> None:
        for page, wanted in (
            ("<html><body><p>Access denied by policy</p></body></html>",
             "Access denied by policy"),
            ('<html lang="en"><body><p>Down for now</p></body></html>', "Down for now"),
        ):
            with self.subTest(page=page[:40]):
                self.assertEqual(chat._without_markup(page), wanted)

    def test_words_a_page_keeps_inside_a_tag_are_kept(self) -> None:
        """A page that says why it is down often says it in a meta tag."""

        said = chat._without_markup(
            "<!DOCTYPE html><html><head><title>Maintenance</title>"
            '<meta http-equiv="refresh" '
            'content="30; url=/status?detail=Upstream overloaded, retry in 30s">'
            "</head><body><p>We will be right back.</p></body></html>"
        )
        self.assertIn("Upstream overloaded", said)
        self.assertIn("We will be right back", said)

    def test_an_apostrophe_does_not_end_the_words_early(self) -> None:
        """"it's" is the commonest word in a message about being down.

        Written to stop at either quote, the words ended at the apostrophe -
        "we are down, it" - and the rest went with the tag, leaving a sentence
        that reads as finished.
        """

        said = chat._without_markup(
            "<html><head><meta name=\"description\" content=\"We are down, "
            "it's a scheduled maintenance, back in 10 minutes\">"
            "</head><body><p>Please check back later.</p></body></html>"
        )
        self.assertIn("back in 10 minutes", said)
        self.assertIn("Please check back later", said)

    def test_a_quote_inside_words_held_by_apostrophes_survives_too(self) -> None:
        said = chat._without_markup(
            "<html><head><meta content='He said \"hi\" and left'>"
            "</head><body><p>rest</p></body></html>"
        )
        self.assertIn('He said "hi" and left', said)

    def test_the_awkward_shapes_a_real_page_takes(self) -> None:
        """Four rounds of patterns each got one of these wrong.

        A `>` inside a quoted value ended the tag early. A `<` inside one leaked
        a half-tag into words that were meant to be clean. The word `content`
        inside somebody else's value was read as the message. All of this is
        what a parser is for, and there is one in the standard library.
        """

        page = (
            '<html><head><meta name="description" content="{}"></head>'
            "<body><h1>Down</h1></body></html>"
        )
        for value, wanted in (
            ("Error: 5 > 3, retry later", "Error: 5 > 3, retry later"),
            ("Value < 5, retry", "Value < 5, retry"),
            ("We are down, it's scheduled", "We are down, it's scheduled"),
        ):
            with self.subTest(value=value):
                said = chat._without_markup(page.format(value))
                self.assertIn(wanted, said)
                self.assertIn("Down", said, "and the rest of the page as well")

        # The word content inside another value is not the message.
        said = chat._without_markup(
            "<html><head><meta name='a fake content=\"gotcha\" attr' "
            'content="Real message here"></head><body><h1>Down</h1></body></html>'
        )
        self.assertIn("Real message here", said)
        self.assertNotIn("gotcha", said)

        # And a bracket in an earlier value does not end the tag.
        said = chat._without_markup(
            '<html><head><meta data-x="a>b" content="Real message">'
            "</head><body><h1>Down</h1></body></html>"
        )
        self.assertIn("Real message", said)
        self.assertNotIn("<", said)

    def test_words_are_only_thrown_away_once_the_thing_really_closes(self) -> None:
        """A page can show the word "script" as text, and then never close it.

        A gateway echoing back what was sent does exactly that. Throwing the
        words away as soon as the word appeared left a page saying half of what
        it said, with nothing to show the rest was gone.
        """

        said = chat._without_markup(
            "<html><body><textarea>debug dump: the path was /api/v1/<script>foo"
            "</textarea><p>Message: the reason is a missing header</p>"
            "</body></html>"
        )
        self.assertIn("debug dump", said)
        self.assertIn("missing header", said, "the reason must survive")

    def test_a_script_that_really_closes_is_still_thrown_away(self) -> None:
        said = chat._without_markup(
            '<html><head><script>var a = 1; alert("hi")</script></head>'
            "<body><p>Message: try again</p></body></html>"
        )
        self.assertEqual(said, "Message: try again")

    def test_a_page_trimmed_part_way_keeps_what_it_had_said(self) -> None:
        said = chat._without_markup(
            "<html><body><p>the message we care about</p>" + "<div>" * 5000
        )
        self.assertIn("the message we care about", said)

    def test_a_long_message_that_is_not_a_page_is_not_cut_at_a_stray_bracket(self) -> None:
        """"queue depth < 5 required", then a thousand lines of detail.

        The rule about not leaving half a tag was being applied before anybody
        asked whether this was a page at all, so a single `<` in ordinary words
        threw away everything after it - most of the message - and what was left
        read like a whole sentence.
        """

        words = (
            "Upstream refused the request: queue depth < 5 required. "
            + ("detail line without brackets. " * 700)
        )
        self.assertGreater(len(words), chat.MOST_TO_READ)
        self.assertEqual(chat._without_markup(words), words)
        # And through the path that really shows it to somebody.
        said = chat._in_plain_words(HarnessError(words))
        self.assertIn("detail line", said, "the detail must survive")

    def test_the_shortest_shape_of_the_same_thing(self) -> None:
        words = "AAAAA<" + "B" * 19_995
        self.assertEqual(chat._without_markup(words), words)

    def test_a_page_and_a_json_answer_in_the_same_failure(self) -> None:
        """A gateway can answer with a page and the upstream's JSON after it.

        The branch that pulls the sentence out of the JSON used to hand back
        whatever came before it untouched - tags and all - because it never
        went through the same rule as everything else.
        """

        said = chat._in_plain_words(HarnessError(
            "Provider HTTP 503: <html><body><h1>503 Service Unavailable</h1>"
            '</body></html>{"detail": "Upstream pool exhausted, retry in 30s"}'
        ))
        self.assertNotIn("<", said)
        self.assertIn("503 Service Unavailable", said)
        self.assertIn("Upstream pool exhausted", said)

    def test_a_page_with_a_stylesheet_before_its_json_answer(self) -> None:
        """A block page's own stylesheet holds the first brace on the line.

        Looked for from the left, that brace is found first, the JSON after it
        never parses, and the whole thing comes back with the braces showing.
        """

        said = chat._in_plain_words(HarnessError(
            "Provider HTTP 403: <!DOCTYPE html><html><head>"
            "<style>body{background:#fff;font-family:sans-serif}h1{color:#333}</style>"
            "<title>Attention Required!</title></head><body><h1>Access denied</h1>"
            "<p>Message: blocked by WAF rule 942100</p></body></html>"
            '{"error": "upstream rejected the request"}'
        ))
        self.assertNotIn("{", said)
        self.assertNotIn("<", said)
        self.assertIn("blocked by WAF rule 942100", said)
        self.assertIn("upstream rejected the request", said)

    def test_the_json_a_tool_wraps_its_reason_in_is_still_found(self) -> None:
        """The inner brace is tried first now, so the outer one must still win."""

        said = chat._in_plain_words(HarnessError(
            'claude said: {"type": "result", "is_error": true, '
            '"result": "Your organisation does not have access.", '
            '"usage": {"input_tokens": 4}}'
        ))
        self.assertIn("does not have access", said)
        self.assertNotIn("input_tokens", said)

    def test_a_line_of_braces_does_not_hold_the_thread_up(self) -> None:
        began = time.monotonic()
        chat._in_plain_words(HarnessError("{" * 50_000 + "x"))
        self.assertLess(time.monotonic() - began, 1.0)

    def test_words_before_the_json_that_are_not_a_page_are_left_alone(self) -> None:
        said = chat._in_plain_words(HarnessError(
            'expected </div> after <div> and then {"error": "no"}'
        ))
        self.assertIn("</div>", said, "ordinary words keep their brackets")
        self.assertTrue(said.endswith("no"))

    def test_a_stylesheet_is_not_words(self) -> None:
        said = chat._without_markup(
            "<html><head><style>body{color:red}</style><title>Error</title></head>"
            "<body><p>Message: try again</p></body></html>"
        )
        self.assertIn("Message: try again", said)
        self.assertNotIn("color:red", said)

    def test_written_out_letters_come_back_as_letters(self) -> None:
        self.assertEqual(
            chat._without_markup("<html><body><p>5 &lt; 6 &amp; 7</p></body></html>"),
            "5 < 6 & 7",
        )

    def test_a_page_full_of_meta_tags_does_not_hold_the_thread_up(self) -> None:
        many = "<html>" + ('<meta name="a" content="some words here">' * 4000)
        began = time.monotonic()
        chat._without_markup(many)
        self.assertLess(time.monotonic() - began, 1.0)

    def test_a_garbled_page_does_not_hold_the_thread_up(self) -> None:
        """Written the obvious way, this took a second and a half."""

        for nasty in ("<html>" + "<" * 60_000, "<html>" + "<a" * 30_000):
            with self.subTest(length=len(nasty)):
                began = time.monotonic()
                chat._without_markup(nasty)
                self.assertLess(time.monotonic() - began, 1.0)

    def test_an_incomplete_prefixed_document_opener_is_never_clipped(self) -> None:
        for opener in ("<!doctype html", "<html lang='en'"):
            with self.subTest(opener=opener):
                original = "prefix " + opener + ("<" * 60_000)
                self.assertEqual(chat._without_markup(original), original)

    def test_a_piece_of_a_page_is_left_alone_on_purpose(self) -> None:
        """A fragment is not a page, and guessing which is which is what went wrong.

        This is untidy on screen - the tags show - and it is the trade that was
        chosen: every word is still there. Three attempts at being clever here
        each ended with a sentence that read perfectly and had the diagnosis
        quietly removed.
        """

        fragment = "<h1>502 Bad Gateway</h1><p>nginx/1.18.0</p>"
        self.assertEqual(chat._without_markup(fragment), fragment)
        self.assertIn("502 Bad Gateway", chat._without_markup(fragment))


if __name__ == "__main__":
    unittest.main()
