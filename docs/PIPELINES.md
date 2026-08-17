# Pipelines

Many jobs, wired together, with gates between them.

A check suite answers one question: does this project work? A pipeline answers a
bigger one. Run these suites at the same time, scan the code for credentials,
only carry on if that went well enough, then run the unit tests, and try the
flaky one again before giving up.

Every piece of that already existed in the harness. What was missing was a way
to say how the pieces fit together, and a picture of it while it runs.

---

## The parts

A pipeline is **steps** and **arrows**. An arrow means "after": a step runs once
everything pointing at it is done. Steps with nothing between them run one after
another in a settled order, so the same pipeline behaves the same way twice.

A **gate** is a step that looks at what came before it and decides whether the
work goes on. With `all` it needs every step before it to have passed; with
`any` one is enough. When a gate shuts, everything after it is marked skipped
rather than failed, because it never ran.

Any step can be told to **try again**, up to five times. That is for the test
that fails on a slow morning, not for hiding a real failure: the number of
tries is shown beside the step in the log.

---

## The kinds of step

| Kind | What it does | What it needs |
| --- | --- | --- |
| Start | Where a run begins | nothing |
| Test suite | Runs your checks | a suite file, a tag, or one check id — or nothing for all of them |
| Unit test | Runs the project's own command | test, lint, or build |
| Security scan | Reads your files for credentials | which files, or nothing for all of them |
| Security gate | Carries on only if the scans passed | all, or any |
| Gate | The same, for anything before it | all, or any |
| Git repo | Says which branch you are on and what is uncommitted | nothing |
| AI drafts a test | Asks your model to write a test and saves it as a draft | what to write, and a file name |
| Keep the evidence | Writes what happened into one file | where to put it |

The Git step only ever reads: it runs `git rev-parse` and `git status` and
nothing else. It does not fetch, pull, commit, or push.

The AI step uses whichever model you have connected, which may be a
subscription you already pay for. See [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md).

What it writes goes into `.harness/pipelines/drafts`, and nowhere else. That is
deliberate. Writing straight into `tests/` was the obvious thing to do and it
was wrong: `tests/` is exactly where every test runner goes looking, so a
pipeline could ask a model for a "test" and have the very next step run
whatever came back. A draft sits where nothing runs it until you have read it
and moved it yourself.

---

## Four ways of looking at one pipeline

Across the top of the board there are four tabs. They are the same pipeline,
not four different things.

| Tab | What it is for |
| --- | --- |
| The picture | Boxes and arrows. Drag them about, join them up, watch them light up while a run goes. |
| The same thing as text | The whole pipeline written out. Change it here and the picture changes with it. Copy it to send to somebody. |
| How long each step took | One bar per step of the last run, laid out in time. The long bar is the slow step. It also says which one that was. |

![How long each step took](images/pipeline-timeline.png)

| What each step is for | Every kind of step, what it does, and what it can be told. No leaving the page to look something up. |

---

## Running less than all of it

Three things people want the first afternoon.

**Run only this.** Every box has it. While you are building a step, run that one
step and nothing else: try it, read what it said, change it, try it again.

**Carry on from here.** Step four of six broke, you fixed it, and running the
first three again would waste five minutes. This runs that step and everything
waiting on it. The earlier ones are marked "left as they were" — not "passed",
because they did not run this time.

**Ask me first.** A step can be told to ask about one of its settings when the
run starts rather than having it fixed now. Tick it in the step's settings and
the board says *Asks first*. Press Run and a short form appears. One saved
pipeline then covers "the quick checks" and "all of them" without being copied.

A run that did less than everything says so in every report. That matters more
than it sounds: a green run that only covered a quarter of the work is worse
than no run at all.

---

## Two more kinds of step

**Wait for a person.** The run stops there and waits. You see the question you
wrote on it, and two buttons: *Carry on* and *Stop here*. Good before anything
that matters — a release, a push, anything hard to take back. If nobody answers
within an hour, it stops rather than holding on for ever, and says nobody
answered.

**Run another pipeline.** Point it at one of your saved pipelines and it runs
that whole thing as one step of this one. Instead of copying five steps into
three pipelines, you keep them in one and call it. A pipeline that calls itself,
or two that call each other, are stopped after three levels and told so plainly.

---

## Starting from a ready-made one

There are ten, in three groups, with a search box over them. Type *release*,
*security*, *tag*, *git* — the words you would actually use. Each one says what
it is for and when to reach for it, and every one really runs; nothing in that
list is a picture of something that does not work.

This is the single biggest thing for somebody new. A blank board is the hardest
thing to hand anybody.

---

## What a pipeline cannot do

There is no "run this shell line" step, and that is on purpose. A pipeline is a
file people pass around and open on each other's machines. A file that can run
anything is a file nobody should open. Every step here does one named thing the
harness already knew how to do.

Paths are confined to the project the same way the rest of the harness confines
them. A step cannot write above the project folder, and it cannot write into the
folders the harness and git keep their own workings in.

---

## Where they live

```text
.harness/pipelines/my-pipeline.json
```

Ordinary JSON, named after the pipeline. Put it in your repository and everyone
gets the same one.

```json
{
  "name": "Before a release",
  "nodes": [
    {"id": "start", "kind": "start", "label": "Start", "settings": {}},
    {"id": "scan", "kind": "security_scan", "label": "No credentials", "settings": {}},
    {"id": "gate", "kind": "security_gate", "label": "Safe to carry on",
     "settings": {"needs": "all"}},
    {"id": "tests", "kind": "unit_test", "label": "The tests",
     "settings": {"command_kind": "test", "tries": 2}}
  ],
  "edges": [
    {"from": "start", "to": "scan"},
    {"from": "scan", "to": "gate"},
    {"from": "gate", "to": "tests"}
  ]
}
```

---

## Using the board

Open `harness ui` and go to **Pipelines**.

- **Add a step** from the list on the left. Steps are grouped: flow, tests,
  security, integrations.
- **Connect** on one box, then press another box, draws an arrow between them.
  Press Connect again on the same box to stop.
- The small cross in the middle of an arrow cuts it.
- **Settings** on a box asks only for what that kind of step needs, and for how
  many times it should try again.
- **Check it** says whether the pipeline would run, without running any of it. A
  pipeline that goes round in a circle is refused, and the circle is named.
- **Run** starts it. Each box lights up as the work reaches it, and the log
  underneath says what each step said.
- **Stop** asks the run to stop after the step it is on.
- **Save**, **Save as**, and **Delete** keep pipelines by name.

One pipeline runs at a time. A pipeline starts real suites and real commands,
and two at once would fight over the same project.
