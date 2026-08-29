# What it knows

The notes the harness keeps about you and your project.

A harness that works on the same project every day learns things. How you like
to be answered. Which command really runs the tests here. What went wrong last
Tuesday and what fixed it. Most tools keep that inside a database, where it is
the tool's private business and you have to take its word for it.

This keeps it as notes. One markdown file each, in `.harness/vault`, with a few
lines at the top and links written `[[like this]]`. Open the folder in any
editor and it is a set of notes about your project — readable, correctable,
deletable, and yours to hand to somebody else.

The note editor shows a live character count. A note body can contain up to
200,000 characters. Nexus never clips a longer note: the editor keeps the full
text and asks you to shorten it before saving, and the server enforces the same
disclosed limit.

---

## The four kinds

| Kind | What it holds |
| --- | --- |
| About you | How you like to be worked with. |
| How to | Something that worked, written down so it can be done again. |
| About this project | What the harness has worked out about the code. |
| Lesson | Something that went wrong once, and what fixed it. |

A note looks like this:

```markdown
---
title: They want plain English
kind: about-you
tags: [writing]
sure: 0.9
learned: 2026-08-17
touched: 2026-08-17
from: run-3f2a
uses: 4
worked: 4
---

Short answers, no jargon. See [[how-to-answer-them]].
```

Nothing there needs the harness. `title`, `kind` and the body are the whole of
it; everything else is bookkeeping the panel fills in.

---

## The picture

Every note is a circle and every link is a line. The picture settles itself:
circles push each other apart, links pull their two together, and what is left
is a shape where things that belong together sit together.

- **Colour** says which kind of note it is.
- **Size** says how connected and how used it is.
- **Dimmed** means nothing has touched it for months, so it may no longer be
  true.
- **A dashed outline** is a note nobody has written yet — something a note
  points at that does not exist. Press it to write it.

The list beside the picture says the same thing in words, and the picture works
with the keyboard alone: Tab to it, arrow keys walk the notes, Enter opens one.

---

## How a note earns its place

Open a note and say whether it helped.

- **That helped** counts a use, counts a win, and nudges how sure the harness is
  about it upwards.
- **That did not help** counts the use, and nudges it down harder than a win
  nudges it up.

A note used ten times that helped ten times is worth more than one written once
and never touched, and the picture shows the difference without anybody reading
a word. One bad afternoon does not throw away what a note has earned: what it
is worth moves towards how it actually goes rather than jumping.

Nothing is ever certain. A note tops out below complete confidence, on purpose.

---

## Where the notes come from

**You write them.** New note, at any time.

**The harness writes them.** *Learn from the runs* reads what it already
remembers from past runs and writes the parts worth keeping as notes. It never
writes over a note you have edited: your version wins, always.

**A note that goes stale asks to be checked.** After ninety days with nothing
touching it, a note is marked as going stale. Not deleted and not disbelieved —
shown, so you can say whether it is still true. A harness that keeps believing
everything it ever learned ends up confidently wrong.

---

## Where the ideas came from

Two of these are borrowed, openly, from other self-improving harnesses — Hermes
from Nous Research among them:

- **A model of the person, not a fixed personality.** What the harness knows
  about how you work deepens with use, rather than being configured once.
- **Skills that improve as they are used.** A "how to" note here is that idea:
  written down after something worked, and kept or dropped on the evidence of
  how it goes afterwards.

What is different here is that all of it is a folder of markdown files you own,
rather than state inside the tool.
