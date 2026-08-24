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
down starts nothing. The board is what you want; **Set them going** is when any
of it happens, and it is the only thing on the tab that reaches an assistant.

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

Inside the maximised view, each connected pair has its own list of saved chats.
The transcript file name is generated from the stable pair and chat ID, so two
pair workspaces never read each other's words.


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

## Setting them going

**Set them going** acts on the board.

Every agent is asked about each project it is on, one at a time, and told the
folder and the jobs wanted there. That is the first round, and every agent takes
it on its own: nobody has read anybody else's answer yet.

Then the second round. Each agent that is allowed to talk to somebody else on
the same project is shown what those agents said, and asked again - to take
their answer into account and to say plainly where it disagrees. An agent that
read the others before writing its own answer is not a second opinion; it is the
first opinion agreeing with itself, which is why the rounds are that way round.

Everything each agent says lands in its own conversation, so you can pick its
box afterwards and read the whole thing, or carry on talking to it.

One at a time, on purpose. These are command line tools signed in to somebody's
subscription, and six at once is six ways to be turned away.

**Stop** stops it. The turn already asked for finishes, because there is no way
to un-ask it, and nothing after it is asked. The list says which turns were
never taken.

If there is nothing to do - no agent with an assistant, no project with jobs, or
nobody on a project - the press is turned down with the reason, before any
assistant is asked.

## Coming to this from a fresh download

The board needs nothing set up. Clone the project, say the settings file is
yours (`python scripts/harness.py trust` - the harness asks for this the first
time, because a settings file can name commands to run), start the panel with
`python scripts/harness.py ui`, and the tab is there - the yellow one, second
from the left.

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

