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

## Two minutes to your first check

```bash
cd your-project
harness qa init        # writes a starter suite from the commands you already use
harness qa run         # runs them side by side and reports
```

That is enough to be useful. Everything below is optional.

```bash
harness ui             # open the control panel in your browser
```

![The guided start view](docs/images/start.png)

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
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting and where it may come from |
| [SECURITY.md](docs/SECURITY.md) | What is fenced off, and how |
| [SUBSCRIPTIONS.md](docs/SUBSCRIPTIONS.md) | Using a Claude or Copilot seat instead of a key |
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

## Licence

No licence has been chosen yet, so default copyright applies and nobody else
may reuse this. Add a `LICENSE` file to change that.
