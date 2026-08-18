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

It is kept, so you can close the panel and carry on tomorrow. The last forty
turns are held; older ones drop off the top.

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

- **It cannot read your files.** Paste what you want it to see.
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
| One message | 6,000 letters. Longer belongs in the project, with the message pointing at it |
| One conversation | The last 40 turns |
| One answer | 20,000 letters, and 3 minutes to arrive |
| Ask all of them | Up to 6 at once |

---

## When it will not answer

The reason comes back as one sentence, not a page. A signed-in tool that will
not answer usually says why in one line and then wraps it in a screen of
machine-readable detail; that line is what you see.

What you typed stays in the box, so nothing is lost, and the conversation is not
left showing a message that never went through.
