# On a timer

Have an automation run itself — every night, every weekday morning, every hour —
with nobody watching, and the report waiting for you afterwards.

Open `harness ui`, go to **Automations**, and pick **When it runs on its own**.

---

## How often

| | |
| --- | --- |
| Every hour | On the hour, all day and all night. For something quick |
| Every day | Once a day, at the time you pick. Weekends as well |
| Every weekday | Monday to Friday, at the time you pick |
| Once a week | On the day and at the time you pick |

Nobody writes five numbers and a star. If none of these fits, put two timers on
the same automation.

---

## The one more step

The harness does **not** sit in the background waiting for two in the morning. A
program that has to stay running is a program that is not running when you need
it: somebody closes the window, the machine restarts, and the night's run
quietly never happened.

Instead your machine's own scheduler is asked to run one short command every so
often:

```bash
harness timer run
```

That command looks at what is due, runs it, writes down what happened, and
stops. Nothing stays running in between. Your machine handles being asleep,
being restarted, and starting up again on its own, because that is what it is
for and it is better at it than we would be.

**The harness never sets this up for you.** Asking a machine to start something
on its own is your decision to make, so it writes out the exact line and stays
out of it:

```bash
harness timer install
```

Run what it prints. Every ten minutes is plenty — the timers themselves decide
what is actually due.

---

## What it will not do

- **Run two at once.** A run that takes longer than the gap between two
  firings would pile up on itself. The second one stands aside and says so;
  nothing is lost, because it is due again next time. A run that is going
  touches its lock every half minute, so a lock is only ever taken from a run
  whose machine stopped - never from one that is simply slow.
- **Catch up on everything missed.** A machine off for a week comes back to one
  run, not a hundred and sixty-eight, and it says how many it skipped. Past a
  thousand it stops counting and says "more than a thousand", rather than a
  number that would be wrong.
- **Run an automation that stops to ask a person.** There is nobody there at two
  in the morning. It says so when you set it up, rather than at two in the
  morning - and again if you turn one back on later, because the reason has not
  gone away in the meantime. It is a warning, not a wall: say yes, or add
  `--anyway`, and it goes on. The refusal lives where a timer is written down, so
  it holds for the panel, for a terminal, and for anything else talking to the
  harness. If the panel cannot find out, it says that and still asks - not being
  able to check is not the same as nothing being wrong.

  It follows the automations one runs inside itself, exactly as deep as a run
  does and no shallower, so an ask hidden a step or two down is still found and
  still named - along with which automation to go and look at.

  An automation you have not drawn yet is a different thing, and is not refused.
  You get told, and the timer says so again if it comes round before you draw
  it.
- **Fire the moment you make it.** A timer added at noon does not run the
  night's job at noon.

---

## From the command line

```bash
harness timer add "Every night" "Before a release" --how-often every-day --at 02:00
```

```bash
harness timer list
```

```bash
harness timer run
```

Also: `harness timer on <name>`, `harness timer off <name>`,
`harness timer remove <name>`, and `harness timer install`.

And the command the timer itself uses, which is worth having on its own — an
automation, run without the panel:

```bash
harness automation run "Before a release"
```

It answers 0 when everything passed and 1 when it did not, so a build server can
use it as it stands.

---

## Where it is kept

```text
.harness/timers/every-night.json
```

Ordinary JSON, one file per timer, holding when it runs and the last twenty
times it did. Put them in your repository and everyone gets the same ones.

`.harness/timers/.what-happened.json` holds when each was last looked at. That
one is about this machine and not about the project - your machine was off last
week, mine was not - so it is left out of the repository, along with the lock a
run holds while it goes. If it is ever unreadable - a hand edit that went
wrong, a write cut off halfway - it is put aside as
`.what-happened.json.could-not-be-read` and started again, and you are told,
rather than every timer quietly looking brand new.

What a run printed is kept with it, and anything that looks like a key or a
password is taken out first. This folder is meant to be committed, and a
committed key is a key you cannot take back.

---

## Being told, rather than going to look

A timer leaves the report where you can find it. If you would rather be told,
`harness tell` sends word to Slack, Discord, Teams, Telegram, email or any
webhook when a run does not pass. That part needs a key you go and get, and it
says so plainly. See [BEING_TOLD.md](BEING_TOLD.md).

---

## What it cannot promise

**A step that has already started is not cut short.** An hour is the longest a
run may take, and that stops the *next* step from starting - it cannot reach
inside a step that is already waiting on something. A check with no time limit
of its own can still sit there. Give the slow thing its own limit; the harness
cannot give it one from outside.

**Running one by hand is extra, not instead.** Pressing *Run it now* does not
move the timer: tonight's run still happens tonight.

**A timer only holds off a run whose machine really stopped.** If a run is going
and the clock jumps - somebody fixes a wrong clock, or the machine is told the
time by the internet - the lock is not taken, because the harness also asks
whether that run is still on this machine. Anything it cannot answer counts as
still going: one skipped firing costs you nothing, and two copies of your suite
at once costs you a morning.

That question is asked properly. A process number on its own is not enough - the
machine hands the same number out again once the first program is gone - so the
lock also holds the moment its run started and the name of the machine it was
on. A number handed out again started at a different moment, and a lock from
somebody else's machine is not about this one. And whatever it says, no lock is
believed for longer than a day, so a lock can never turn into a project that
never runs anything again.

**A run does not bring back a timer you took off.** Take one off while it is
running and the run still finishes and still tells you what it did, but there is
nothing left to write it on, so nothing is written.
