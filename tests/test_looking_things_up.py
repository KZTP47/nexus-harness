"""Looking things up in the code, with and without a language server.

The whole point of this is the difference between an exact answer and a guess.
A guess called a guess is useful. A guess called an answer sends somebody to the
wrong place and wastes their afternoon, so every test here checks the label as
well as the places.

The tests do not need a real language server installed. One is written into the
temporary folder: a small program that speaks the same protocol and answers a
fixed thing. That way these run the same on a fresh machine as on this one.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import navigate
from our_harness.config import DEFAULT_CONFIG, LoadedConfig

# A language server, small enough to read. It answers the three questions with
# one fixed place each, which is all these tests need it to do.
A_STAND_IN_SERVER = r'''
import json, sys

def read():
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        said = line.decode("utf-8").strip()
        if not said:
            break
        if said.lower().startswith("content-length:"):
            length = int(said.split(":", 1)[1])
    if not length:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

def write(message):
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()

HERE = json.loads(open(sys.argv[1], encoding="utf-8").read())

while True:
    said = read()
    if said is None:
        break
    method = said.get("method")
    if method == "exit":
        break
    if "id" not in said:
        continue
    if method == "initialize":
        write({"jsonrpc": "2.0", "id": said["id"], "result": {"capabilities": {}}})
    elif method in ("textDocument/definition", "textDocument/references"):
        write({"jsonrpc": "2.0", "id": said["id"], "result": HERE[method]})
    elif method == "textDocument/hover":
        write({"jsonrpc": "2.0", "id": said["id"], "result": HERE[method]})
    else:
        write({"jsonrpc": "2.0", "id": said["id"], "result": None})
'''


class LookingUpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        (self.root / "shop.py").write_text(
            "def add_up(prices):\n"
            "    return sum(prices)\n"
            "\n"
            "\n"
            "def bill(prices):\n"
            "    return add_up(prices)\n",
            encoding="utf-8",
        )

    def a_stand_in_server(self, answers: dict[str, object]):
        """Put a small language server on the machine, for .py files only."""

        program = self.root / "stand_in_server.py"
        program.write_text(A_STAND_IN_SERVER, encoding="utf-8")
        said = self.root / "stand_in_answers.json"
        said.write_text(json.dumps(answers), encoding="utf-8")
        return mock.patch.object(
            navigate,
            "KNOWN_SERVERS",
            (
                (
                    "stand-in",
                    "The stand-in",
                    (sys.executable, str(program), str(said)),
                    (".py",),
                    "it is already here",
                ),
            ),
        )

    def where_the_server_says(self, line: int) -> dict[str, object]:
        uri = (self.root / "shop.py").as_uri()
        span = {"start": {"line": line - 1, "character": 4}, "end": {"line": line - 1, "character": 10}}
        return {
            "textDocument/definition": [{"uri": uri, "range": span}],
            "textDocument/references": [{"uri": uri, "range": span}],
            "textDocument/hover": {"contents": {"value": "add_up(prices) -> int"}},
        }


class WithAServer(LookingUpTestCase):
    def test_where_is_it_is_marked_exact(self) -> None:
        with self.a_stand_in_server(self.where_the_server_says(1)):
            answer = navigate.look_it_up(
                self.config, asking="where-is-it", path="shop.py", line=6, column=12
            )
        self.assertTrue(answer.exact, "a server answered, so this is not a guess")
        self.assertIn("stand-in", answer.how)
        self.assertEqual([one.path for one in answer.places], ["shop.py"])
        self.assertEqual(answer.places[0].line, 1)
        # The line itself is read back, so somebody can see what they are being
        # sent to without opening the file.
        self.assertEqual(answer.places[0].text, "def add_up(prices):")

    def test_what_uses_it_asks_for_the_declaration_too(self) -> None:
        with self.a_stand_in_server(self.where_the_server_says(5)):
            answer = navigate.look_it_up(
                self.config, asking="what-uses-it", path="shop.py", line=1, column=5
            )
        self.assertTrue(answer.exact)
        self.assertEqual(answer.places[0].line, 5)

    def test_what_is_it_comes_back_as_words(self) -> None:
        with self.a_stand_in_server(self.where_the_server_says(1)):
            answer = navigate.look_it_up(
                self.config, asking="what-is-it", path="shop.py", line=1, column=5
            )
        self.assertTrue(answer.exact)
        self.assertEqual(answer.places[0].what, "add_up(prices) -> int")

    def test_a_server_that_found_nothing_says_so_plainly(self) -> None:
        with self.a_stand_in_server({
            "textDocument/definition": [],
            "textDocument/references": [],
            "textDocument/hover": None,
        }):
            answer = navigate.look_it_up(
                self.config, asking="where-is-it", path="shop.py", line=2, column=1
            )
        self.assertTrue(answer.exact)
        self.assertEqual(answer.places, [])
        # Nothing found by a tool that knows the file is a different thing from
        # nothing found because no tool was asked.
        self.assertIn("found nothing", answer.note)

    def test_a_file_outside_the_project_is_refused(self) -> None:
        with self.a_stand_in_server(self.where_the_server_says(1)):
            with self.assertRaises(Exception) as caught:
                navigate.look_it_up(
                    self.config, asking="where-is-it", path="../elsewhere.py", line=1
                )
        self.assertNotIn("Traceback", str(caught.exception))


# A language server that starts and then says nothing, ever. Real ones do this
# while they read a big project for the first time, and broken ones do it for
# good. Reading from a pipe waits until something arrives, so this is the shape
# of hang that holds a thread until the machine is turned off.
A_SILENT_SERVER = """
import time
time.sleep(600)
"""


class AServerThatWillNotAnswer(LookingUpTestCase):
    def a_silent_server(self):
        program = self.root / "silent_server.py"
        program.write_text(A_SILENT_SERVER, encoding="utf-8")
        return mock.patch.object(
            navigate,
            "KNOWN_SERVERS",
            (("silent", "The silent one", (sys.executable, str(program)), (".py",), "-"),),
        )

    def test_it_gives_up_instead_of_waiting_for_ever(self) -> None:
        with self.a_silent_server():
            with mock.patch.object(navigate, "LONGEST_START_SECONDS", 1.5):
                began = time.monotonic()
                with self.assertRaises(navigate.NavigateError) as caught:
                    navigate.look_it_up(
                        self.config, asking="where-is-it", path="shop.py", line=1, column=5
                    )
                took = time.monotonic() - began
        self.assertLess(took, 20.0, "it waited far longer than it was told to")
        self.assertIn("did not answer", str(caught.exception))

    def test_the_server_is_not_left_running(self) -> None:
        """A leaked language server sits there eating memory until a reboot."""

        started = []
        real = navigate._Talking

        class Watched(real):
            def __init__(self, argv, root):
                super().__init__(argv, root)
                started.append(self)

        with self.a_silent_server():
            with mock.patch.object(navigate, "LONGEST_START_SECONDS", 1.5):
                with mock.patch.object(navigate, "_Talking", Watched):
                    with self.assertRaises(navigate.NavigateError):
                        navigate.look_it_up(
                            self.config, asking="where-is-it", path="shop.py", line=1
                        )
        self.assertEqual(len(started), 1)
        talking = started[0]
        for _ in range(50):
            if talking.process.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(talking.process.poll(), "the server was left running")
        # And both pipes to it are shut, not left for the run to collect.
        for pipe in (talking.process.stdin, talking.process.stdout):
            if pipe is not None:
                self.assertTrue(pipe.closed)


class WithoutAServer(LookingUpTestCase):
    def setUp(self) -> None:
        super().setUp()
        # No server for anything, so every answer has to be the honest guess.
        self.nothing_installed = mock.patch.object(navigate, "KNOWN_SERVERS", (
            ("nothing", "Nothing", ("a-program-that-is-not-here",), (".py",), "install it"),
        ))
        self.nothing_installed.start()
        self.addCleanup(self.nothing_installed.stop)

    def test_a_guess_says_it_is_a_guess(self) -> None:
        answer = navigate.look_it_up(self.config, asking="where-is-it", name="add_up")
        self.assertFalse(answer.exact)
        self.assertIn("guess", answer.note.lower())
        self.assertEqual([one.line for one in answer.places], [1])

    def test_what_uses_it_finds_every_mention(self) -> None:
        answer = navigate.look_it_up(self.config, asking="what-uses-it", name="add_up")
        self.assertFalse(answer.exact)
        self.assertEqual(sorted(one.line for one in answer.places), [1, 6])

    def test_a_mix_of_languages_says_which_ones_you_have_a_tool_for(self) -> None:
        """"Click one of these" is wrong when only some of them would be exact."""

        places = [navigate.Place(path="a.py", line=1), navigate.Place(path="b.go", line=1)]
        program = self.root / "stand_in_server.py"
        program.write_text(A_STAND_IN_SERVER, encoding="utf-8")
        with mock.patch.object(navigate, "KNOWN_SERVERS", (
            ("stand-in", "The stand-in", (sys.executable, str(program)), (".py",), "-"),
        )):
            said = navigate._what_would_make_it_exact(places)
        self.assertIn(".py", said)
        self.assertIn("For the others", said)

    def test_it_does_not_tell_you_to_install_what_you_already_have(self) -> None:
        """Wrong twice: they did install it, and what they must do goes unsaid."""

        program = self.root / "stand_in_server.py"
        program.write_text(A_STAND_IN_SERVER, encoding="utf-8")
        said = self.root / "stand_in_answers.json"
        said.write_text("{}", encoding="utf-8")
        with mock.patch.object(navigate, "KNOWN_SERVERS", (
            ("stand-in", "The stand-in", (sys.executable, str(program), str(said)),
             (".py",), "it is already here"),
        )):
            answer = navigate.look_it_up(self.config, asking="where-is-it", name="add_up")
        self.assertTrue(answer.places)
        self.assertNotIn("Install", answer.note)
        self.assertIn("Click one of these", answer.note)

    def test_what_is_it_cannot_be_guessed_and_says_what_to_do(self) -> None:
        answer = navigate.look_it_up(self.config, asking="what-is-it", name="add_up")
        self.assertFalse(answer.exact)
        self.assertEqual(answer.places, [])
        self.assertIn("language server", answer.note)

    def test_a_file_with_no_server_and_no_name_says_what_is_missing(self) -> None:
        answer = navigate.look_it_up(self.config, asking="where-is-it", path="shop.py", line=1)
        self.assertFalse(answer.exact)
        self.assertIn("Install one", answer.note)

    def test_a_name_that_is_not_a_name_is_refused(self) -> None:
        for bad in ("add up", "add-up", "1add", "a" * 200, "add_up()"):
            with self.subTest(bad=bad):
                with self.assertRaises(navigate.NavigateError):
                    navigate.look_it_up(self.config, asking="where-is-it", name=bad)

    def test_a_question_nobody_asked_is_refused(self) -> None:
        with self.assertRaises(navigate.NavigateError):
            navigate.look_it_up(self.config, asking="what-colour-is-it", name="add_up")



class WhenNothingIsInstalledAtAll(LookingUpTestCase):
    """The real list of languages, and not one of them on this machine."""

    def setUp(self) -> None:
        super().setUp()
        nothing = mock.patch.object(navigate, "_how_to_start", lambda argv: ())
        nothing.start()
        self.addCleanup(nothing.stop)

    def test_it_finds_a_definition_in_every_language_it_knows(self) -> None:
        """A guess for Python only is not a guess for a project in C or Go."""

        written = {
            "shop.c": (
                "int compute_total(int *prices, int many) {\n"
                "    return 0;\n"
                "}\n"
            ),
            "shop.go": (
                "package shop\n"
                "\n"
                "func ComputeTotal(prices []int) int {\n"
                "    return 0\n"
                "}\n"
            ),
            "shop.rs": (
                "pub fn compute_total(prices: &[i32]) -> i32 {\n"
                "    0\n"
                "}\n"
            ),
            "shop.ts": (
                "export function computeTotal(prices: number[]) {\n"
                "  return 0;\n"
                "}\n"
            ),
        }
        for name, body in written.items():
            (self.root / name).write_text(body, encoding="utf-8")
        wanted = {
            "shop.c": "compute_total",
            "shop.go": "ComputeTotal",
            "shop.rs": "compute_total",
            "shop.ts": "computeTotal",
        }
        for where, name in wanted.items():
            with self.subTest(language=where):
                answer = navigate.look_it_up(self.config, asking="where-is-it", name=name)
                found = [one.path for one in answer.places]
                self.assertIn(where, found, f"nothing found for {name}")

    def test_every_language_it_knows_has_a_way_to_look_for_it(self) -> None:
        # C and C++ are read line by line rather than matched against a shape,
        # so they are covered by the reader instead of by the table.
        covered = {one for suffixes, _shapes in navigate.DEFINING for one in suffixes}
        covered |= set(navigate.C_LIKE)
        named = {
            one for _k, _l, _a, suffixes, _h in navigate.KNOWN_SERVERS for one in suffixes
        }
        self.assertEqual(
            named - covered, set(),
            "these file kinds are offered a language server but can never be "
            "searched without one",
        )

    def test_the_shapes_match_definitions_and_not_uses(self) -> None:
        """C is read line by line, and this is the list of what it has to get right."""

        definitions = [
            "int compute_total(int a) {",
            "static void compute_total (void)",
            "std::vector<int> Shop::compute_total(int a) {",
            "int compute_total(int a, int b) { return a + b; }",
            # A constructor, with the fields set before the body opens.
            "Shop::compute_total(int a) : field_(a) {",
            # A signature broken over more than one line.
            "int compute_total(",
            # The older way: the type on the line above, the name below it.
            "compute_total(int *prices)",
            "    compute_total(int a, int b)",
            "#define compute_total(x) 0",
            # Wrapped in the things real C is wrapped in.
            'extern "C" int compute_total(void) {',
            "API_EXPORT int compute_total(void) {",
            "static inline int compute_total(int a) { return a; }",
            "template <typename T> T compute_total(T a) {",
            "void compute_total(int a = 0) {",
            # Braces in a default argument, which no bracket-counting by hand
            # ever got right.
            "void compute_total(int a, Options opts = {}) {",
            "compute_total(void)",
            "struct compute_total {",
        ]
        uses = [
            "    return compute_total(x);",
            "  compute_total(1);",
            # At the left margin, which is where the old rule let them through.
            "return compute_total(1, 2);",
            "else compute_total(1, 2);",
            "compute_total(i, i);",
            "    if (compute_total(a, b))",
            "    x = compute_total(a, b);",
            # A header saying it exists somewhere. True, and not where it is.
            "int compute_total(int a, int b);",
            "// compute_total is the one",
            "   * compute_total() adds up",
            # A call on a line of its own, with no semicolon after it. Nothing
            # in the brackets is shaped like an argument, so it is not one.
            "compute_total(1)",
            "    compute_total(a, b)",
            "#if compute_total(1)",
            "Thing t = compute_total(1);",
            # A variable holding a pointer to one, which is not where it is.
            "int (*compute_total)(void);",
            # The same as two of the uses above with the semicolon left off,
            # which is how the rule about semicolons was got round.
            "return compute_total(1, 2)",
            "else compute_total(1, 2)",
        ]
        for line in definitions:
            with self.subTest(line=line):
                self.assertTrue(navigate._a_c_definition(line, "compute_total"))
        for line in uses:
            with self.subTest(line=line):
                self.assertFalse(navigate._a_c_definition(line, "compute_total"))

    def test_a_signature_written_out_in_a_comment_is_not_a_place(self) -> None:
        """An example in a comment reads exactly like the real thing."""

        for line in (
            "// int compute_total(int a) {",
            "  /* int compute_total(int a) { */",
            "int x = 1; // compute_total(int a) {",
        ):
            with self.subTest(line=line):
                self.assertFalse(navigate._a_c_definition(line, "compute_total"))
        # And a real one with a comment on the end of it is still real.
        for line in (
            "int compute_total(int a) {  // adds up",
            "int compute_total(int a) { /* adds up */ }",
        ):
            with self.subTest(line=line):
                self.assertTrue(navigate._a_c_definition(line, "compute_total"))

    def test_a_signature_printed_inside_a_piece_of_text_is_not_a_place(self) -> None:
        self.assertFalse(
            navigate._a_c_definition('printf("compute_total(%d) {");', "compute_total")
        )
        # A comment opener inside a piece of text does not start a comment.
        self.assertTrue(
            navigate._a_c_definition(
                'const char *s = "/*"; int compute_total(int a) {', "compute_total"
            )
        )

    def test_a_line_carried_on_from_a_comment_is_still_in_it(self) -> None:
        code, still = navigate._the_code_on_this_line("int f(void) {", True)
        self.assertTrue(still)
        self.assertEqual(code, "")
        code, still = navigate._the_code_on_this_line("*/ int f(void) {", True)
        self.assertFalse(still)
        self.assertIn("int f(void) {", code)

    def test_a_type_is_told_from_a_variable_of_that_type(self) -> None:
        """"struct Thing x;" says there is a Thing here. It is not where Thing is."""

        yes = [
            "struct compute_total {",
            "struct compute_total",
            "class compute_total : public Base {",
        ]
        no = [
            "    struct compute_total local_var;",
            "    struct compute_total *p;",
            "struct compute_total make_one(void) {",
            # Saying it exists somewhere. True, and not where it is.
            "struct compute_total;",
        ]
        for line in yes:
            with self.subTest(line=line):
                self.assertTrue(navigate._a_c_definition(line, "compute_total"))
        for line in no:
            with self.subTest(line=line):
                self.assertFalse(navigate._a_c_definition(line, "compute_total"))

    def test_the_things_real_headers_wrap_a_struct_in(self) -> None:
        """None of these is unusual, and all of them used to hide the name."""

        for line in (
            "struct compute_total final : Base {",
            "enum class compute_total : int {",
            "enum struct compute_total {",
            "template <> struct compute_total<int> {",
            "struct __attribute__((packed)) compute_total {",
            "struct compute_total __attribute__((packed)) {",
            "class API_EXPORT compute_total {",
        ):
            with self.subTest(line=line):
                self.assertTrue(navigate._a_c_definition(line, "compute_total"))

    def test_a_line_of_chained_calls_does_not_hold_it_up(self) -> None:
        """Counting the brackets again for every place took the line squared.

        Forty thousand letters took fourteen seconds. Generated C is full of
        lines like this one.
        """

        line = "foo(" * 30_000 + ")" * 30_000
        began = time.monotonic()
        navigate._a_c_definition(line, "foo")
        self.assertLess(time.monotonic() - began, 2.0)

    def test_a_name_inside_a_bracket_is_being_handed_to_something(self) -> None:
        """Debris from an unclosed bracket read as a type and made a definition."""

        self.assertFalse(navigate._a_c_definition("foo(a, foo(b, c", "foo"))
        self.assertFalse(navigate._a_c_definition("    if (compute_total(a, b))",
                                                  "compute_total"))
        # And the one bracket that is part of the name, not around it.
        self.assertTrue(
            navigate._a_c_definition("int (*get_handler(int type))(int, int) {",
                                     "get_handler")
        )

    def test_text_kept_exactly_as_written_does_not_swallow_the_line(self) -> None:
        """C++ raw text holds quotes and brackets, and ate the rest of the line."""

        line = 'const char *s = R"(a "quote)"; int compute_total(int a) {'
        self.assertTrue(navigate._a_c_definition(line, "compute_total"))

    def test_a_typedef_over_two_lines_is_found(self) -> None:
        (self.root / "kinds.c").write_text(
            "typedef int\n(*compute_total)(int);\n", encoding="utf-8"
        )
        answer = navigate.look_it_up(
            self.config, asking="where-is-it", name="compute_total"
        )
        self.assertEqual([one.line for one in answer.places if one.path == "kinds.c"], [2])

    def test_rust_impl_says_where_it_is_used_not_where_it_lives(self) -> None:
        shapes = navigate._what_a_definition_looks_like(".rs", "ComputeTotal")
        self.assertFalse(any(one.search("impl ComputeTotal for Foo {") for one in shapes))
        self.assertTrue(any(one.search("trait ComputeTotal {") for one in shapes))

    def test_a_label_is_not_a_type(self) -> None:
        """The line above a bare bracket decides, so what counts matters."""

        for line in ("done:", "case X:", "public:", "", "    x = 1;", "}",
                     # One lowercase word on its own is a leftover, not a type.
                     "foo", "leftover"):
            with self.subTest(line=line):
                self.assertFalse(navigate._only_a_type(line))
        for line in ("int", "static void", "std::vector<int>", "const char *",
                     "size_t", "MyClass", "uint32_t"):
            with self.subTest(line=line):
                self.assertTrue(navigate._only_a_type(line))

    def test_a_typedef_makes_the_name_at_the_end_of_it(self) -> None:
        """"typedef A B;" is where B lives, not where A does."""

        yes = [
            # How most C names a struct: no name until the closing line.
            "typedef struct { int total; } compute_total;",
            "} compute_total;",
            "typedef struct Shop compute_total;",
            "typedef int (*compute_total)(int);",
        ]
        no = ["typedef compute_total new_alias_t;"]
        for line in yes:
            with self.subTest(line=line):
                self.assertTrue(navigate._a_c_definition(line, "compute_total"))
        for line in no:
            with self.subTest(line=line):
                self.assertFalse(navigate._a_c_definition(line, "compute_total"))

    def test_a_signature_over_three_lines_is_found(self) -> None:
        """The middle line carries only brackets, so the line above decides it."""

        (self.root / "wide.c").write_text(
            "int\n"
            "compute_total(\n"
            "    int a, int b)\n"
            "{\n"
            "    return a + b;\n"
            "}\n",
            encoding="utf-8",
        )
        answer = navigate.look_it_up(
            self.config, asking="where-is-it", name="compute_total"
        )
        lines = [one.line for one in answer.places if one.path == "wide.c"]
        self.assertEqual(lines, [2])

    def test_a_call_split_over_two_lines_is_not_a_definition(self) -> None:
        """The other half of the same rule: inside a function there is no type above."""

        (self.root / "caller.c").write_text(
            "void run(void) {\n"
            "    int x = 1;\n"
            "compute_total(\n"
            "    x, x);\n"
            "}\n",
            encoding="utf-8",
        )
        answer = navigate.look_it_up(
            self.config, asking="where-is-it", name="compute_total"
        )
        self.assertEqual([one.line for one in answer.places if one.path == "caller.c"], [])

    def test_a_line_that_uses_it_and_then_defines_it_still_counts(self) -> None:
        self.assertTrue(
            navigate._a_c_definition(
                "assert(compute_total(1) > 0); int compute_total(int a) { return a; }",
                "compute_total",
            )
        )

    def test_a_thing_kept_in_a_variable_is_found(self) -> None:
        self.assertTrue(
            navigate._a_c_definition(
                "auto compute_total = [](int a, int b) { return a + b; };",
                "compute_total",
            )
        )
        self.assertFalse(
            navigate._a_c_definition("    compute_total = [](int a) { };", "compute_total")
        )

    def test_a_rust_function_shared_with_c_is_found(self) -> None:
        shapes = navigate._what_a_definition_looks_like(".rs", "compute_total")
        self.assertTrue(
            any(one.search('pub extern "C" fn compute_total() -> i32 { 0 }')
                for one in shapes)
        )

    def test_a_function_giving_back_a_function_is_found(self) -> None:
        self.assertTrue(
            navigate._a_c_definition("int (*get_handler(int type))(int, int) {",
                                     "get_handler")
        )

    def test_a_rust_function_with_words_in_front_of_it_is_found(self) -> None:
        shapes = navigate._what_a_definition_looks_like(".rs", "compute_total")
        for line in (
            "pub async fn compute_total() -> i32 {",
            "pub unsafe fn compute_total() -> i32 {",
            "pub const fn compute_total() -> i32 {",
            "async fn compute_total() -> i32 {",
            "pub(crate) fn compute_total() {",
        ):
            with self.subTest(line=line):
                self.assertTrue(any(one.search(line) for one in shapes))

    def test_a_very_long_line_does_not_hold_the_search_up(self) -> None:
        """Reading one long line took thirty-six seconds when it was matched.

        The shape it was matched against had two loose runs around a name,
        which is the arrangement that makes a search try every way of splitting
        the line. The lines below are the ones that set it off.
        """

        lines = [
            "compute_total(" + "a a " * 100_000 + "z",
            "int " + "a" * 200_000 + " something_else(void) {",
            "int compute_total(" + "int a, " * 20_000 + "int z) {",
        ]
        for line in lines:
            with self.subTest(length=len(line)):
                began = time.monotonic()
                navigate._a_c_definition(line, "compute_total")
                self.assertLess(time.monotonic() - began, 2.0)
        for suffix in (".py", ".ts", ".rs", ".go"):
            shapes = navigate._what_a_definition_looks_like(suffix, "compute_total")
            with self.subTest(language=suffix):
                began = time.monotonic()
                for one in shapes:
                    one.search(lines[0])
                self.assertLess(time.monotonic() - began, 2.0)

    def test_a_signature_inside_a_block_comment_is_not_a_place(self) -> None:
        """An example in a comment is not where the thing lives."""

        (self.root / "notes.c").write_text(
            "/*\n"
            " Example:\n"
            "int compute_total(int a, int b) {\n"
            "*/\n"
            "int compute_total(int a, int b) { return a + b; }\n",
            encoding="utf-8",
        )
        answer = navigate.look_it_up(
            self.config, asking="where-is-it", name="compute_total"
        )
        lines = [one.line for one in answer.places if one.path == "notes.c"]
        self.assertEqual(lines, [5], "a line inside a comment came back as a place")

    def test_a_go_method_on_a_pointer_is_found(self) -> None:
        shapes = navigate._what_a_definition_looks_like(".go", "ComputeTotal")
        self.assertTrue(
            any(one.search("func (s *Shop) ComputeTotal(prices []int) int {")
                for one in shapes)
        )

    def test_a_javascript_arrow_kept_in_a_const_is_found(self) -> None:
        shapes = navigate._what_a_definition_looks_like(".js", "computeTotal")
        self.assertTrue(
            any(one.search("export const computeTotal = (prices) => 0;") for one in shapes)
        )

    def test_a_call_is_not_mistaken_for_a_definition(self) -> None:
        """C's shape matches a plain line of Python, so it must not be used on one."""

        (self.root / "uses.py").write_text(
            "from shop import add_up\n\nanswer = add_up([1, 2])\n", encoding="utf-8"
        )
        answer = navigate.look_it_up(self.config, asking="where-is-it", name="add_up")
        self.assertEqual([one.path for one in answer.places], ["shop.py"])

    def test_a_c_call_without_braces_around_it_is_not_a_definition(self) -> None:
        """The body of a brace-less loop sits at the margin, like a definition."""

        (self.root / "batch.c").write_text(
            "int compute_total(int a, int b) {\n"
            "    return a + b;\n"
            "}\n"
            "\n"
            "void run_batch(void) {\n"
            "for (int i = 0; i < 10; i++)\n"
            "compute_total(i, i);\n"
            "}\n",
            encoding="utf-8",
        )
        answer = navigate.look_it_up(
            self.config, asking="where-is-it", name="compute_total"
        )
        lines = [one.line for one in answer.places if one.path == "batch.c"]
        self.assertEqual(lines, [1], "a call came back as a place it is defined")

    def test_a_header_saying_it_exists_is_not_where_it_is(self) -> None:
        """True, and not the answer to "where is it"."""

        (self.root / "shop.h").write_text(
            "int compute_total(int a, int b);\n", encoding="utf-8"
        )
        (self.root / "shop2.c").write_text(
            "int compute_total(int a, int b) {\n    return a + b;\n}\n", encoding="utf-8"
        )
        answer = navigate.look_it_up(
            self.config, asking="where-is-it", name="compute_total"
        )
        found = [one.path for one in answer.places]
        self.assertIn("shop2.c", found)
        self.assertNotIn("shop.h", found)


class ReadingWhatAServerSays(unittest.TestCase):
    def test_a_folder_with_a_space_in_its_name_survives(self) -> None:
        """This project's own folder has a space in it, so this is not a corner."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "A Folder With Spaces"
            (root / "src").mkdir(parents=True)
            where = root / "src" / "a file.py"
            where.write_text("x = 1\n", encoding="utf-8")
            said = navigate._from_a_uri(where.as_uri(), root)
        self.assertEqual(said, "src/a file.py")
        self.assertNotIn("%20", said)

    def test_something_outside_the_project_keeps_its_own_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "project"
            root.mkdir()
            outside = Path(temporary).resolve() / "library.py"
            outside.write_text("y = 2\n", encoding="utf-8")
            said = navigate._from_a_uri(outside.as_uri(), root)
        self.assertIn("library.py", said)
        self.assertNotIn("%20", said)

    def test_an_empty_address_is_an_empty_answer(self) -> None:
        self.assertEqual(navigate._from_a_uri("", Path.cwd()), "")

    def test_the_list_of_tools_says_how_to_get_each_one(self) -> None:
        found = navigate.what_is_on_this_machine()
        self.assertTrue(found)
        for one in found:
            with self.subTest(tool=one["key"]):
                self.assertTrue(one["label"])
                self.assertTrue(one["for_files"])
                # A missing tool is only useful if it says how to get it.
                self.assertTrue(one["how_to_get_it"])
                self.assertIsInstance(one["ready"], bool)


if __name__ == "__main__":
    unittest.main()
