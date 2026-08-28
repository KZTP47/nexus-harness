"""Looking things up in the code: where it is, what uses it, what it is.

Until now the harness answered these by matching text. That works until two
things share a name, which in real code is always. deepseek-harness answers
them properly, by asking the tool built for that language - the same tool your
editor asks when you press "go to definition".

This does the same. If a language server for the language is on the machine, the
answer is exact and says so. If none is, it falls back to reading the files and
says **that** - a guess called a guess is useful; a guess called an answer sends
somebody to the wrong place.

Nothing here changes a file. Every path is confined to the project the same way
the rest of the harness confines them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from urllib import request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path

# The language servers this knows how to start, and what each one is for. Every
# one of them is a program somebody installs themselves - free, no account.
KNOWN_SERVERS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "python", "Python", ("pylsp",), (".py",),
        "python -m pip install python-lsp-server",
    ),
    (
        "python-pyright", "Python (Pyright)", ("pyright-langserver", "--stdio"), (".py",),
        "npm install -g pyright",
    ),
    (
        "typescript", "TypeScript and JavaScript",
        ("typescript-language-server", "--stdio"),
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        "npm install -g typescript typescript-language-server",
    ),
    (
        "c", "C and C++", ("clangd",),
        (".c", ".h", ".cc", ".cpp", ".hpp", ".cxx"),
        "Install clangd from your package manager, or with LLVM",
    ),
    ("rust", "Rust", ("rust-analyzer",), (".rs",), "rustup component add rust-analyzer"),
    ("go", "Go", ("gopls",), (".go",), "go install golang.org/x/tools/gopls@latest"),
)

# How long one question may take. A language server reads a whole project the
# first time it is asked, which is slow once and quick afterwards.
LONGEST_START_SECONDS = 45.0
LONGEST_ASK_SECONDS = 20.0
# Bounds on what comes back, so one question cannot fill a screen or a machine.
MOST_PLACES = 200
MOST_BYTES = 4_000_000
A_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,120}$")


class NavigateError(HarnessError):
    """A question about the code that could not be answered."""


@dataclass
class Place:
    """One place in the project: a file, a line, and the line itself."""

    path: str
    line: int
    column: int = 0
    text: str = ""
    what: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "text": self.text,
            "what": self.what,
        }


@dataclass
class Answer:
    """What was found, and how sure it is."""

    asked: str
    places: list[Place] = field(default_factory=list)
    exact: bool = False
    how: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asked": self.asked,
            "places": [one.to_dict() for one in self.places],
            "exact": self.exact,
            "how": self.how,
            "note": self.note,
        }


# Some language servers are Python programs. Installing one puts a small
# launcher in a folder that is often not on the path, especially on Windows -
# and then a server that is genuinely installed looks missing. Asking Python to
# run it by name finds it either way.
AS_A_MODULE = {"pylsp": "pylsp"}


def _how_to_start(argv: tuple[str, ...]) -> tuple[str, ...]:
    """The real way to start this server on this machine, or nothing."""

    if shutil.which(argv[0]):
        return argv
    module = AS_A_MODULE.get(argv[0])
    if module and importlib.util.find_spec(module) is not None:
        return (sys.executable, "-m", module, *argv[1:])
    return ()


def what_is_on_this_machine() -> list[dict[str, Any]]:
    """Every language server this knows about, and whether it is installed."""

    found = []
    for key, label, argv, suffixes, how_to_get_it in KNOWN_SERVERS:
        starts_with = _how_to_start(argv)
        found.append({
            "key": key,
            "label": label,
            "command": argv[0],
            "found_at": (shutil.which(argv[0]) or (" ".join(starts_with) if starts_with else "")),
            "ready": bool(starts_with),
            "for_files": list(suffixes),
            "how_to_get_it": how_to_get_it,
        })
    return found


def _server_for(path: Path) -> tuple[str, tuple[str, ...]] | None:
    """The language server to ask about this file, if one is installed."""

    suffix = path.suffix.lower()
    for _key, label, argv, suffixes, _how in KNOWN_SERVERS:
        if suffix not in suffixes:
            continue
        starts_with = _how_to_start(argv)
        if starts_with:
            return label, starts_with
    return None


class _Talking:
    """One language server, started, asked, and stopped again.

    The protocol is plain JSON with a length written above each message. It is
    small enough to speak directly, which is better than adding something to
    install in order to talk to something else you install.

    Reading happens on its own thread, and everything it reads goes on a queue.
    That is not tidiness: reading from a pipe blocks until something arrives,
    and a language server that says nothing at all - still indexing, or simply
    broken - would otherwise hold this thread for good. Waiting on a queue can
    be given a time limit; waiting on a pipe cannot.
    """

    def __init__(self, argv: tuple[str, ...], root: Path):
        self.root = root
        self._next = 0
        self._lock = threading.Lock()
        self._said: queue.Queue[dict[str, Any] | None] = queue.Queue()
        try:
            self.process = subprocess.Popen(  # noqa: S603 - the path came from this machine
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(root),
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
        except OSError as exc:
            raise NavigateError(f"{argv[0]} would not start: {exc}") from exc
        self._reading = threading.Thread(target=self._pump, daemon=True)
        self._reading.start()

    def _pump(self) -> None:
        """Everything the server says, put on the queue as it arrives."""

        try:
            while True:
                one = self._one_message()
                if one is None:
                    break
                self._said.put(one)
        except (OSError, ValueError):
            # The pipe was closed under it, which is what stopping looks like
            # from in here.
            pass
        finally:
            self._said.put(None)  # nothing more is coming

    def _write(self, message: dict[str, Any]) -> None:
        if not self.process.stdin:
            raise NavigateError("The language server closed its input")
        body = json.dumps(message).encode("utf-8")
        try:
            self.process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.process.stdin.flush()
        except OSError as exc:
            raise NavigateError(f"The language server stopped listening: {exc}") from exc

    def _one_message(self) -> dict[str, Any] | None:
        """One whole message, or nothing once the server has stopped talking."""

        if not self.process.stdout:
            return None
        length = 0
        while True:
            line = self.process.stdout.readline()
            if not line:
                return None
            said = line.decode("utf-8", errors="replace").strip()
            if not said:
                break
            if said.lower().startswith("content-length:"):
                try:
                    length = int(said.split(":", 1)[1].strip())
                except ValueError:
                    return None
        if not 0 < length <= MOST_BYTES:
            return None
        body = self.process.stdout.read(length)
        try:
            held = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return held if isinstance(held, dict) else None

    def ask(self, method: str, params: dict[str, Any], seconds: float) -> Any:
        """Ask one thing and wait for the answer to that thing."""

        with self._lock:
            self._next += 1
            number = self._next
        self._write({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        until = time.monotonic() + seconds
        while True:
            try:
                said = self._said.get(timeout=max(0.0, until - time.monotonic()))
            except queue.Empty:
                raise NavigateError(
                    f"The language server did not answer {method} in "
                    f"{int(seconds)} seconds, so it was stopped."
                ) from None
            if said is None:
                raise NavigateError(
                    f"The language server stopped before it answered {method}"
                )
            if said.get("id") != number:
                # Something it said on its own - a diagnostic, a log line. Not
                # what was asked for, so it is passed over.
                continue
            if "error" in said:
                inside = said["error"]
                why = inside.get("message") if isinstance(inside, dict) else inside
                raise NavigateError(f"The language server refused {method}: {why}")
            return said.get("result")

    def tell(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def stop(self) -> None:
        try:
            self.tell("exit", {})
        except NavigateError:
            pass
        try:  # noqa: SIM105 - the two ways it can go are handled below
            self.process.terminate()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except OSError:
                pass
        # The two pipes to it, closed by hand. The panel asks these questions
        # all day; a handle left behind on each one adds up to a panel that
        # cannot open files any more.
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        # Closing the pipe is what lets the reading thread finish. It is a
        # daemon thread, so a stuck one would not hold the program open, but
        # waiting a moment for it keeps a run from piling them up.
        self._reading.join(timeout=2.0)


def _as_a_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _from_a_uri(uri: str, root: Path) -> str:
    """A place the server named, written the way the rest of the harness writes them.

    A server names files the way the web names them, which turns a space into
    %20 and a drive letter into something with a slash in front of it. Undoing
    that by hand goes wrong on the first project folder with a space in its
    name - and this project's own folder has one.
    """

    said = str(uri or "")
    if not said:
        return ""
    parts = urllib.parse.urlsplit(said)
    if parts.scheme == "file":
        where = Path(request.url2pathname(parts.path))
    else:
        where = Path(urllib.parse.unquote(said))
    try:
        return str(where.resolve().relative_to(root.resolve())).replace(os.sep, "/")
    except (ValueError, OSError):
        # Somewhere outside the project - a library, most often. Its own path is
        # the only useful thing to say about it.
        return str(where)


def _the_line(root: Path, relative: str, line: int) -> str:
    try:
        where = confined_path(root, relative, allow_control=True)
        with where.open(encoding="utf-8", errors="replace") as reading:
            for number, text in enumerate(reading, start=1):
                if number == line:
                    return text.strip()[:200]
    except (HarnessError, OSError):
        return ""
    return ""


def _places_from(result: Any, root: Path) -> list[Place]:
    """Whatever shape the server answered in, as a list of places."""

    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    places: list[Place] = []
    for one in result[:MOST_PLACES]:
        if not isinstance(one, dict):
            continue
        uri = one.get("uri") or one.get("targetUri") or ""
        span = one.get("range") or one.get("targetSelectionRange") or one.get("targetRange") or {}
        start = span.get("start", {}) if isinstance(span, dict) else {}
        line = int(start.get("line", 0)) + 1
        relative = _from_a_uri(str(uri), root)
        places.append(
            Place(
                path=relative,
                line=line,
                column=int(start.get("character", 0)) + 1,
                text=_the_line(root, relative, line),
            )
        )
    return places


def _ask_a_server(
    config: LoadedConfig, path: str, line: int, column: int, method: str
) -> tuple[list[Place], str]:
    """Start a server, ask it one thing about one place, and stop it again."""

    where = confined_path(config.project_root, path, allow_control=True)
    if not where.is_file():
        raise NavigateError(f"There is no file at {path}")
    chosen = _server_for(where)
    if chosen is None:
        return [], ""
    label, argv = chosen
    talking = _Talking(argv, config.project_root)
    try:
        talking.ask(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _as_a_uri(config.project_root),
                "capabilities": {},
            },
            LONGEST_START_SECONDS,
        )
        talking.tell("initialized", {})
        talking.tell(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _as_a_uri(where),
                    "languageId": where.suffix.lstrip("."),
                    "version": 1,
                    "text": where.read_text(encoding="utf-8", errors="replace"),
                }
            },
        )
        answered = talking.ask(
            method,
            {
                "textDocument": {"uri": _as_a_uri(where)},
                "position": {"line": max(0, line - 1), "character": max(0, column - 1)},
                **({"context": {"includeDeclaration": True}}
                   if method == "textDocument/references" else {}),
            },
            LONGEST_ASK_SECONDS,
        )
    finally:
        talking.stop()
    if method == "textDocument/hover":
        return _hover_as_places(answered, path, line), label
    return _places_from(answered, config.project_root), label


def _hover_as_places(answered: Any, path: str, line: int) -> list[Place]:
    if not isinstance(answered, dict):
        return []
    said = answered.get("contents")
    if isinstance(said, dict):
        said = said.get("value", "")
    elif isinstance(said, list):
        said = "\n".join(
            one.get("value", "") if isinstance(one, dict) else str(one) for one in said
        )
    said = str(said or "").strip()
    return [Place(path=path, line=line, what=said[:2000])] if said else []


# What a definition looks like in each language, for the days when no language
# server is installed. Kept per language on purpose: C's shape - a type, then a
# name, then a bracket - also matches an ordinary line of Python calling
# something, so using every language's shapes on every file turns a rough guess
# into a wrong one. Every language named in KNOWN_SERVERS is here, or "where is
# it" could never find anything written in it.
DEFINING: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    ((".py",), (r"^\s*(?:async\s+)?(?:def|class)\s+{name}\b",)),
    (
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        (
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
            r"(?:function|class|const|let|var)\s+{name}\b",
        ),
    ),
    (
        (".rs",),
        (
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:(?:async|unsafe|const|default)\s+|extern\s+(?:\"[^\"]*\"\s+)?){{0,4}}"
            r"(?:fn|struct|enum|type|trait|union|mod|static)\s+{name}\b",
        ),
    ),
    (
        (".go",),
        (
            # func Name(, and func (r Receiver) Name(
            r"^\s*func\s+(?:\([^)]*\)\s*)?{name}\b",
            r"^\s*type\s+{name}\b",
            r"^\s*(?:var|const)\s+{name}\b",
        ),
    ),
)


# The files C and C++ live in. These are read rather than matched: see below.
C_LIKE = (".c", ".h", ".cc", ".cpp", ".hpp", ".cxx")

# Words that can stand in front of a name and a bracket while the line is still
# only using it. "return compute_total(a, b)" is not where compute_total lives.
NOT_A_DEFINITION = frozenset({
    "return", "else", "if", "while", "for", "switch", "case", "do", "goto",
    "sizeof", "throw", "new", "delete", "co_return", "co_await", "assert",
    "and", "or", "not", "static_assert",
})


# Words that can stand alone on a line without that line being a type. A label
# and an access specifier both look like one word and are neither.
# The words C calls a type, for the times one stands alone on a line above a
# signature. Anything ending in _t, anything in capitals, and anything starting
# with a capital counts as well; those are checked in the code.
PLAIN_TYPES = frozenset({
    "int", "void", "char", "bool", "float", "double", "long", "short",
    "unsigned", "signed", "size_t", "auto", "wchar_t", "uint8_t", "uint16_t",
    "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t",
})

NOT_A_TYPE = frozenset({
    "public", "private", "protected", "case", "default", "else", "do", "try",
    "return", "break", "continue", "goto",
})


def _every_whole_word_at(line: str, name: str):
    """Every place `name` sits, with how many brackets are open in front of it.

    Every place, not the first: one line can use a thing and then define it
    further along, and giving up at the first use loses the definition.

    The brackets are counted as the line goes past, once. Counting them again
    from the start for each place made a line of chained calls take as long as
    the line squared: forty thousand letters took fourteen seconds.
    """

    deep = 0
    at = 0
    end = len(line)
    first = name[0]
    while at < end:
        letter = line[at]
        if letter == "(":
            deep += 1
        elif letter == ")":
            deep = max(0, deep - 1)
        elif letter == first and line.startswith(name, at):
            before = line[at - 1] if at else ""
            after = line[at + len(name):].lstrip()
            if not (before and (before.isalnum() or before == "_")) and after[:1] == "(":
                # A pointer to a function is written (*name), so the bracket
                # right in front of that one is part of the name, not something
                # the name is being handed to.
                wrapper = line[:at].rstrip().rstrip("*").rstrip()
                yield at, max(0, deep - 1 if wrapper.endswith("(") else deep)
        at += 1


def _the_matching_bracket(line: str, opened_at: int) -> int:
    """Where the bracket opened at `opened_at` closes, or -1 on this line."""

    deep = 0
    for at in range(opened_at, len(line)):
        if line[at] == "(":
            deep += 1
        elif line[at] == ")":
            deep -= 1
            if not deep:
                return at
    return -1


# What a word is made of, and what can sit between a type and the name without
# being part of either.
_PART_OF_A_WORD = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:<>"
)
_BETWEEN_THEM = frozenset(" \t*&(")


def _the_word_in_front(prefix: str) -> str:
    """The last real word before the name, with the noise taken off it.

    "int *" is a type and so is "std::vector<int>"; "x =" is not, and neither
    is "return". Stars, brackets and ampersands are stripped because they
    belong to the type, not to the question of whether there is one.

    Walked back from the end rather than cutting the line up. Splitting the
    whole prefix at every place the name sits made one line of chained calls
    take as long as the line squared: forty thousand letters took fourteen
    seconds.
    """

    at = len(prefix) - 1
    while at >= 0 and prefix[at] in _BETWEEN_THEM:
        at -= 1
    end = at + 1
    while at >= 0 and prefix[at] in _PART_OF_A_WORD:
        at -= 1
    return prefix[at + 1:end]


def _the_arguments_look_declared(inside: str) -> bool:
    """True when the brackets hold arguments rather than values.

    Only asked when there is no type in front of the name, which is either the
    older way of writing a definition or a bare call. "int a, int b" is one;
    "i, i" is the other.
    """

    said = inside.strip()
    if not said or said == "void":
        return True
    return re.search(r"[A-Za-z_]\w*[\s\*]+[A-Za-z_]", said) is not None


def _a_raw_string_starts_at(line: str, at: int) -> tuple[int, str] | None:
    """Does C++ text kept exactly as written start here, and what ends it?

    R"end(...)end" - the word between the quote and the bracket is chosen by
    whoever wrote it, and the same word closes it.
    """

    letter = line[at]
    if letter not in "Ru8LU":
        return None
    quote = line.find('"', at)
    if quote == -1 or quote - at > 3 or line[quote - 1] != "R":
        return None
    opened = line.find("(", quote)
    if opened == -1 or opened - quote > 17:
        return None
    return opened + 1, line[quote + 1:opened]


def _the_code_on_this_line(line: str, in_a_comment: bool) -> tuple[str, bool]:
    """The line with its comments gone and its text emptied, and where that leaves us.

    A signature written out as an example in a comment is not a place anybody
    wants to be sent, and neither is one printed inside a piece of text. Both
    read exactly like the real thing to anything looking at the line as it is
    written, so both are taken out before the line is read at all.

    The quotes are kept and only what is between them is dropped, so that
    `extern "C" int f(void) {` still has a type in front of the name.
    """

    kept: list[str] = []
    at = 0
    end = len(line)
    while at < end:
        if in_a_comment:
            shut = line.find("*/", at)
            if shut == -1:
                return "".join(kept), True
            in_a_comment = False
            at = shut + 2
            continue
        if line.startswith("//", at):
            break
        if line.startswith("/*", at):
            in_a_comment = True
            at += 2
            continue
        if line[at] == '"' or _a_raw_string_starts_at(line, at):
            raw = _a_raw_string_starts_at(line, at)
            if raw:
                # C++ text kept exactly as written: R"end(...)end". What is
                # inside can hold quotes and brackets, and reading it as
                # ordinary text swallowed the rest of the line.
                kept.append('""')
                shut = line.find(f"){raw[1]}\"", raw[0])
                at = len(line) if shut == -1 else shut + len(raw[1]) + 2
                continue
            # Only double quotes. A single quote in C++ is as likely to be a
            # separator inside a number as the start of anything.
            kept.append('""')
            at += 1
            while at < end:
                if line[at] == "\\":
                    at += 2
                    continue
                if line[at] == '"':
                    at += 1
                    break
                at += 1
            continue
        kept.append(line[at])
        at += 1
    return "".join(kept), in_a_comment


def _only_a_type(line: str) -> bool:
    """Is this whole line nothing but a type, with the name on the line below?

    A signature can be spread over three lines, and the middle one carries no
    type at all. Looking at the line above is the only way to tell that from a
    call written over two lines - inside a function, the line above ends in a
    semicolon, a brace, or a bracket, and none of those are a type.
    """

    said = (line or "").strip()
    if not said or len(said) > 80:
        return False
    # A label, a case, and an access specifier all end in a colon and none of
    # them is a type. "std::vector<int>" ends in a bracket, so it still counts.
    if said.endswith(":") and not said.endswith("::"):
        return False
    if said.split()[0] in NOT_A_TYPE:
        return False
    if re.fullmatch(r"[a-z_][a-z0-9_]*", said) and said not in PLAIN_TYPES:
        # One lowercase word on its own, and not one of the words C calls a
        # type. A leftover line inside a function looks exactly like this, and
        # taking it for a type turns the call under it into a definition.
        return False
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\s\*&:<>,]*", said) is not None


def _a_typedef_of(bare: str, name: str) -> bool:
    """Does this typedef make `name`, or only use it to make something else?

    `typedef A B;` is where B lives, not where A does. The name being made is
    the last one before the semicolon, or the one in brackets when what is
    being made is a pointer to a function.
    """

    safe = re.escape(name)
    if re.search(rf"\(\s*\*\s*{safe}\s*\)", bare):
        return True
    body = bare.rstrip().rstrip(";")
    # `typedef struct { int total; } Thing;` - the name is after the brace, and
    # everything inside the braces is somebody else's business.
    shut = body.rfind("}")
    if shut != -1:
        body = body[shut + 1:]
    words = re.findall(r"[A-Za-z_]\w*", body)
    return bool(words) and words[-1] == name


def _a_definition_at(
    line: str, name: str, at: int, before: str, deep: int = 0
) -> bool:
    """Is the name at this place in the line being defined, or only used?"""

    if deep:
        # Inside a bracket that has not closed: this name is being handed to
        # something, not declared.
        return False
    in_front = _the_word_in_front(line[:at])
    if in_front in NOT_A_DEFINITION:
        return False
    if in_front and not (in_front[0].isalpha() or in_front[0] == "_"):
        # An equals sign, a comma, an operator: this line is doing something
        # with it, not saying what it is.
        return False
    opened = line.index("(", at + len(name))
    closed = _the_matching_bracket(line, opened)
    if closed == -1:
        # The arguments carry on below. A signature does that, with its type
        # either in front of it or on the line above.
        return bool(in_front) or _only_a_type(before)
    if not in_front and not _only_a_type(before) and not _the_arguments_look_declared(
        line[opened + 1:closed]
    ):
        return False
    rest = line[closed + 1:].strip()
    if not rest:
        return True
    # What is left has to open a body before it ends the statement. A call and
    # a declaration in a header both end in a semicolon; neither is a place to
    # send somebody who asked where a thing is.
    brace = rest.find("{")
    semicolon = rest.find(";")
    if brace == -1:
        return False
    return semicolon == -1 or brace < semicolon


def _a_c_definition(
    line: str, name: str, in_a_comment: bool = False, before: str = ""
) -> bool:
    """Is this line where `name` is defined, rather than somewhere it is used?"""

    line, _after = _the_code_on_this_line(line, in_a_comment)
    bare = line.strip()
    safe = re.escape(name)
    if bare.startswith("#"):
        # Only one kind of line beginning with a hash defines anything.
        return bool(re.match(rf"#\s*define\s+{safe}\b", bare))
    if bare.startswith("typedef"):
        return _a_typedef_of(bare, name)
    if (before or "").strip().startswith("typedef"):
        # A typedef can be spread over two lines, with the name on the second.
        return _a_typedef_of(f"{before.strip()} {bare}", name)
    # The closing line of a struct written without a name of its own, which is
    # how most C names one.
    if re.match(rf"^\}}\s*{safe}\s*;", bare):
        return True
    named = re.match(
        # template <...> in front, "enum class" as two words, and the export
        # macros and attributes real headers put between the keyword and the
        # name. All of it noise, and all of it common.
        rf"(?:template\s*<[^>]*>\s*)?"
        rf"(?:struct|class|enum(?:\s+(?:class|struct))?|union|namespace)\s+"
        rf"(?:__attribute__\s*\(\([^)]*\)\)\s*|alignas\s*\([^)]*\)\s*"
        rf"|[A-Z_][A-Z0-9_]*\s+)*"
        rf"{safe}\b(.*)$",
        bare,
    )
    if named:
        # A body has to follow, or this is a variable of that type - or a line
        # saying the type exists somewhere else, which is not where it is.
        rest = named.group(1).strip()
        # What it was made from, for a template written out for one type.
        if rest.startswith("<"):
            shut = rest.find(">")
            if shut != -1:
                rest = rest[shut + 1:].strip()
        # And an attribute on the other side of the name.
        rest = re.sub(r"^(?:__attribute__\s*\(\([^)]*\)\)|alignas\s*\([^)]*\))\s*",
                      "", rest).strip()
        if not rest or rest.startswith("{"):
            return True
        if rest.startswith((":", "final")) and "{" in rest:
            return True
        return False
    # A thing kept in a variable rather than written as a function, which in
    # C++ is how a lambda is named.
    kept = re.search(rf"\b{safe}\s*=\s*\[", line)
    if kept:
        in_front = _the_word_in_front(line[:kept.start()])
        return bool(in_front) and in_front not in NOT_A_DEFINITION and (
            in_front[0].isalpha() or in_front[0] == "_"
        )
    return any(
        _a_definition_at(line, name, at, before, deep)
        for at, deep in _every_whole_word_at(line, name)
    )


def _what_a_definition_looks_like(suffix: str, name: str) -> list[re.Pattern[str]]:
    """The shapes a definition takes in this kind of file."""

    safe = re.escape(name)
    for suffixes, shapes in DEFINING:
        if suffix in suffixes:
            return [re.compile(shape.format(name=safe)) for shape in shapes]
    return []


def _by_reading_the_files(config: LoadedConfig, name: str, *, defining: bool) -> list[Place]:
    """The old way: read the files and match text. A guess, and labelled as one."""

    every_mention = [re.compile(rf"\b{re.escape(name)}\b")]
    ignore = set(config.get("project.ignore") or [])
    places: list[Place] = []
    for where in sorted(config.project_root.rglob("*")):
        if len(places) >= MOST_PLACES:
            break
        if not where.is_file() or where.suffix.lower() not in {
            one for _k, _l, _a, suffixes, _h in KNOWN_SERVERS for one in suffixes
        }:
            continue
        if any(part in ignore or part.startswith(".") for part in where.parts):
            continue
        suffix = where.suffix.lower()
        reads_the_line = defining and suffix in C_LIKE
        looking = (
            _what_a_definition_looks_like(suffix, name) if defining else every_mention
        )
        if defining and not reads_the_line and not looking:
            continue
        try:
            with where.open(encoding="utf-8", errors="replace") as reading:
                in_a_comment = False
                the_line_above = ""
                for number, text in enumerate(reading, start=1):
                    if reads_the_line:
                        # A signature written out as an example in a comment is
                        # not a place anybody wants to be sent, and a block
                        # comment can only be followed line by line.
                        found = _a_c_definition(text, name, in_a_comment, the_line_above)
                        the_line_above, in_a_comment = _the_code_on_this_line(
                            text, in_a_comment
                        )
                    else:
                        found = any(one.search(text) for one in looking)
                    if found:
                        places.append(Place(
                            path=str(where.relative_to(config.project_root)).replace(os.sep, "/"),
                            line=number,
                            text=text.strip()[:200],
                        ))
                        if len(places) >= MOST_PLACES:
                            break
        except OSError:
            continue
    return places


def _what_would_make_it_exact(places: list[Place]) -> str:
    """What this person should do next to stop guessing.

    Telling somebody to install a language server when they already have one is
    the wrong answer twice over: they have done it, and the thing they actually
    need to do - click a place, so there is a file and a line to point a server
    at - goes unsaid.
    """

    always = (
        "This is a guess: it matched the text of your files. Two things with the "
        "same name look the same to it. "
    )
    ready = sorted({
        Path(one.path).suffix.lower() for one in places
        if _server_for(Path(one.path)) is not None
    })
    every = sorted({Path(one.path).suffix.lower() for one in places})
    if ready and len(ready) == len(every):
        return always + (
            "Click one of these places below and ask again: with a file and a line, "
            "the tool for that language is asked directly and the answer is exact."
        )
    if ready:
        # Some of these are in a language with a tool here and some are not,
        # and clicking the wrong one gets the same guess again. Say which.
        return always + (
            f"You have a tool for {', '.join(ready)}. Click one of those places "
            "below and ask again for an exact answer. For the others, install "
            "the tool from the list below first."
        )
    if places:
        return always + (
            "Install the language server for that kind of file, from the list below, "
            "then click one of these places and ask again."
        )
    return always + "Install a language server for an exact answer."


def look_it_up(
    config: LoadedConfig,
    *,
    asking: str,
    path: str = "",
    line: int = 0,
    column: int = 0,
    name: str = "",
) -> Answer:
    """Where is it, what uses it, or what is it.

    Given a file and a line, a language server answers exactly. Given only a
    name, there is nothing to point a server at, so the files are read and the
    answer says plainly that it is a guess.
    """

    methods = {
        "where-is-it": "textDocument/definition",
        "what-uses-it": "textDocument/references",
        "what-is-it": "textDocument/hover",
    }
    if asking not in methods:
        raise NavigateError(
            "Ask one of: where is it, what uses it, what is it."
        )
    name = str(name or "").strip()
    if path:
        places, label = _ask_a_server(config, path, int(line or 1), int(column or 1), methods[asking])
        if label:
            return Answer(
                asked=name or f"{path}:{line}",
                places=places,
                exact=True,
                how=f"{label} answered",
                note="" if places else "That tool knows the file and found nothing here.",
            )
    if not name:
        return Answer(
            asked=path or "",
            exact=False,
            how="nothing to ask",
            note=(
                "No language server for that kind of file is installed, and no name was "
                "given to look for. Install one from the list, or type a name."
            ),
        )
    if not A_NAME.fullmatch(name):
        raise NavigateError(
            "A name is letters, numbers and underscores, up to 120 of them."
        )
    if asking == "what-is-it":
        return Answer(
            asked=name,
            exact=False,
            how="reading the files",
            note=(
                "Saying what something is needs a language server, and one has to be "
                "pointed at a file and a line. Press Where is it? first, then click one "
                "of the places it finds, and ask this again."
            ),
        )
    places = _by_reading_the_files(config, name, defining=asking == "where-is-it")
    return Answer(
        asked=name,
        places=places,
        exact=False,
        how="reading the files",
        note=_what_would_make_it_exact(places),
    )
