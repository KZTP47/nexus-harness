"""The workflow, said in words a person who has never seen it can follow.

The Workflow view already draws the graph, and it draws it for somebody who
already knows what a node is. Somebody opening the harness for the first time
has a simpler question: what actually happens when I ask for a change?

This answers that question from the real graph, never from a picture drawn by
hand. A hand-drawn explanation is right on the day it is written and quietly
wrong from the first time somebody rewires anything. So every line here comes
from the nodes and edges the harness will really run.

The order is the order the work happens in. Where a stage can send the work
back to an earlier one, that is said too, because going back is the whole point
of the loop: the harness checks its own work and has another go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# What each kind of node is for, in plain words. The key is the node type as
# the graph writes it.
WHAT_EACH_KIND_IS_FOR = {
    "start": ("You ask for a change", "You say what you want, in your own words."),
    "planner": ("It works out a plan", "It reads your project and decides what to change."),
    "coder": ("It changes the files", "It writes the code, and can undo everything if the run fails."),
    "tool": ("It checks the work", "A check that has to pass before the work moves on."),
    "evaluator": ("Another model reviews it", "A second opinion on the change, before you see it."),
    "merge": ("It puts the answers together", "Several answers, weighed up into one."),
    "gauntlet": ("It runs the whole set of checks", "Every check, one after another."),
    "approval_required": ("It waits for you", "Nothing goes further until you say yes."),
    "end": ("Done", "The change is finished, and the run log says what happened."),
}

# The checks in the shipped workflow, in plain words. A check nobody has named
# here still shows up, under its own label, so a new one is never hidden.
WHAT_EACH_CHECK_IS_FOR = {
    "syntax": ("Does it still build?", "The change is read for mistakes that stop it running at all."),
    "security": ("Is it safe?", "The change is read for the mistakes that let somebody in."),
    "performance": ("Is it fast enough?", "The change is read for work that would make things slow."),
    "unit_test": ("Do your tests pass?", "Your own checks are run against the change."),
}


@dataclass
class Stage:
    """One step of the story, and what it can do next."""

    id: str
    title: str
    detail: str
    kind: str
    goes_back_to: list[str] = field(default_factory=list)
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "detail": self.detail,
            "kind": self.kind,
            "goes_back_to": self.goes_back_to,
            "label": self.label,
        }


def _plain_words(node: dict[str, Any]) -> tuple[str, str]:
    """The title and the one line under it, for one node."""

    kind = str(node.get("type") or "")
    if kind == "tool":
        role = str((node.get("config") or {}).get("role") or node.get("id") or "")
        named = WHAT_EACH_CHECK_IS_FOR.get(role)
        if named:
            return named
        label = str(node.get("label") or role or "A check")
        return label, "A check that has to pass before the work moves on."
    title, detail = WHAT_EACH_KIND_IS_FOR.get(
        kind, (str(node.get("label") or node.get("id") or "A step"), "Part of the workflow.")
    )
    return title, detail


def _the_arrows_that_go_back(
    onward: dict[str, list[str]], starts: list[str], place: dict[str, int] | None = None
) -> set[tuple[str, str]]:
    """The arrows that point at a stage the work has already been through.

    Walking forward and marking each stage while it is still being walked. An
    arrow onto a stage that is still open is the only kind that really goes
    back; every other arrow moves the work along, however far apart the two
    stages end up in the written order.

    Where two stages point at each other, either arrow could be called the one
    that goes back, and only one of the two answers reads right. So the walk
    follows the arrows in the order the stages were written down, which is the
    order somebody laying out a workflow works in. The retry then comes out as
    the arrow pointing at the earlier stage, which is what a person would say
    looking at the same picture.
    """

    place = place or {node_id: spot for spot, node_id in enumerate(onward)}
    open_now: set[str] = set()
    done: set[str] = set()
    going_back: set[tuple[str, str]] = set()

    def walk(node_id: str) -> None:
        open_now.add(node_id)
        for onto in sorted(onward.get(node_id, []), key=lambda one: place.get(one, 0)):
            if onto in open_now:
                going_back.add((node_id, onto))
            elif onto not in done:
                walk(onto)
        open_now.discard(node_id)
        done.add(node_id)

    for node_id in sorted([*starts, *onward], key=lambda one: (one not in starts, place.get(one, 0))):
        if node_id not in done:
            walk(node_id)
    return going_back


def _in_running_order(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
    """The node ids in an order where nothing comes before what it waits on.

    Following the arrows forward is not enough on its own. Where two paths meet
    again - the work splits, and both halves lead into one later stage - simply
    walking outwards can reach that later stage down the short path first and
    put it ahead of a stage it actually waits for. The story then reads
    backwards, and an ordinary arrow looks like the work being sent back to try
    again.

    So: find the arrows that really do go back, leave those out, and take what
    is left in the order that respects what waits on what.
    """

    by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}
    onward: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    coming_in: dict[str, int] = {node_id: 0 for node_id in by_id}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in by_id and target in by_id:
            onward[source].append(target)
            coming_in[target] += 1

    starts = [node_id for node_id, node in by_id.items() if node.get("type") == "start"]
    if not starts:
        starts = [node_id for node_id, count in coming_in.items() if count == 0]
    if not starts:
        starts = list(by_id)[:1]

    place = {node_id: spot for spot, node_id in enumerate(by_id)}
    back = _the_arrows_that_go_back(onward, starts, place)
    waiting_on: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for source, targets in onward.items():
        for target in targets:
            if (source, target) not in back:
                waiting_on[target].add(source)

    # A stage the work reaches sooner is written sooner; between two that are
    # equally ready, the one written down first.
    order: list[str] = []
    left = {node_id: set(needs) for node_id, needs in waiting_on.items()}
    while left:
        ready = sorted(
            (node_id for node_id, needs in left.items() if not needs),
            key=lambda node_id: (node_id not in starts, place[node_id]),
        )
        if not ready:
            # Every arrow that goes back was left out above, so this cannot
            # happen. If it ever did, the rest still belongs in the story.
            ready = sorted(left, key=lambda node_id: place[node_id])[:1]
        for node_id in ready:
            order.append(node_id)
            del left[node_id]
        for needs in left.values():
            needs.difference_update(ready)
    return order


def story(graph: dict[str, Any]) -> list[Stage]:
    """The whole workflow as an ordered list of plain steps."""

    if not isinstance(graph, dict):
        return []
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    edges = [edge for edge in (graph.get("edges") or []) if isinstance(edge, dict)]
    order = _in_running_order(nodes, edges)
    by_id = {str(node.get("id")): node for node in nodes}
    onward: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in by_id and target in by_id:
            onward[source].append(target)
    starts = [node_id for node_id, node in by_id.items() if node.get("type") == "start"]
    going_back = _the_arrows_that_go_back(
        onward,
        starts or list(by_id)[:1],
        {node_id: spot for spot, node_id in enumerate(by_id)},
    )

    stages: list[Stage] = []
    for node_id in order:
        node = by_id[node_id]
        title, detail = _plain_words(node)
        # An arrow pointing at a stage the work has already been through is the
        # loop: the harness found something wrong and is having another go.
        back = sorted({onto for source, onto in going_back if source == node_id})
        stages.append(
            Stage(
                id=node_id,
                title=title,
                detail=detail,
                kind=str(node.get("type") or ""),
                goes_back_to=back,
                label=str(node.get("label") or ""),
            )
        )
    return stages


def in_plain_words(graph: dict[str, Any]) -> dict[str, Any]:
    """Everything the first screen needs to draw the picture."""

    stages = story(graph)
    titles = {stage.id: stage.title for stage in stages}
    # Written with the titles left as they are. Lower-casing them reads as
    # nonsense the moment a title is a question.
    loops = [
        f"{stage.title} — if that goes wrong, the work goes back to: "
        f"{titles.get(target, target)}"
        for stage in stages
        for target in stage.goes_back_to
    ]
    return {
        "stages": [stage.to_dict() for stage in stages],
        "titles": titles,
        "loops": loops,
        "headline": (
            f"{len(stages)} steps. The harness does them in order, and goes back a step "
            "whenever a check says something is wrong."
            if stages
            else "There is no workflow to show yet."
        ),
    }
