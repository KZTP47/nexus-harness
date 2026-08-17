# Nexus Harness

A local test lab and coding assistant for your project. It writes and runs your
checks, tells you plainly what broke, and can ask a model to fix it — all on
your own machine.

Python 3.11 or newer. The core uses only the standard library: no packages to
install, no account to create, nothing sent anywhere unless you set that up
yourself.

![The checks view, with every check passing](docs/images/checks.png)

---

## What it does

**Writes your tests for you.** Point it at a page and click the thing you want
to check. Record yourself using the site once and get a test written from it.
Or pick from twelve ready-made checks and change one line.

**Runs them and says what happened in plain words.** Not a stack trace: "Step 2
of 5 did not work: the Sign in button. The browser said it was still hidden
after 10 seconds," and a picture of the page at that moment.

**Tells you what changed since last time.** A check that has failed all week is
not news. A check that passed yesterday and fails today is the whole story.

**Finds the pages nobody checks.** It walks your site the way a visitor would
and colours in every page: checked, only walked over, or nobody looks at it.

**Packs the evidence up.** One web page with the screenshots inside it that you
can send to anyone — no install needed to read it. Credentials are taken out
first.

---

## Install

Python 3.11+ must already be on the machine. Nothing else is needed.

```bash
git clone https://github.com/KZTP47/nexus-harness.git
cd nexus-harness
python -m pip install .
```

Or build a single self-contained file and a launcher, with no pip and no
network:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

```bash
sh ./scripts/install.sh
```

Browser and screenshot checks additionally need Node.js and Playwright. Every
other kind works without them, and a browser check says so plainly instead of
failing when they are missing:

```bash
npm install playwright
npx playwright install chromium
```

---

## One command

```bash
cd your-project
harness start
```

That reads your project, writes a starter suite from the commands you already
use, says what is still missing, and opens the panel. Press **Show me around**
on the first screen and it walks you through the rest.

The same thing, a step at a time, if you would rather:

```bash
harness qa init        # writes a starter suite from the commands you already use
harness qa run         # runs them side by side and reports
harness ui             # open the control panel in your browser
```

![The guided start view](docs/images/start.png)

---

## See what the harness actually does

The first screen draws the whole workflow as a picture, in plain words. Press
**Show me how it works** and it walks through the steps one at a time. While a
real run is going, the same boxes light up as the work reaches them.

![What happens when you ask for a change](docs/images/how-it-works.png)

Every box is one agent or one check from the Workflow tab. Rewire it there and
this picture changes with it, because it is drawn from the workflow that will
really run — not from a drawing somebody has to remember to update.

---

## What it knows about you

A harness that runs against the same project every day learns things: how you
like to be answered, which command really runs the tests here, what went wrong
last time and what fixed it. Kept in a database, that is the harness's private
business. Kept as notes, it is yours.

![What the harness has learned](docs/images/what-it-knows.png)

Every note is one markdown file in `.harness/vault`, with a few lines at the
top and links written `[[like this]]`. Open the folder in any editor and it is
a set of notes about your project. Nothing needs the harness to read it.

| Kind of note | What it holds |
| --- | --- |
| About you | How you like to be worked with. |
| How to | Something that worked, written down so it can be done again. |
| About this project | What the harness has worked out about the code. |
| Lesson | Something that went wrong once, and what fixed it. |

The picture is the point. A circle is a note, a line is a link, colour says
which kind, and size says how connected and how used it is. A note nothing has
touched for months is dimmed rather than believed for ever, and a link to a
note nobody has written yet is drawn as a dashed outline you can press to write
it.

**How a note earns its place.** Open one and say whether it helped. A note that
is used and goes well grows and rises; one that does not fade. That is the
whole loop: the harness writes down what worked, you correct what did not, and
what is left is true.

**Learn from the runs** reads what the harness already remembers and writes the
parts worth keeping as notes. It never writes over a note you have edited.

---

## Pipelines: many jobs, wired together

A check suite answers "does this project work?". A pipeline answers a bigger
question: run these suites side by side, scan the code for credentials, only go
on if that passed, then run the unit tests, and try the flaky one again.

![The pipelines board](docs/images/pipelines.png)

Drag steps out of the list on the left, press **Connect** on one box and then
another to join them, and press the small cross on an arrow to cut it. Each box
lights up as the run reaches it: blue while it works, green when it passed, red
when it did not, and dim when a gate stopped the work before it got there.

| Step | What it does |
| --- | --- |
| Start | Where a run begins. Everything it points at starts together. |
| Test suite | Runs your checks, or only the ones carrying a tag. |
| Unit test | Runs the project's own test, lint, or build command. |
| Security scan | Reads your files for credentials left in them. |
| Security gate | Lets the work go on only if the scans before it went well enough. |
| Gate | The same, for anything: all of what came before, or any of it. |
| Git repo | Reads which branch you are on and what is uncommitted. It never writes. |
| AI drafts a test | Asks the model you set up to write a test, and saves it as a draft for you to read. Nothing runs a draft where it is kept. |
| Keep the evidence | Writes what happened into one file you can send to somebody. |

Any step can be told to try again up to five times before it gives up, which is
usually enough for the one test that fails on a slow morning.

A pipeline is ordinary JSON in `.harness/pipelines`, so it can go into your
repository and everyone gets the same one. There is deliberately no "run this
shell line" step: a saved pipeline is a file people pass around, and a file
that can run anything is a file nobody should open.

---

## "I don't care, just do it for me"

Connecting a model is a short list of instructions, and a short list is still
work if you have never done it. Every way of connecting one has a button that
does the list for you.

![The do it for me button on a service that needs a key](docs/images/just-do-it.png)

It will start Ollama if it is installed but not running, fetch the model, write
the route into your own settings file, and trust that file. It will not install
software, make an account, or ask you for a key — so it says which single part
is left for you, and where to do it. A key is never typed into the page and
never written into a settings file.

---

## Change any setting without opening a file

Everything the harness can be told, in plain words: what it is set to now,
which file that came from, and what it shipped as. Type a new value and press
Save. A setting that only counts from your own file goes there by itself, and
anything the harness would refuse is put straight back with the reason.

```bash
harness ui             # then open Settings
```

There is no list of key names to learn and no JSON to edit. A command can be
typed the way you would type it in a terminal: `pytest -q`, not
`[["pytest", "-q"]]`.

---

## When a check fails

Every failing check has a **What does this mean?** button. It turns the error
into a sentence and a short list of things to try:

```text
Nothing was listening at that address.
The check asked a server on this machine for a page, and no server answered.

Worth trying:
  - Start the thing being checked, then run the check again.
  - Look at the address in the check: a different port is the usual reason.
  - If it is the harness's own panel, run: harness ui
```

If it does not recognise a failure it says so, rather than guessing. A
confident wrong answer sends you looking in the wrong place for an hour.

---

## Carrying a setup to another machine

```bash
harness carry pack                       # writes harness-setup.json
harness carry unpack harness-setup.json  # on the other machine
```

Your checks, your pipelines and the shared settings travel. Your own settings
file never does: it names the tools on your machine, the addresses you call,
and the variables holding your keys. Nothing already on the other machine is
written over unless you say so.

---

## The seven kinds of check

| Kind | What it does |
| --- | --- |
| `command` | Runs a program and looks at how it finished. |
| `file` | Reads a file in the project and checks what is in it. |
| `http` | Asks a local server a question and checks the answer, including its shape against a JSON Schema. |
| `browser` | Opens a real page, walks through a written workflow, and watches for errors. |
| `visual` | Takes a picture of a page and compares it with one you saved. |
| `secrets` | Reads your own files and looks for credentials left in them. |
| `crawl` | Follows every link from one page and reports what is broken. |

Plugins can add their own kinds. See [docs/PLUGINS.md](docs/PLUGINS.md).

A check is ordinary JSON, so it reads like something a person wrote:

```json
{
  "id": "sign-in-works",
  "title": "A person can sign in",
  "kind": "browser",
  "url": "http://127.0.0.1:8000/",
  "steps": [
    {"do": "type", "target": "#email", "text": "someone@example.com"},
    {"do": "type", "target": "#password", "text": "example"},
    {"do": "click", "target": "#sign-in", "note": "Press sign in"},
    {"do": "expect_text", "target": "#welcome", "text": "Welcome back"}
  ],
  "expect": {"max_console_errors": 0}
}
```

---

## Which pages nobody checks

```bash
harness qa coverage --url http://127.0.0.1:8000/ --write-missing
```

It walks the site, sorts every page into checked, only walked over, or nobody
looks at it, and can write a check for each page in the last group.

![The coverage view](docs/images/coverage.png)

---

## What changed since the last run

```bash
harness qa changed
```

Only what moved: what started failing, what got fixed, what is new, what went
away, and what got a lot slower. A check that was already failing is mentioned
at the end, not the top.

---

## One file you can send to anyone

```bash
harness qa share
```

One web page holding the results and the screenshots, openable on a machine
that has never seen this project. Credentials, your own folder names, and
terminal colour codes are all taken out first.

---

## Asking a model to make a change

This part is optional and off until you set up a model.

```bash
harness doctor                      # says what is missing and how to fix it
harness run "Fix the failing parser test"
```

The harness plans the change, edits the files, runs your checks, reviews the
result, and tries a bounded number of repairs. Files are restored if a run
fails. `--dry-run` plans without touching anything.

You can rewire who does what, and in which order:

![The workflow view](docs/images/workflow.png)

Model services it can use: Ollama or any OpenAI-compatible server on your own
machine; OpenAI, Anthropic or Gemini with your own key; or the Claude and
GitHub Copilot command lines, which use a seat your organisation already pays
for and need no key at all. See [docs/SUBSCRIPTIONS.md](docs/SUBSCRIPTIONS.md).

---

## Worked example: two subscriptions on one job

Plenty of organisations have Claude and Copilot seats and no API keys, and never
will. Both of those ship a command line tool that is already signed in, so the
harness can put them on the same job: Claude plans and reviews, Copilot writes
the code, and the two send notes to each other as they go.

### The short way: let it set itself up

Open `harness ui`, stay on the Start view, and work down the three steps.

![Setting up the assistants you already pay for](docs/images/seats.png)

1. **Find the assistants.** It looks for each tool on this machine, asks its
   version, and says which ones are ready. If one is missing it tells you what
   to install.
2. **Write the settings and trust them.** One button writes a route per
   assistant into your own settings file and marks the file as yours. It shows
   you exactly what it wrote, and **Put my settings back** undoes it.
3. **Share the work out.** One button gives each agent in the workflow on
   screen a seat, lets them send notes to each other, and — when you have two
   assistants — puts the reviewer on the other one from the coder.

Same thing without the screen:

```bash
harness seats list      # what is on this machine
harness seats setup     # write the routes and trust the file
```

### The long way: do it by hand

Useful when your tools take different arguments, or you want to see every part.

**1. Check both tools are installed and signed in.** They are separate products
from the subscriptions, and each signs in on its own.

```bash
claude --version        # 2.1.101 (Claude Code)
copilot --version
```

**2. Name one route per seat** in `.harness/config.local.json` — your own file,
never shared:

```json
{
  "providers": {
    "claude":  {"kind": "claude-cli",  "model": "claude-sonnet-4-5", "endpoint": ""},
    "copilot": {"kind": "copilot-cli", "model": "gpt-5",             "endpoint": ""}
  },
  "provider": {"name": "claude-cli", "model": "claude-sonnet-4-5",
               "endpoint": "", "api_key_env": ""}
}
```

`endpoint` stays empty: a signed-in tool has no address to call.

**3. Say the file is yours.** Until you do, the harness refuses to use it:

```text
error: providers.claude.kind requires trusted local, user, environment,
explicit, or command-line config
```

```bash
harness trust
harness doctor          # OK provider: Provider configuration is present: claude-cli
```

**4. Give each agent a seat.** Open `harness ui`, go to the Workflow tab, and
set the **Provider route** on each agent box:

| Agent | Route |
| --- | --- |
| Planner | `claude` |
| Coder | `copilot` |
| Final Reviewer | `claude` |

A reviewer on a different assistant from the coder is the whole point. Two
models that share no training tend not to share the same blind spot.

**5. Let them talk.** Tick `team.message` on each agent. Then one can tell
another what it found:

```json
{"to": "coder", "subject": "The parser caches by file name",
 "body": "Two files with the same name share a cache slot. Key on the full path."}
```

**6. Press Start run.** That runs the workflow you have on screen. The finished
setup routes like this:

```text
planner  -> route claude   = claude-cli claude-sonnet-4-5
coder    -> route copilot  = copilot-cli gpt-5
review   -> route claude   = claude-cli claude-sonnet-4-5
```

Subscription work is recorded as `subscription-unpriced` — there is no price
per request, so the harness does not invent one. Your plan's own rate limits
still apply.

Full walkthrough, including what to do when a tool takes different arguments:
[docs/TWO_SEATS.md](docs/TWO_SEATS.md).

---

## Running in a build server

```bash
harness qa ci github          # writes the workflow file
harness qa ci gitlab
harness qa run --format junit --output report.xml
```

Reports come out as JSON, Markdown, JUnit XML, or a single HTML page.

---

## Every command

```text
harness start                       Set a project up and open the panel, in one go
harness init                        Scan a project and write its settings
harness doctor                      Say what is missing and how to fix it
harness run <task>                  Plan, edit, test, review, repair
harness ui                          Open the control panel
harness qa init                     Write a starter suite from your own commands
harness qa run [--tag|--case ...]    Run the checks side by side
harness qa watch                    Run them again whenever a file changes
harness qa coverage --url <address> Which pages have no check at all
harness qa changed                  What moved since the run before
harness qa share                    One page of a run you can send to anyone
harness carry pack                  Pack this setup up to carry to another machine
harness carry unpack <file>         Write a carried setup into this project
harness qa record --url <address>   Do a workflow by hand and get a check from it
harness qa pick --url <address>     Click a thing and get a name a check can use
harness qa starters | add <name>    Ready-made checks
harness qa baseline                 Save today's screenshots as the ones to match
harness qa explain                  Ask your model why a check failed
harness qa flaky | advise           Which checks need attention, and why
harness qa ci github|gitlab         Write the file a build server needs
harness bundle                      Zip the evidence to send on
```

`harness <command> --help` explains any of them.

---

## What it will not do

It will not run a command your settings deny, and the deny list has a floor it
cannot be talked out of: nothing formats a disk or shuts down a machine, whatever
a project's own settings say.

It will not read or write outside your project folder.

It will not send anything anywhere until you configure a model, and it takes
credentials out of everything it writes for a person to read or pass on.

Project settings that could run something — provider commands, test commands,
plugins — are refused unless the file they come from is trusted on this machine.
Cloning a repository never gives that repository the right to run code.

---

## Documentation

| Guide | About |
| --- | --- |
| [QA.md](docs/QA.md) | Checks, in full: every kind, every option, every command |
| [PIPELINES.md](docs/PIPELINES.md) | Wiring many jobs together, with gates between them |
| [WHAT_IT_KNOWS.md](docs/WHAT_IT_KNOWS.md) | The notes the harness keeps about you and your project |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting and where it may come from |
| [SECURITY.md](docs/SECURITY.md) | What is fenced off, and how |
| [SUBSCRIPTIONS.md](docs/SUBSCRIPTIONS.md) | Using a Claude or Copilot seat instead of a key |
| [TWO_SEATS.md](docs/TWO_SEATS.md) | Two subscriptions on one job, step by step |
| [CONTROL_PANEL.md](docs/CONTROL_PANEL.md) | The panel and the workflow editor |
| [DESKTOP.md](docs/DESKTOP.md) | The desktop app |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the parts fit together |
| [PLUGINS.md](docs/PLUGINS.md) | Adding your own kind of check |
| [ACCESSIBILITY.md](docs/ACCESSIBILITY.md) | Keyboard and screen reader support |

---

## Working on the harness itself

```bash
PYTHONPATH=src python -m unittest discover -s tests -t tests -q
```

1324 tests, no test dependencies beyond the standard library. The project also
checks itself with its own tool:

```bash
PYTHONPATH=src python -m our_harness qa run --suite .harness/qa/suite.json
PYTHONPATH=src python -m our_harness qa run --suite .harness/qa/workflows.json
PYTHONPATH=src python -m our_harness audit
```

The second of those is 67 browser checks over the control panel. Three guards
keep the panel honest: every control has to be really used by a check, every
kind of news the server sends has to be one the page listens for, and no check
may lean on data the panel can replace underneath it.

To retake the screenshots in this file:

```bash
python scripts/make_screenshots.py
```

---


