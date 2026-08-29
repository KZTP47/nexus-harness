# Pipelines: work that runs itself

This is the tab for automating work. Draw each job as a box, join the boxes
with arrows, and press Run.

**What you can do here**

| | |
| --- | --- |
| Chain up the jobs you already run | Your checks, your tests, a scan for credentials, a look at the repository |
| Stop the work when something is wrong | A gate lets it go on only if what came before passed |
| Try again, and wait before you do | Up to five tries, with the same wait each time or a longer one each time |
| Do something when it breaks | A step can run only on failure, or whatever happened |
| Stop and ask a person | The run waits, shows your question, and carries on when somebody says so |
| Ask an assistant one question | Part way through, with the answer kept beside the run |
| Run one of your other pipelines | As a single step, instead of copying its steps |
| Ask you a couple of questions at the start | So one saved pipeline covers the quick run and the full one |
| Run less than all of it | One step on its own, or from the step that broke onwards |
| Keep what you had | Every save keeps the version before it, and any can be put back |
| Show you what happened | Every box lights up as the work reaches it, and a timeline names the slow step |
| Start from a ready-made one | Ten of them, with a search box |
| Run itself on a timer | Every night, every weekday morning, every hour, with nobody watching |

Not to be confused with the **Workflow** tab, which is about how the assistants
work on one change - who plans, who writes, who reviews. This tab is about jobs
on this machine.

A check suite answers one question: does this project work? A pipeline answers a
bigger one. Run these suites at the same time, scan the code for credentials,
only carry on if that went well enough, then run the unit tests, and try the
flaky one again before giving up.

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

## Telling one step from another

Every step can be told four things beyond what it does. The dialog on the step
has all of them.

**Try again this many times if it fails**, and **how long to wait** between
tries. For anything that fails because something else was briefly busy.

**When this step runs.** The usual answer is "when all is well" — anything before
it failed and this is skipped. The other is "when something failed", which is how
you get a step that only ever runs to tell somebody it broke.

**Give up on this step after N seconds.** New. A step with nothing to say for
itself otherwise holds the whole run up until the run's own limit runs out, which
on a long automation is the rest of the afternoon — and whoever set it going is
not watching. 0 means no limit of its own.

**Let the rest carry on even if this one fails.** New. For the steps that are a
nice-to-have rather than the point: posting a note, tidying up afterwards. One of
those failing should not throw away work that already passed, and a step marked
this way is not counted against the run either.

Both new ones are shown on the step itself, so you can see them without opening
it. Something you can only see by opening it is something nobody checks.

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
Its instruction field accepts up to 200,000 characters. Nexus sends an accepted
instruction exactly, including leading indentation, trailing spaces, and line
breaks; an over-boundary value is rejected visibly and never shortened.

What it writes goes into `.harness/pipelines/drafts`, and nowhere else. That is
deliberate. Writing straight into `tests/` was the obvious thing to do and it
was wrong: `tests/` is exactly where every test runner goes looking, so a
pipeline could ask a model for a "test" and have the very next step run
whatever came back. A draft sits where nothing runs it until you have read it
and moved it yourself.

The step uses the selected provider's displayed `max_output_tokens` setting; it
does not impose a hidden 4,096-token cap. A draft is saved only when the provider
explicitly reports a successful terminal completion. A length/filter outcome,
missing or unknown completion reason, nonterminal status, or stream that ends
without a completion event saves no partial file. Nexus tells you to raise that
budget, retry the provider, or split the request into several explicit
test-file steps.

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
- A visible **Unsaved changes** status appears as soon as the name, steps,
  connections, positions, or step settings differ from the last saved/opened
  version. Opening another automation, starting a new one, choosing a starter,
  or opening a run snapshot then asks you to **Save and continue**, **Discard
  changes**, or **Cancel**. Cancel keeps the exact drawing open.
- **Import JSON** validates and saves the imported automation into the library
  without replacing the drawing you are editing. Open it from **Your
  automations** when you are ready. **Export JSON** exports the selected saved
  automation.

One pipeline runs at a time. A pipeline starts real suites and real commands,
and two at once would fight over the same project.

---

## When a step runs

Most steps run when everything before them passed. That is what anybody
expects, and it is what you get without touching anything.

Two others exist for the work that is only there because things go wrong.
Pick one in **Settings** on the box.

| Choice | What it does |
| --- | --- |
| When everything before it passed | The usual one. Anything before it failed, and this is skipped. |
| Only when something before it failed | For putting things right, or telling somebody. Skipped when all is well. |
| Whatever happened before it | Runs either way. For the step that writes down what happened. |

A step that only runs on failure does **not** make a good run look bad when it
is skipped, and a step that always runs does **not** make a bad run look good
when it passes. The run says which steps were only there for trouble that
never came.

```json
{"id": "tell-the-team", "kind": "webhook", "label": "Tell the team",
 "settings": {"when": "when-something-failed"}}
```

---

## Waiting before trying again

A step can try more than once. Trying again straight away is the wrong answer
for anything that failed because something else was busy - a port still held, a
service still starting, a file still locked.

| Choice | What it does |
| --- | --- |
| Straight away | No wait. For a step that fails for its own reasons. |
| Wait a few seconds each time | Two seconds before every try. For a test that needs a moment. |
| Wait longer each time | Two seconds, then four, then eight, up to thirty. |

The wait is only offered once the step is set to try more than once, because
with one try there is nothing to wait for. Pressing **Stop** during a wait
stops the run then, not after the wait.

```json
{"id": "tests", "kind": "unit_test", "label": "The tests",
 "settings": {"command_kind": "test", "tries": 3, "wait": "growing-wait"}}
```

---

## How it looked before

Every time you save over a pipeline, the one that was there is kept. Open
**How it looked before** next to the picture to see them, newest first, each
with a line saying what changed.

**Put this one back** puts an older version on the board and saves it. What was
on the board is kept too, so you can swap straight back. Twenty versions are
kept for each pipeline. Deleting a pipeline deletes its old versions with it.

```text
.harness/pipelines/before/my-pipeline.json
```

---

## Asking an assistant mid-run

The **Ask an assistant** step puts one question to an assistant you already pay
for and keeps the answer with the run. "Is the old parser still used anywhere?"
"Which of these two is the real entry point?"

It cannot read files, run commands, or change anything. It is asked one thing
and it answers. The answer's first line shows in the log; the whole answer is
underneath it.

It goes through the same model routes as everything else, so a route nobody set
up is a route it cannot use. Leave **Which assistant** empty for the one this
project already uses.

---

## Running itself

An automation you have to press Run on is half an automation. Open **When it
runs on its own**, pick how often, and it runs itself.

The harness does not sit in the background waiting. Your machine's own
scheduler is asked to run one short command every so often, which is what makes
this survive closing the window and restarting the machine. The harness writes
out the exact line and leaves the running of it to you.

See [ON_A_TIMER.md](ON_A_TIMER.md).
