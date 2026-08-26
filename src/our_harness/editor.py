"""Working inside an editor you already have open.

The panel is a good place to watch a run. It is a bad place to be when you are
in the middle of writing code: you are in your editor, and the answer you want -
where is this used, what do we already know about this, run my checks - is one
window away.

Editors have agreed on one way to talk to a tool like this. We already speak the
asking half of it: `mcp.py` is how the harness calls somebody else's tools. This
is the same conversation the other way round, so an editor can call ours.

What it offers
--------------

Reading only, always:

  - Where is it, what uses it, what is it - the same three questions the Look it
    up tab asks, answered by a real language server where there is one.
  - What this project already knows, out of the harness's own memory.
  - Which automations are saved here.

And, only if you say so when you start it:

  - Run one of those automations.
  - Run the checks.

Both of those can run commands on your machine and do whatever the person who
wrote them down put in them - which can include writing files into the project,
deleting them, and calling an assistant over the network. Most checks only read,
but a check can be a plain command, and a suite is only as safe as the least
careful check in it. An editor is a place where a tool gets
called without anybody deciding to call it, so both are off unless you turn them
on, and turning them on is a sentence you write yourself.

How it runs
-----------

The editor starts `harness editor serve` and talks to it through the pipe
between them. Nothing listens on a port, nothing is reachable from anywhere
else, and it stops when the editor stops. The harness never edits your editor's
settings: `harness editor setup` prints exactly what to paste, and where.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, TextIO

from . import PRODUCT_NAME
from .config import LoadedConfig
from .models import HarnessError

# The version of the conversation we answer. An editor says which one it wants;
# if it is one we know, we agree to that one, and if it is not, we say what we
# speak and let it decide. Refusing outright would leave somebody staring at a
# tool that will not start with nothing to go on.
WE_SPEAK = "2025-06-18"
ONES_WE_KNOW = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

# The most that comes back from one call. An editor puts this in front of a
# model, and a whole file of it is a whole file of somebody's budget.
MOST_TO_SEND = 60_000
# One line of a message, and one message. A wildly long line is a runaway, not
# an answer.
LONGEST_LINE = 4_000_000

WHAT_WE_ARE_CALLED = "our-harness"


# The numbers everybody who speaks this uses. The middle one is not a spare:
# our own client watches for it to decide that a server is an older one and try
# the older way of saying hello. Sent the wrong number, our own client gave up
# on our own server rather than trying again.
NOTHING_LIKE_A_MESSAGE = -32600
NOT_ONE_WE_DO = -32601
SOMETHING_WRONG_WITH_THE_ASK = -32602
SOMETHING_WENT_WRONG_HERE = -32603
NOT_EVEN_JSON = -32700


class EditorError(HarnessError):
    """Something the editor asked for that we will not or cannot do."""


class NotOneWeDo(EditorError):
    """A message this does not answer at all, as opposed to one asked badly."""


def _text(value: object, name: str, most: int = 500) -> str:
    # Not given at all is not the same as given wrongly. Treated the same, an
    # editor asking "what uses put_this_file_in_place" - with no file and no
    # line, which is the whole point of asking by name - was told off for a
    # path it never sent.
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EditorError(f"{name} has to be text")
    said = value.strip()
    if len(said) > most:
        raise EditorError(f"{name} is longer than {most} letters")
    return said


def _whole_number(value: object, name: str, most: int = 1_000_000) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise EditorError(f"{name} has to be a whole number")
    if not 0 <= value <= most:
        raise EditorError(f"{name} has to be between 0 and {most}")
    return value


def _shorten(said: str) -> str:
    if len(said) <= MOST_TO_SEND:
        return said
    return (
        said[:MOST_TO_SEND]
        + f"\n\n[Cut short here. That is the first {MOST_TO_SEND} letters of a "
        "longer answer. Ask something narrower to see the rest.]"
    )


def _as_words(value: Any) -> str:
    """Anything, as something a person and a model can both read.

    An editor shows this to somebody. JSON with one key on each of forty lines
    is not something anybody reads, so what comes back is written out plainly
    and only falls back to JSON for shapes that have no plain form.
    """

    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------
# What we offer, and what each one does.
# --------------------------------------------------------------------------


def _look_it_up(config: LoadedConfig, given: dict[str, Any]) -> str:
    from . import navigate

    asking = _text(given.get("asking"), "asking") or "where-is-it"
    answer = navigate.look_it_up(
        config,
        asking=asking,
        path=_text(given.get("path"), "path", 1_000),
        line=_whole_number(given.get("line"), "line"),
        column=_whole_number(given.get("column"), "column"),
        name=_text(given.get("name"), "name"),
    )
    said = [
        f"{'Exactly' if answer.exact else 'A guess'}: {answer.how or 'read the files'}."
    ]
    if answer.note:
        said.append(answer.note)
    if not answer.places:
        said.append(f"Nothing found for {answer.asked}.")
    for place in answer.places:
        held = place.to_dict()
        line = held.get("line") or 0
        said.append(f"{held.get('path', '')}:{line}  {held.get('text', '').strip()}")
    return "\n".join(said)


def _what_the_project_knows(config: LoadedConfig, given: dict[str, Any]) -> str:
    from .memory import MemoryStore

    about = _text(given.get("about"), "about", 1_000)
    if not about:
        raise EditorError("Say what to look for.")
    how_many = _whole_number(given.get("how_many"), "how_many", 50) or 8
    with MemoryStore(config) as memory:
        found = list(memory.search_documents(about, limit=how_many))
        found += list(memory.search_episodes(about, limit=how_many))
    if not found:
        return f"Nothing about {about} yet. The harness learns as it runs."
    said = []
    for hit in found[:how_many]:
        said.append(f"--- {hit.source}: {hit.key}")
        said.append(hit.text.strip()[:2_000])
    return "\n".join(said)


def _list_the_automations(config: LoadedConfig, _given: dict[str, Any]) -> str:
    from . import pipelines

    saved = pipelines.saved_ones(config)
    if not saved:
        return "Nothing saved here yet. Draw one in the panel: harness ui."
    return "\n".join(saved)


def _run_an_automation(config: LoadedConfig, given: dict[str, Any]) -> str:
    from . import pipelines
    from .pipeline_runs import PipelineRunStore

    name = _text(given.get("name"), "name")
    if not name:
        raise EditorError("Say which automation to run.")
    definition = pipelines.load(config, name)
    frozen = pipelines.freeze_definition(config, definition)
    store = PipelineRunStore(config)
    accepted, _created = store.accept(frozen, source="editor")
    run_id = accepted["run_id"]
    attempt_id = accepted["attempt_id"]
    store.start(run_id, attempt_id)
    try:
        run = pipelines.run_it(
            config, definition,
            tell=lambda event: store.append_event(run_id, attempt_id, event),
            stopping=lambda: store.should_stop(run_id),
            run_id=run_id, frozen=frozen,
            decision_nonce=attempt_id,
        )
        store.finish(run_id, attempt_id, run.to_dict())
    except BaseException as exc:
        try:
            store.fail(run_id, attempt_id, str(exc))
        except HarnessError:
            pass
        raise
    return f"{'Passed' if run.passed else 'Did not pass'}: {run.said}"


def _run_the_checks(config: LoadedConfig, given: dict[str, Any]) -> str:
    from . import qa

    from .redaction import CredentialRedactor

    only = _text(given.get("only"), "only")
    suite = qa.load_suite(config)
    result = qa.QaRunner(config).run(
        suite,
        ids=(only,) if only else (),
        workers=1,
        run_id=f"editor-{uuid.uuid4().hex}",
        # Editor-triggered checks are still real executions. Keep their
        # immutable run-scoped evidence so a later report cannot silently
        # refer to overwritten or missing visual/command evidence.
        write_artifacts=True,
        immutable_artifacts=True,
    )
    # The same report the terminal prints, cleaned the same way, because it is
    # about to be handed to somebody else's program.
    return qa.render_report(result, "markdown", CredentialRedactor(config))


# Every one of these reads and nothing else. They are offered whatever else is
# turned on.
READING_ONLY: list[dict[str, Any]] = [
    {
        "name": "look_it_up",
        "description": (
            "Where is this used, where is it written, what is it. Answered by a "
            "real language server when this machine has one for that kind of "
            "file, and by reading the files when it does not - and it always "
            "says which, because that is what decides whether you trust it."
        ),
        # Said in the protocol's own words as well as in the description,
        # because our own client refuses to call a tool that does not say it
        # only reads - and that includes our own tools.
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "asking": {
                    "type": "string",
                    "enum": ["where-is-it", "what-uses-it", "what-is-it"],
                },
                "name": {"type": "string"},
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 0},
                "column": {"type": "integer", "minimum": 0},
            },
            "required": ["asking"],
        },
        "run": _look_it_up,
    },
    {
        "name": "what_the_project_knows",
        "description": (
            "Search what this project has already learnt - the notes and the "
            "runs the harness has kept. Use it before working something out "
            "from scratch that somebody here has worked out before."
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "about": {"type": "string"},
                "how_many": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["about"],
        },
        "run": _what_the_project_knows,
    },
    {
        "name": "list_the_automations",
        "description": "The automations saved in this project, by name.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {}},
        "run": _list_the_automations,
    },
]

# These run commands on this machine. Off unless somebody said so out loud.
RUNS_THINGS: list[dict[str, Any]] = [
    {
        "name": "run_an_automation",
        "description": (
            "Run one of this project's saved automations and say what happened. "
            "This runs real commands on this machine, and an automation may "
            "also write files into the project and call an assistant over the "
            "network - whatever the person who drew it put in it."
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "run": _run_an_automation,
    },
    {
        "name": "run_the_checks",
        "description": (
            "Run this project's checks and hand back the report. Give one check "
            "by name to run only that one. Most checks only read - they open a "
            "page, or look through the files. But a check can also be a plain "
            "command somebody in this project wrote down, and that runs on "
            "this machine and does whatever it does, including deleting "
            "things. It can also take a while."
        ),
        # Not a soft one. This is said about the tool, and a suite may hold one
        # check of the plain command kind among fifty that only read - so the
        # tool has to answer for the worst one in it. The whole point of saying
        # this in the protocol's own words is that an editor may ask before a
        # destructive thing and not before a safe one.
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"only": {"type": "string"}},
        },
        "run": _run_the_checks,
    },
]


def what_we_offer(*, may_run_things: bool = False) -> list[dict[str, Any]]:
    """The tools an editor is told about, in the order they are useful."""

    offered = [dict(one) for one in READING_ONLY]
    if may_run_things:
        offered.extend(dict(one) for one in RUNS_THINGS)
    return offered


# --------------------------------------------------------------------------
# The conversation itself.
# --------------------------------------------------------------------------


class Conversation:
    """One editor talking to one harness, over the pipe between them.

    Kept apart from the reading and writing so it can be tested without pipes:
    hand it a message, get an answer back, or None where the rules say to stay
    quiet.
    """

    def __init__(self, config: LoadedConfig, *, may_run_things: bool = False) -> None:
        self.config = config
        self.may_run_things = may_run_things
        self.offered = {one["name"]: one for one in what_we_offer(may_run_things=may_run_things)}
        self.agreed_version = ""

    def answer(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _went_wrong(None, NOTHING_LIKE_A_MESSAGE, "That is not a message this speaks.")
        method = message.get("method")
        given = message.get("params")
        given = given if isinstance(given, dict) else {}
        number = message.get("id")
        is_a_question = "id" in message and number is not None
        if not isinstance(method, str):
            return _went_wrong(number, NOTHING_LIKE_A_MESSAGE, "A message has to say what it wants.")
        # A notification wants no answer, whatever it says. Answering one is how
        # an editor ends up waiting for a reply to something it never asked.
        if not is_a_question:
            return None
        try:
            return _here_you_are(number, self._do_it(method, given))
        except NotOneWeDo as exc:
            return _went_wrong(number, NOT_ONE_WE_DO, str(exc))
        except EditorError as exc:
            return _went_wrong(number, SOMETHING_WRONG_WITH_THE_ASK, str(exc))
        except HarnessError as exc:
            return _went_wrong(number, SOMETHING_WENT_WRONG_HERE, str(exc))
        except Exception as exc:  # noqa: BLE001 - an editor must never be left hanging
            # The kind of thing and nothing else. What went wrong in detail is
            # this project's business, and this goes to another program.
            return _went_wrong(
                number, SOMETHING_WENT_WRONG_HERE,
                f"Something went wrong that the harness did not expect ({type(exc).__name__}).",
            )

    def _do_it(self, method: str, given: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            wanted = given.get("protocolVersion")
            self.agreed_version = wanted if wanted in ONES_WE_KNOW else WE_SPEAK
            return {
                "protocolVersion": self.agreed_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": WHAT_WE_ARE_CALLED, "version": "0.1.0"},
                # The name is written down once, in our_harness.PRODUCT_NAME.
                # Written out again here, a rename would leave this saying the
                # old one to every editor that asks.
                "instructions": (
                    f"This is {PRODUCT_NAME}, on the project you have open. Ask "
                    "it where something is used and what this project already "
                    "knows before working either out from scratch."
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": one["name"],
                        "description": one["description"],
                        "inputSchema": one["inputSchema"],
                        "annotations": one["annotations"],
                    }
                    for one in self.offered.values()
                ]
            }
        if method == "tools/call":
            return self._call_one(given)
        raise NotOneWeDo(f"This does not do {method}.")

    def _call_one(self, given: dict[str, Any]) -> dict[str, Any]:
        name = given.get("name")
        arguments = given.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        if not isinstance(name, str) or name not in self.offered:
            # Said as a failed call rather than a broken message, so the editor
            # shows the model the reason instead of a red light.
            return _the_tool_said(
                f"There is no tool called {name}. "
                + (
                    ""
                    if self.may_run_things
                    else "Anything that runs commands is turned off: whoever "
                    "started this did not say it could. "
                )
                + f"What there is: {', '.join(self.offered)}.",
                went_wrong=True,
            )
        try:
            said = self.offered[name]["run"](self.config, arguments)
        except EditorError as exc:
            return _the_tool_said(str(exc), went_wrong=True)
        except HarnessError as exc:
            return _the_tool_said(str(exc), went_wrong=True)
        return _the_tool_said(_shorten(_as_words(said)))


def _here_you_are(number: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": number, "result": result}


def _went_wrong(number: Any, code: int, said: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": number, "error": {"code": code, "message": said}}


def _the_tool_said(said: str, *, went_wrong: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": said}], "isError": went_wrong}


def talk(
    config: LoadedConfig,
    *,
    may_run_things: bool = False,
    reading: TextIO | None = None,
    writing: TextIO | None = None,
    stop_after: int = 0,
) -> int:
    """Read messages from the editor and answer them until it goes away.

    One message on each line, which is what this transport is. Nothing is
    printed that is not an answer: anything else on this pipe is read by the
    editor as a message, and one stray print stops the whole conversation.
    """

    reading = reading if reading is not None else sys.stdin
    writing = writing if writing is not None else sys.stdout
    conversation = Conversation(config, may_run_things=may_run_things)
    answered = 0
    while True:
        line = reading.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        if len(line) > LONGEST_LINE:
            _say_it(writing, _went_wrong(None, NOTHING_LIKE_A_MESSAGE, "That message is far too long."))
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _say_it(writing, _went_wrong(None, NOT_EVEN_JSON, "That message is not JSON."))
            continue
        for one in message if isinstance(message, list) else [message]:
            said = conversation.answer(one)
            if said is not None:
                _say_it(writing, said)
        answered += 1
        if stop_after and answered >= stop_after:
            return 0


def _say_it(writing: TextIO, message: dict[str, Any]) -> None:
    writing.write(json.dumps(message, ensure_ascii=False) + "\n")
    writing.flush()


# --------------------------------------------------------------------------
# What to paste into the editor.
# --------------------------------------------------------------------------


def how_to_tell_your_editor(
    config: LoadedConfig, *, may_run_things: bool = False
) -> dict[str, Any]:
    """The exact thing to paste, and where to paste it.

    Written out rather than written in. An editor's settings are the editor's,
    and a tool that edits them behind your back is a tool you cannot trust with
    anything else.
    """

    where = str(config.project_root.resolve())
    command = _how_this_harness_is_started()
    arguments = [*command[1:], "--project", where, "editor", "serve"]
    if may_run_things:
        arguments.append("--let-it-run-things")
    settings = {
        "mcpServers": {
            WHAT_WE_ARE_CALLED: {
                "command": command[0],
                "args": arguments,
            }
        }
    }
    return {
        "settings": json.dumps(settings, indent=2),
        # The command and its parts, kept apart. Joined into one line, it would
        # need quoting, and the quoting a terminal wants is not the quoting the
        # other terminal wants - and nothing here knows which one you use.
        "command": command[0],
        "arguments": arguments,
        "project": where,
        "may_run_things": may_run_things,
        "where_it_goes": [
            "VS Code: .vscode/mcp.json in this project, or the mcp section of your settings.",
            "Cursor: .cursor/mcp.json in this project.",
            "Claude Desktop: the claude_desktop_config.json its settings point at.",
            "Anything else that speaks this: give it the command and the arguments above.",
        ],
    }


def _how_this_harness_is_started() -> list[str]:
    """The command that starts this very harness, however it got here.

    Asked in one place, because the timer and the desktop app need the same
    answer and all three used to get it wrong the same way.
    """

    from .starting import how_to_start_the_harness

    return how_to_start_the_harness()
