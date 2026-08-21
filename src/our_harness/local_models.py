"""Models running on this machine, found rather than typed in.

A model on your own machine is the one assistant nobody has to approve. No seat,
no key, no administrator, and nothing said to it leaves the building. On a
locked-down company machine it is often the only one that will ever work.

The harness could always use one - the settings have taken an Ollama address for
as long as there have been settings. What it could not do was find one. Somebody
with Ollama running and a model pulled still had to know the port number, know
what the model was called, and write both into a file by hand, which is a strange
thing to ask of the one route that needs no permission at all.

So this goes and looks. Two servers, because they are the two nearly everybody
has, and then anything else somebody points it at:

  - Ollama, on port 11434, which lists what it has
  - LM Studio, on port 1234, which answers the OpenAI shape

Nothing here starts anything or installs anything. It asks, briefly, and says
what answered.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

# Short, because this runs while somebody is looking at a page. A server on this
# machine answers in a few milliseconds or it is not there, and two seconds each
# for two that are not running is four seconds of somebody waiting to be told
# nothing - long enough that opening the tab feels broken.
HOW_LONG_TO_WAIT = 0.6
# Enough names to choose from without turning the page into a list of models.
MOST_MODELS_SHOWN = 40


@dataclass
class WhereModelsRun:
    """One place on this machine that will answer with a model."""

    id: str
    label: str
    kind: str
    endpoint: str
    # The address to ask for the list of models, and where the names sit in it.
    asks_at: str
    names_under: str
    how_to_get_it: str
    models: list[str] = field(default_factory=list)
    running: bool = False
    why_not: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "endpoint": self.endpoint,
            "models": self.models,
            "running": self.running,
            "why_not": self.why_not,
            "how_to_get_it": self.how_to_get_it,
        }


# The two nearly everybody has. Both are asked for at the loopback address and
# nowhere else: a model server reachable from the network is somebody else's
# machine, and this is about what is on yours.
THE_USUAL_ONES = (
    WhereModelsRun(
        id="ollama",
        label="Ollama",
        kind="ollama",
        endpoint="http://127.0.0.1:11434",
        asks_at="http://127.0.0.1:11434/api/tags",
        names_under="models",
        how_to_get_it=(
            "Ollama runs models on your own machine and needs no key and nobody's "
            "permission. Get it from ollama.com, then fetch a model: "
            "ollama pull qwen2.5-coder:7b"
        ),
    ),
    WhereModelsRun(
        id="lm-studio",
        label="LM Studio",
        kind="openai-compatible",
        endpoint="http://127.0.0.1:1234/v1",
        asks_at="http://127.0.0.1:1234/v1/models",
        names_under="data",
        how_to_get_it=(
            "LM Studio runs models on your own machine with a window to pick them "
            "in. Get it from lmstudio.ai, load a model, and turn on its local "
            "server - it is the button that says Start Server."
        ),
    ),
)


def _ask_briefly(where: str) -> Any:
    """Ask one address and read what comes back, or nothing.

    Nothing is a perfectly good answer here. A server that is not running is the
    ordinary case, not a fault, and it must not put a red line on a page or take
    more than a moment to find out.
    """

    try:
        with urllib.request.urlopen(where, timeout=HOW_LONG_TO_WAIT) as answered:  # noqa: S310
            return json.loads(answered.read(2_000_000))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _names_in(said: Any, under: str) -> list[str]:
    """The model names out of whatever shape the server answered with.

    Ollama says {"models": [{"name": ...}]} and LM Studio says
    {"data": [{"id": ...}]}, so both keys are looked for in each entry rather
    than one being assumed.
    """

    if not isinstance(said, dict):
        return []
    held = said.get(under)
    if not isinstance(held, list):
        return []
    found = []
    for one in held:
        if isinstance(one, dict):
            name = one.get("name") or one.get("id") or one.get("model")
            if isinstance(name, str) and name.strip():
                found.append(name.strip())
        elif isinstance(one, str) and one.strip():
            found.append(one.strip())
    return sorted(set(found))[:MOST_MODELS_SHOWN]


def look(also: tuple[str, ...] = ()) -> list[WhereModelsRun]:
    """Every place on this machine that will answer with a model.

    The ones that are not running are listed too, with what to do about it.
    "Nothing was found" is a worse answer than "here is what you could have and
    where to get it", especially for the one route that needs nobody's
    permission.
    """

    # All of them at once. One at a time, the wait is the sum of every place
    # that is not running, and somebody opening the tab pays for each of them in
    # turn to tell them nothing.
    asking = [WhereModelsRun(**{**one.__dict__, "models": [], "running": False, "why_not": ""})
              for one in THE_USUAL_ONES]
    with ThreadPoolExecutor(max_workers=max(1, len(asking) + len(also))) as crowd:
        answers = list(crowd.map(_ask_briefly, [one.asks_at for one in asking]))
        elsewhere = list(crowd.map(_one_somebody_pointed_at, also))
    for held, said in zip(asking, answers):
        if said is None:
            held.why_not = f"Nothing answered at {held.endpoint}, so it is not running."
            continue
        held.running = True
        held.models = _names_in(said, held.names_under)
        if not held.models:
            held.why_not = (
                f"{held.label} is running and has no models in it yet. "
                f"{held.how_to_get_it}"
            )
    return [*asking, *elsewhere]


def _one_somebody_pointed_at(where: str) -> WhereModelsRun:
    """A server at an address somebody typed in themselves.

    Plenty of things answer the OpenAI shape - llama.cpp, vLLM, a box under
    somebody's desk - and there is no finding those by guessing. Asked for by
    address, they work like the rest.
    """

    tidy = _only_this_machine(where.rstrip("/"))
    held = WhereModelsRun(
        id=f"typed-in:{tidy}",
        label=tidy,
        kind="openai-compatible",
        endpoint=tidy,
        asks_at=f"{tidy}/models",
        names_under="data",
        how_to_get_it=(
            "This is an address you gave. Anything that answers the OpenAI shape "
            "works here - llama.cpp, vLLM, or a machine of your own."
        ),
    )
    said = _ask_briefly(held.asks_at)
    if said is None:
        held.why_not = f"Nothing answered at {tidy}."
        return held
    held.running = True
    held.models = _names_in(said, "data")
    if not held.models:
        held.why_not = f"{tidy} answered and listed no models."
    return held


def _only_this_machine(where: str) -> str:
    """An address on this machine, or nothing worth asking.

    Nothing hands an address to this today, and unreachable is not the same as
    safe: the moment somebody wires a box up to it, whatever is typed there is
    fetched by the panel, from wherever the panel can reach. A file on the disk,
    a machine inside the company network, a name that resolves to somewhere
    else entirely. This is about models running on your own machine, so that is
    all it will ask.
    """

    held = urllib.parse.urlsplit(where)
    if held.scheme not in ("http", "https"):
        raise ValueError(
            f"{where} is not a web address this will ask. Local model servers "
            "answer at http or https."
        )
    name = (held.hostname or "").lower()
    if name not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        raise ValueError(
            f"{where} is not on this machine. This looks for models running "
            "here, and a server somewhere else is somebody else's machine."
        )
    return where


def a_route_for(one: WhereModelsRun, model: str) -> dict[str, Any]:
    """The settings that would be written for one local model.

    No key, because there is nothing to pay and nobody to prove yourself to.
    That is the whole point of running it here.
    """

    if not one.running:
        raise ValueError(f"{one.label} is not running")
    if model not in one.models:
        raise ValueError(f"{one.label} has no model called {model}")
    return {
        "kind": one.kind,
        "model": model,
        "endpoint": one.endpoint,
        "api_key_env": "",
    }
