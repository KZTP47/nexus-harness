# Talk to them

A box to type in, and whichever assistant you have hooked up answers.

Open `harness ui` and go to **Talk to them**.

---

## Who you can talk to

Everything set up on this machine is in the list on the left: a seat you signed
into, a model running here, a route with a key in an environment variable. Pick
one and type.

Anything that is on the machine but not wired up yet is in the same list,
greyed, with why and what to do about it. "Nobody is here" is a worse answer
than "here is who you could have in one press".

If the list is empty, open **Your team** and press **Set them up**.

---

## The conversation

It is a conversation, not a row of unrelated questions: what was said before
goes with the next thing you say, so "and in three words?" means something.

It is kept, so you can close the panel and carry on tomorrow. Every canonical
turn remains on disk. The ordinary chat panel and its next provider request use
the most recent 40 complete turns as a projection; that display/request bound
does not delete the older turns.

Ordinary conversation stays available if Nexus has paused project execution
because a folder was copied or its local authority registration needs repair.
Talking to an assistant cannot run project commands or change project files, so
it does not borrow that mutation authority.

Long-horizon team work keeps its full canonical history in the append-only,
paged collaboration ledger. Every discussion, planning, execution,
verification, and final-synthesis request uses the same disclosed projection:
up to 120,000 characters of the newest complete turns plus a deterministic
semantic summary of older requirements, decisions, facts, blockers, paths, and
checkpoints, bounded at 40,000 characters. Those two numbers bound what is sent
to a provider in one request; they are not retention limits and Nexus never
clips a turn in the middle.

**Enter** sends. **Shift and Enter** starts a new line.

**Start again** throws the conversation away and begins a fresh one. There is
no undo on that, so it asks first.

---

## Ask all of them

The button next to Send puts the same question to every assistant that is ready,
all at the same time, and lays the answers out side by side.

That is what two subscriptions are actually for. One model's blind spot is not
usually the other's, so two answers to the same question is worth more than one
answer twice.

One that will not answer does not stop the others: it gets its own box saying
what went wrong.

---

## What it will not do

- **It reads only what you explicitly give it authority to read.** Attach a
  file, point it at project material through a project-working feature, or
  paste the relevant text. It does not silently roam outside that boundary.
- **It cannot run anything, or change anything.** Everything that changes your
  project goes through a run, where there is a record of it.
- **It never keeps a credential.** Everything you type and everything it says
  has credentials taken out before anything is written down.
- **It never leaves this machine except to answer you.** The conversations live
  in `.harness/chats`, which is not committed.

---

## Bounds

| | |
| --- | --- |
| One message | 200,000 characters. An over-limit message is rejected with its measured size and is never silently shortened; attach or point at a file for larger source material |
| Stored conversation | Every canonical turn remains durable. Ordinary Talk projects the newest 40 complete turns into its screen/provider request; long-horizon work uses the disclosed 120,000-character recent projection plus 40,000-character semantic summary while preserving the full ledger |
| One canonical answer | 8,000,000 characters. Overflow is a visible failure, never a plausible-looking truncated success |
| Time to answer | The effective timeout comes from the selected route/provider configuration and is shown by Nexus. The current global safety maximum is 600 seconds; web-chat bridges use their own disclosed configured wait. There is no universal short cutoff |
| Ask all of them | Up to 6 at once |

A provider or model can have a smaller context or output window than Nexus.
Nexus cannot enlarge that external limit, but it reports the provider's
redacted reason instead of hiding the failure or silently discarding text.

---

## When it will not answer

The reason comes back as one sentence, not a page. A signed-in tool that will
not answer usually says why in one line and then wraps it in a screen of
machine-readable detail; that line is what you see.

What you typed stays in the box, so nothing is lost, and the conversation is not
left showing a message that never went through.
