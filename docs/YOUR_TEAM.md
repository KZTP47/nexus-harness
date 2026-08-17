# Your team

The assistants you already pay for, working together on one job.

Most organisations have seats, not keys. Somebody signs in to Claude once,
somebody signs in to Copilot once, and both are then usable from the command
line without a key anywhere. The harness can drive either of them. What it
cannot do is guess who you have, what each one should do, or who should check
whose work.

That is this tab.

![Your team](images/your-team.png)

---

## On this machine

Open the tab and it looks. For each assistant it says one of three things:

- **Ready, and already set up.** Nothing to do.
- **Ready. It is not set up yet.** It is installed and signed in; press *I
  don't care, just set them up for me* and it is wired in.
- **Not on this machine**, and what would have to happen first.

It never says an assistant is there when it is not. The looking is the same
looking the seats setup does, so the two views cannot disagree.

---

## The picture

A box is one assistant with a job. An arrow is a hand-over: who passes the work
to whom.

| Job | What they do |
| --- | --- |
| Plans the work | Reads the task and writes down what has to happen, before anybody changes a file. |
| Writes the code | Takes the plan and makes the change. |
| Reads the work back | Looks at what was written and says whether it really does what was asked. |
| Puts several answers together | Where two assistants answered the same question, decides what to keep. |

Colour says the job. The name under it says who does it, and turns red if that
one is not ready. A dashed arrow is one the work only takes sometimes — the way
back to the writer when the review says no.

Everything works with the keyboard alone: Tab to a box, arrow keys move it, C
connects it to another, S opens its settings, Delete takes it out.

Beside the picture the same thing is written out as sentences, so nobody has to
look at a picture to know what the team does.

---

## The ready-made team

Press *Use the ready-made team* and you get this:

1. The first assistant reads the task and plans it.
2. The second one writes the code.
3. The first one reads that work back.
4. If it is not right yet, it goes back to the writer — up to three times, then
   it stops. A team that can argue forever is a team that never finishes.

**The one that reads the work back is deliberately not the one that wrote it.**
Two assistants trained apart tend not to share a blind spot. If only one is
ready on your machine, it does every job and the panel says so plainly — that
still works, it just catches less.

---

## One of your own

*Add one of your own* is for everything the four ready-made jobs do not cover.
You choose:

| What | What it means |
| --- | --- |
| What to call it | Your own name for the box. "The one who checks the maths." |
| What it does | One of the four jobs, so the rest of the harness knows what to do with its answer. |
| Who does it | Any assistant found on this machine, or any model you added yourself. |
| Which model | Leave it empty for the usual one, or name a particular model. |
| How it is asked | One set prompt, or a conversation you can carry on. |
| What to tell it | The prompt itself. |

**One set prompt** is the ordinary one: the same instructions every time, the
run does not stop. **A conversation you can carry on** is for work nobody can
write down in advance — the run stops there, you talk to it as long as you like,
and you say when to carry on. The board says *Stops here to talk* on that box,
so nobody is surprised.

---

## A model of your own

*Add a model* wires up something that is not one of the signed-in tools:

- **A model running on this machine** — Ollama or anything that answers like it.
  Nothing leaves the machine.
- **A service, with the key kept in an environment variable** — you give the
  **name** of the variable, like `OPENAI_API_KEY`. Never the key itself. The
  harness reads the key from there when it runs, so nothing secret is written
  into a file that travels. Paste a key where the name goes and it is refused.

It is added to your own settings file and left there. Everything else in that
file is untouched, and the assistant used by default does not change.

---

## Making your own

*Add somebody* puts a new box on the board. Open it to choose who does it and
what they do. Connect the boxes with arrows. Name it and save.

Two rules the panel keeps:

- **A job is only ever given to somebody really here.** The list you choose
  from is what was found on this machine. Anything not ready cannot be picked.
- **Nothing that could not run is saved.** Press *Check it* at any time — it
  says what is in the way in plain words, and saves nothing.

A saved team is an ordinary saved workflow in `.harness/workflows`. Everything
that already runs a workflow runs a team; this is not a second way of doing the
same thing.

---

## What they say to each other

Along an arrow the work moves. Beside it there is a board where an agent can
leave a note for another — *"the parser caches by file name, watch out"* — and
that is the part that makes two assistants better than one used twice. See
[TEAM_NOTES.md](TEAM_NOTES.md) for what that board is and what it refuses to
carry.
