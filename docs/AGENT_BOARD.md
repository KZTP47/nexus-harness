# The agent board

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

**Agents.** A name, which assistant on this machine it uses, and one line
saying what it is for. An agent is not a new kind of program: it is one of the
assistants you already pay for, given a name and a job. Two agents can both use
Claude, and they will not read each other's words.

**Projects.** A folder, and the jobs you want done in it.

**Works on.** Which agents are on which projects. Many to many: one agent can be
on three projects, and one project can have three agents. This is the part that
makes it a board rather than a list - it is meant for work that spans more than
one folder.

**Talks to.** Which pairs of agents may pass notes to each other while a run is
going.

## Two agents never share a conversation

An agent's conversation is filed under its own name, not under the assistant it
uses. Two agents both on Claude would otherwise each read the other's half of
it, which is worse than useless: it is one assistant answering as though it were
two, and quietly agreeing with itself.

So the name matters. Rename an agent and its conversation moves with it. Two
agents may not have the same name, and the board says so plainly rather than
letting it happen.

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

Press **Add an agent** and give it a name. Press **Add a project** and give it a
folder. Pick a box to open its settings on the right.

For an agent you can change its name, which assistant it uses, and what it is
for; tick the projects it works on and the agents it may talk to; and talk to it
in the box underneath. **Save this agent** writes the top part down - the ticks
save themselves as you make them.

For a project you can write down the jobs you want done there, and take it off
the board again. Taking a project off the board changes nothing in the folder.

Drag a box to move it, or pick it and use the arrow keys - holding shift moves
it a small step, for lining two of them up. **Tidy the board** puts every box
back in rows: agents on top, projects below.

**What is not ready** on the left lists everything standing between the board
and it being any use - an agent with no assistant, a project nobody is on, a
project with no jobs written down, a folder that is no longer there. It is in
the order somebody would fix them.

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

## Where it is kept

Beside your own settings, in `swarm.json`, next to the list of projects - not
inside any project. A board spans projects and belongs to none of them, and a
board kept inside one project would be invisible from the others.

Each agent's conversation is kept in the project's own `.harness/chats`, which
is never committed, with credentials taken out before anything is written down.

