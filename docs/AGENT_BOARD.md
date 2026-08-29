# The AI Agent Swarm orchestrator

One picture of every agent you have, the projects you want worked on, and the
lines between them.

The harness could already do each of these things on its own tab. **Your team**
finds the assistants on your machine. **Talk to them** puts a question to one.
The project bar switches which project you are looking at. What none of them
showed was all of it at once - which agents you have, which projects they are
on, which of them are allowed to talk to each other - or let you change any of
it in one place.

That is what the board is for.

## What is on it

**Agents.** A name, which assistant on this machine it uses, one line saying
what it is for, and its own icon or profile picture, accent colour, and
chat-bubble colour. A picture can be chosen from the desktop, then zoomed and
hue-shifted in the settings preview. An agent is not a new kind of program: it
is one of the assistants you already pay for, given a name and a job. Two
agents can both use Claude, and they will not read each other's words.

**Projects.** A folder, and the jobs you want done in it.

**Works on.** Which agents are on which projects. Many to many: one agent can be
on three projects, and one project can have three agents. This is the part that
makes it a board rather than a list - it is meant for work that spans more than
one folder.

**Talks to.** Which pairs of agents may pass notes to each other while a run is
going.

## Conversations belong to an exact pair

The full chat has a left pane of durable conversations grouped by the exact two
agents on a green communication line. Each pair can have several chats. You can
create a fresh one, return to an older one, or delete one transcript without
touching any other pair's history.

Pair identity uses stable board IDs rather than provider routes or display
names. Two agents both using Claude therefore remain distinct, renaming either
agent does not orphan its pair chats, and GPT Codex ↔ Claude can never read the
history from GPT Codex ↔ Gemini.

Each chat stores one active project. Its dropdown contains only folders that
both agents work on. That selection is included in their authoritative board
context and is the only folder the Work action can change; with no shared
project selected, file work is refused.

## Nobody talks to anybody unless you say so

A pair that has no line between them is a pair that will not hear from each
other. Off is the answer when nothing was said.

That is the safer way round to be wrong. A reviewer that can read the writer's
notes before writing its own review is not an independent reviewer, and the
whole reason for having two assistants is that one model's blind spot is not
usually the other's. If you want them talking, tick the box and mean it.

## Nothing happens until you press the button

Adding an agent starts nothing. Drawing a line starts nothing. Writing a job
down starts nothing. The board is what you want; **Work until the goals are
achieved** starts project work, and **Get two-round advice** starts optional
discussion without changing files. Those are the deliberate actions on this
tab that reach assistants.

A board that set twelve assistants going the moment you dragged a line would be
a board nobody would dare touch.

## Using it

Under the board: **Add another agent**, **Remove an agent**, **Add another
project folder**, **Remove a project folder**, **Tidy the board**, **Look
again**. Remove works on whichever box is picked, so it is off until one is.

Every box carries two small buttons of its own:

  - the **gear**, which opens that box's settings on the right;
  - the **chat** button, on agents, which opens a chat for that agent on the
    board.

Every line carries a **gear** too, halfway along it, with what the line means
beside it: **works on** between an agent and a project, and **communicates?
YES** or **communicates? NO** between two agents. A pair who may not talk still
gets a line, crossed out and grey, so there is always a gear to press. Pressing
it opens the line on the right, where one tick turns it on or off.

An agent's settings hold its name, which assistant it uses, what it is for, its
fallback icon, optional profile picture, picture zoom and hue, colours, the
projects it works on, and the agents it may talk to. Names and appearance are
previewed immediately on both the settings card and the real board card.
Agent fields save automatically after a short typing pause and immediately when
a control is finished. The panel keeps an unsaved draft across board redraws,
flushes it when another agent is opened or the panel closes, and says whether it
is waiting, saving, saved, or needs a retry. **Save now** remains as a manual
fallback. The ticks save themselves as you make them. The appearance follows the speaker into every chat, including
a connected agent speaking in somebody else's chat.

A project's settings hold the jobs you want done there. Taking a project off the
board changes nothing in the folder.

Drag a box to move it, or pick it and use the arrow keys - holding shift moves
it a small step, for lining two of them up. **Tidy the board** puts every box
back in rows: agents on top, projects below.

Saved boards are named workspaces. Opening one records that name in the live
board itself. Nexus therefore returns to the last named board you opened after
the desktop app closes and starts again, including any edits made after it was
opened. The current saved board is marked in the list. Deleting that saved board
clears the marker without deleting the live arrangement still on screen.

**What is not ready** on the left lists everything standing between the board
and it being any use - an agent with no assistant, a project nobody is on, a
project with no jobs written down, a folder that is no longer there. It is in
the order somebody would fix them.

## Talking to one of them

The chat button on an agent opens a chat box on the board, joined to that agent
by a thin line. It is a big box on purpose: a chat squeezed into a strip at the
edge of a page is a chat nobody uses, and the answer is the part you came to
read.

Several can be open at once, one launcher per agent, and each can be dragged where you
want it. Two of them can be waiting for an answer at the same time, because they
are two different assistants being asked two different things. **Start again**
empties that one chat and no other. **Close** puts it away; nothing said is
lost, and opening it again reads it back.

When a request involves connected agents, each completed reply appears in the
chat immediately. Nexus does not wait for every provider and then drop the
whole exchange onto the screen at once. The lead can still be working on the
final answer while finished peer replies are already readable. Once the final
answer is saved, the temporary live view is replaced by the durable transcript
with the same named turns.

For confirmed project-file work, planning and execution are separate. Every
participant first proposes and reviews its contribution. Nexus then gives each
agent its own execution turn in board order, explicitly names that agent as the
actor, and applies only the complete file changes returned from that turn. A
later agent sees the real files produced by earlier turns, so a task such as
“Claude creates the file, then Codex populates it” is performed by those two
agents rather than repeatedly sent to whichever agent happened to lead the
chat. Every participant verifies the final on-disk state. Identical file
proposals do not count as progress. The project-work guard stops only after
fourteen consecutive identical engine-attested end-of-pass states, or after
four complete two-state oscillations (eight alternating states). Provider
paraphrasing and repeated reads are excluded from that progress identity.

The original user request is sent as the active prompt only for the independent
first round. After that, it remains authoritative goal context while each model
receives a new current-turn instruction: discuss the latest exchange, review the
current plan, execute its assigned contribution, or verify the newest on-disk
state. Completed informational questions are treated as closed and considered
silently. This keeps a long collaboration moving forward instead of making each
agent answer the opening question again on every round.

Each pair-chat composer also exposes its team-round policy. By default it is
unlimited while progress continues, so a real long-horizon conversation has no
arbitrary twelve-round ceiling. The user can instead enter an exact maximum;
that maximum applies independently to each discussion, plan-review, and
execution/verification phase. “Unlimited” removes only the numeric ceiling.
Nexus still stops a proven no-progress cycle.

Cycle detection follows actionable state rather than comparing whole replies.
For every participant it tracks completion, structured remaining work,
requested files, and whether the provider failed its structured turn. Planning
and discussion use the same deliberately patient thresholds: fourteen
identical actionable states for a stable loop, or eight alternating states for
four full `A → B → A` cycles. Project execution uses an even stricter
engine-owned identity: the actual relevant project-state digest, deterministic
verification result and unmet requirements, sealed causal receipts, and
authenticated transaction evidence. Cosmetic paraphrasing therefore cannot
keep dead work alive, while real file/evidence progress resets the guard and
permits the collaboration to continue.

Inside the maximised view, each connected pair has its own list of saved chats.
The transcript file name is generated from the stable pair and chat ID, so two
pair workspaces never read each other's words.

## The live shared collaboration ledger

Every multi-agent run also has a Nexus-owned shared ledger beside its ordinary
chat transcript under `.harness/chats/`. The append-only JSONL file is the
canonical record. A Markdown mirror makes the complete live exchange readable
to people and to desktop agents that can inspect project files. The maximised
pair chat shows its relative path and can open that readable mirror directly.

Nexus is the only writer. Each entry is numbered and hash-chained, and an
externally changed or damaged suffix is rejected rather than silently trusted
or overwritten. Provider text is stored as quoted conversation evidence, not
as an instruction to Nexus or to another provider. Credentials are redacted
before any entry reaches disk.

Each participant has an independent cursor. On every later round Nexus supplies
the current user goal, the latest structured shared state, the ledger paths,
and only the entries that participant has not seen. File-capable desktop agents
may additionally read the full Markdown mirror themselves. Web agents receive
the same projection in their turn because they cannot safely be assumed to
have local filesystem access. This keeps normal prompts bounded without making
Nexus the only place where the evolving conversation can be observed.
Preparing a prompt does not advance its cursor: Nexus commits the new cursor
only after that provider returns, so a failed call receives the same unseen
context again on retry.

The ledger is local runtime state and is ignored by Git. Starting a pair chat
again or deleting it removes its transcript, canonical ledger, readable mirror,
and cursor file together, so chat identities and retention boundaries remain
the same across every layer.


## The page they share, and the tray of chats

Agents write to a shared page rather than talking into each other's chats, and
every open chat lives in a tray along the bottom. See
[The page they share](THE_PAGE_THEY_SHARE.md) and
[The tray of chats](THE_CHAT_TRAY.md).

## Every conversation, down the side

The left of the board lists all of them: **You and The planner**, one row per
agent, and **The planner and The writer**, one row per pair that really passed
something. Under each name is the last thing said in it.

Press one of yours and that agent's chat opens on the board. Press a pair and
what those two passed is shown on its own, with a way back to all of them.

A chat you can only reach by finding the box it belongs to is a chat you stop
going back to, which is the whole reason this is here.

## What they said to each other

Under the board, folded away until you ask for it.

The second round of a run shows each agent what the others said - but only to
the agent. This is the same thing where you can read it: every answer that was
passed, who said it, who was shown it, and which project it was about. A pair
with no line passes nothing, and nothing appears here for them.

It is kept after the run finishes, so it is still there when you come back to
the panel tomorrow. Only the last run is kept: it is there to be read, not as a
history of everything that ever ran.

## Achieving the written goals

**Work until the goals are achieved** is the primary board action. Each project
needs at least two ready assigned agents connected by a green communication
line. Nexus shows the exact project folders and goal count before asking for
confirmation. It then opens or reuses a durable project chat for the pair and
runs each written goal through the long-horizon project-work engine.

This mode works on the real goal: agents plan, inspect and edit authorised
project files, run deterministic checks, review the result, and repair what is
still wrong. Nexus starts the next goal only when the current result says both
`goal_complete` and `verified`. If it pauses on a provider, tool budget, user
question, failing verification, or remaining work, the exact run stays in the
lead agent's chat. Use its **Resume** action; Nexus does not call the goal done
or move on merely because a turn ended.

## Getting advice without changing files

**Get two-round advice** is a separate optional mode. First, every agent is
asked about each assigned project independently. Then each agent that may talk
to another agent on the same project is shown those agents' answers and asked
again, including where it disagrees. This is useful for opinions and planning,
but it does not inspect, edit, or test project files.

Advice runs one assistant at a time to avoid avoidable subscription throttling.
**Stop advice run** lets the already-started turn finish and asks nothing after
it. Every successfully accepted answer lands in a durable board conversation
and, when the project page accepts the write, on the shared page. A stopped turn
or page-write failure is labelled explicitly rather than described as landed.

Direct board requests accept up to 200,000 characters and saved agent answers
up to 8,000,000. If the complete authorised page and handoffs exceed one direct
request, Nexus ingests every character in ordered 100,000-character chunks,
reduces complete evidence ledgers to at most 30,000 characters, and keeps a
hash receipt. Exact source is stored in independently verified, content-defined
8,192-to-32,768-character blocks whose rolling boundaries resynchronise after
local insertions. Extractions are reused only when exact chunk,
route, model, and policy identities match; reductions require the same ordered
input hashes and reduction policy. Successful turns collect obsolete tail blocks,
bound completed receipts and caches, and remove their working manifest. Failed
turns retain an exact reconstruction manifest instead of silently omitting or
overwriting context. The profile keeps the newest 128 completed advice receipts
and up to 4,096 provider-cache records or 256 MiB; active failed-turn manifests
are exempt from that success-only retention. These are disclosed processing
boundaries, not hidden truncation.

An agent role description accepts up to 100,000 characters. Each project goal
and direct board request accepts up to 200,000. Text at either exact boundary is
preserved—including outer whitespace and line endings; over-limit input is
measured and rejected visibly, never silently shortened.

Durable agent-to-agent mail has explicit flow-control boundaries rather than a
hidden text cut-off. A mailbox holds at most 2,000 queued records; if all 2,000
are still undelivered, a new send is rejected visibly and nothing existing is
discarded. One receiving turn takes at most 50 messages and 10,000,000 exact
characters. Additional queued mail remains durable and is reported as deferred
for the next turn—it is not shortened or acknowledged early.

If either action is not ready, the panel says why before contacting an
assistant—for example, a missing provider, missing project goals, or no
connected pair assigned to a project.

## Coming to this from a fresh download

The board needs nothing set up. Clone the project, say the settings file is
yours (`python scripts/harness.py trust` - the harness asks for this the first
time, because a settings file can name commands to run), start the panel with
`python scripts/harness.py ui`, and the tab is there - the yellow one, second
from the left.

The installed app carries its own contained Python verification runtime,
including a pinned pytest runner. When
running straight from a source checkout on Windows, the first task that needs a
contained Python test downloads the pinned official CPython 3.11.9 embeddable
archive from `python.org`, verifies its SHA-256, and caches the verified archive
in the current user's local app data. This is the only automatic setup for that
path: it installs no project packages and no browser. If it cannot be verified, Nexus
states that plainly and does not run project code outside containment.
Bare `pytest`/`py.test` commands use the engine-owned interpreter with
already-prepared pure-Python packages copied inside conventional project
`.venv`, `venv`, `__pypackages__/3.11`, `vendor`, or `src` locations. Nexus
does not run the project's interpreter or silently install project packages.
Native-extension incompatibilities are reported as limitations, never accepted
as successful verification.

For a project folder outside the Nexus checkout, press the project's **gear**
and open **Project test commands**. Nexus shows each discovered command as its
exact argument array and does not run it until you approve the displayed
fingerprint. The approval is kept on the local board and is bound to that
project path, command set, and the project files that selected the commands.
A path or test-configuration change expires it visibly; use the same panel to
review and approve the new fingerprint or to revoke approval. Exported or
imported board JSON never carries permission to execute project code.
Because portable boards contain absolute local project paths, another computer
may show those folders as unavailable. Press that project's gear and **Use a
different folder on this computer** to preserve its tasks, assignments, lines,
stable project/chat identity, and clear the old machine's command approval.

It will say what is not ready, which on a machine with nothing set up is: no
assistant is set up to be used by name. Open **Your team** and press **Set them
up**, and come back.

## Where it is kept

Beside your own settings, in `swarm.json`, next to the list of projects - not
inside any project. A board spans projects and belongs to none of them, and a
board kept inside one project would be invisible from the others.

Pair-chat metadata and transcripts are kept in the project's own
`.harness/chats`, which is never committed, with credentials taken out before
anything is written down.

