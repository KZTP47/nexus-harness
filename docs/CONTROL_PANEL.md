# Cooperative Control Panel

The local control panel edits schema-v2 workflow graphs. It keeps provider credentials outside the browser. Agent nodes refer to named provider profiles from harness configuration.

## The first screen

Two things there explain and do the setting up, so nobody has to read the rest
of this page before their first run.

**What happens when you ask for a change** draws the workflow as a row of
plain-words boxes: you ask, it plans, it changes the files, each check runs, a
second model reviews, done. The boxes come from the graph the harness will
really run, so editing the workflow changes the picture. **Show me how it
works** walks through them one at a time; during a real run the same boxes show
what the work has reached.

**I don't care, just do it for me** sits on every way of connecting a model. It
does the parts a program may do — start Ollama if it is installed, fetch the
model, write the provider route, trust the settings file — and names the one
part it will not: installing software, or making a key. A key's value is never
read, never shown, and never written down; only the name of the environment
variable is.

Trusting follows the same rule as everywhere else. A settings file that was
already there and never trusted is left untrusted, because it can start
programs and nobody has read it. The panel says so and gives the command.

## Talk to them

A box to type in, and whichever assistant you have hooked up answers. Everything
set up on this machine is in the list on the left; anything on the machine but
not wired up yet is there too, greyed, with what to do about it.

The conversation is kept, so it survives closing the panel. **Ask all of them**
puts the same question to every one that is ready, at the same time, and lays
the answers out side by side.

It cannot read files, run anything, or change anything. See
[TALK_TO_THEM.md](TALK_TO_THEM.md).

## The agent board

One picture of every agent you have, the projects you want worked on, and the
lines between them. Add an agent and give it a name and an assistant; add a
project folder and write down the jobs wanted there; tick which projects each
agent works on and which other agents it may talk to. Drag a box to move it.

Each agent keeps its own conversation, filed under its own name, so two agents
both using Claude never read each other's words. A pair with no line between
them never hears from each other at all: off is the answer when nothing was
said.

**Set them going** acts on the board. Every agent is asked about the projects it
is on, one at a time and on its own; then the ones allowed to talk are shown
what the others said and asked again. While it is going the board cannot be
changed, and the panel says why. See [AGENT_BOARD.md](AGENT_BOARD.md).

## Look it up

Three questions about your own code, on their own tab: **Where is it?**, **What
uses it?**, **What is it?**

Type a name and press one of the three. Every answer says whether it is exact -
a tool built for that language was asked - or a guess from matching the text of
your files. Click a place it found and the file and line fill in, so the next
question is exact.

The second panel lists the language servers it knows about, says which are
installed, and gives the one command that installs each missing one. See
[LOOK_IT_UP.md](LOOK_IT_UP.md).

## Agent nodes

Select **Add agent**, then set:

- Agent type: planner, coder, evaluator, or merge.
- Provider route: a named entry from `providers`.
- Model: the configured model or an explicit override.
- Role: a short name shown in run and usage records.
- System prompt: instructions specific to this node.
- Capabilities: `workspace.read` for repository evidence and `workspace.write` for a coder's staged edits.

Planner, evaluator, and merge nodes are read-only. Only coder nodes may request staged writes. Verification commands come from configured or detected project commands; the UI does not grant arbitrary shell or Git access. API-key values never enter the graph. The provider catalog reports only the environment-variable name and whether a value is present.

## Cooperation

Connections have three transfer modes:

- `state`: pass selected shared-state fields to the next node.
- `delegate`: give a bounded subtask to another agent and declare its return fields.
- `merge_input`: place selected fields into a named input slot on a merge node.

A merge node waits for every required slot. The current `implementation_plan` output contract combines those inputs into a typed planner result in the configured output state field. Each delegation cycle still needs an iteration limit and timeout.

`our_harness.cooperation.CooperativeScheduler` supplies deterministic fan-out and fan-in scheduling state. It returns ready work in graph order, caps parallel work and total dispatches, waits for required merge slots, and enforces a wall-clock deadline. Provider execution stays in the workflow runtime.

Runtime integration uses this sequence:

```python
scheduler = CooperativeScheduler(
    graph,
    max_parallelism=provider_pool_limit,
    max_dispatches=workflow_dispatch_limit,
    timeout_seconds=remaining_workflow_seconds,
)
scheduler.set_entry_state(shared_state)

while not terminal(scheduler.snapshot()):
    ready = scheduler.ready()
    results = dispatch_ready_agents(ready)  # Runtime owns provider calls.
    for dispatch, result in zip(ready, results):
        if result.error:
            scheduler.fail(dispatch.node_id, result.error)
        else:
            scheduler.complete(dispatch.node_id, result.state_update)
```

Each `CooperativeDispatch` contains `node_id`, `node_type`, a detached `inputs` object, and the node attempt number. `complete()` accepts a shared-state update. It evaluates outgoing conditions against the combined shared state, copies only declared edge fields, marks delegation origin, fills named merge slots, and reopens downstream nodes when a bounded repair edge starts a new generation.

`snapshot()` returns versioned JSON with shared state, attempts, ready/running/completed/failed nodes, retained inputs, merge slots, loop counts, and elapsed deadline state. Save it with an atomic file replacement. Restore it with `CooperativeScheduler.restore(graph, snapshot)`. Restore rejects malformed state and a graph hash mismatch.

A call that was running during a crash becomes ready after restore. Its retained input and attempt number stay unchanged, and redispatch does not consume another logical dispatch. Provider calls and tool effects must use a stable idempotency key based on the run ID, node ID, and attempt. A second crash before redispatch preserves the same work. Global and per-loop elapsed time also carry across restore.

## Live usage

The usage table is fed by normalized provider-request records. It shows the agent, provider route, model, input, output, reasoning, tool-use and billed-output token counts, latency, micro-US-dollar cost, price status, and snapshot ID. Prices come from reviewed configuration snapshots. The browser does not contain provider prices.

Local providers may report zero cost. A remote request with no price record appears as unavailable unless policy rejects it before the call.

## Shared memory

The Memory view reads bounded records from the local memory database. Links distinguish:

- `discovered_by`: the agent wrote the record.
- `read_by`: the agent retrieved the record for later work.

The browser follows bounded cursor pages instead of silently dropping records after the first page. The table below the map contains the same information for keyboard and screen-reader use. Raw embeddings are never returned.

Prompt-cache contents are not available from providers. The UI may show stable prefix IDs and provider-reported cached-token counts; it must not describe these as stored cache contents.

## Prompt history

The Prompt history view reads prompt versions, their parent IDs, evidence, runtime observations, and active state. Runtime agent system prompts are content-hash deduplicated and remain inactive history. The context compiler reads only active reviewed versions, so runtime history is not injected into unrelated requests. Two versions can be compared side by side. Promotion and rollback remain subject to the existing review-bound refinement flow.

## Keyboard controls

- `Tab`: enter or leave the workflow graph.
- Arrow keys: focus the nearest node.
- `Enter`: select a node or finish a pending connection.
- `C`: start a connection from the focused node.
- `Ctrl+Arrow`: move a node by 8 pixels.
- `Ctrl+Shift+Arrow`: move a node by 32 pixels.
- `Delete`: remove the focused node.
- `Ctrl+Z`: restore the last graph edit.
- `Escape`: cancel a pending connection.

Every drag action also has a click and keyboard path. The Workflow outline exposes every node and connection as ordinary buttons.

To connect with a pointer, drag from the output port on the right side of a node to the input port on the left side of another node. The preview and target turn green for a valid connection and red for an invalid one. Release on a green target to add the connection. Release elsewhere, press Escape, or trigger pointer cancellation to stop without changing the graph.

## Read API

The loopback server exposes token-protected read endpoints:

- `GET /api/catalog`
- `GET /api/memory?after=0&limit=100&query=&kind=`
- `GET /api/usage?after=0&limit=100&run_id=`
- `GET /api/prompts?after=0&limit=100&name=`
- `GET /api/seats` and `GET /api/setup/do-it`

Responses are bounded and redacted. Cursor fields support paging. `/api/events?meta=1` also reports an event-buffer gap so the UI can state when older live events were dropped.

## Accessibility checks

Before release, verify:

- Complete graph editing without a pointer.
- Focus remains on the selected control after graph updates.
- Dialog focus remains inside the dialog and returns to its opener.
- Text contrast is at least 4.5:1 and control/focus contrast is at least 3:1.
- Status uses text as well as color.
- Layout works at 200% zoom and 320 CSS pixels.
- Reduced-motion mode removes scripted transition delays.
- Workflow, memory, and prompt visuals have synchronized text tables.
- NVDA with Firefox and Narrator with Edge can operate the full add, connect, inspect, and run flow.
