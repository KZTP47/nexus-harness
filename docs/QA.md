# Checks: the test lab

A check says what to do and what a good result looks like. Checks live in one
JSON file, run side by side, and need no model. A model can propose new ones,
but a proposal only becomes a real check after you accept it.

## Start

```bash
harness qa init
harness qa run
```

`qa init` reads the test, lint, and build commands the harness already found for
your project and turns each one into a check. If it finds none, it writes a
single check that looks for a README, so you have something to edit.

## The suite file

`.harness/qa/suite.json` by default. Change it with `qa.suite` in your config.

```json
{
  "schema_version": 1,
  "name": "default",
  "cases": [
    {
      "id": "unit-tests",
      "title": "The unit tests finish without an error",
      "kind": "command",
      "tags": ["fast", "tests"],
      "command": ["python", "-m", "pytest", "-q"],
      "retries": 1,
      "timeout_seconds": 300,
      "expect": {"exit_code": 0, "stdout_contains": ["passed"]}
    }
  ]
}
```

Every case needs an `id` and a `kind`. Everything else has a sensible default.
An id is lowercase letters, digits, dash, or underscore, and no two cases may
share one. Tags let you run part of the suite. `touches` says what a check
changes while it runs, so two checks that change the same thing wait for each
other instead of colliding — see [Two checks that change the same
thing](#two-checks-that-change-the-same-thing).

## The six kinds of check

### command

Runs a program and looks at how it finished.

| Field | Meaning |
|---|---|
| `command` | The program and its arguments, one item per argument. There is no shell, so no pipes or wildcards. |
| `cwd` | A folder inside the project. Defaults to the project root. |
| `stdin` | Text handed to the program. |

Its `expect` may use `exit_code`, `max_duration_ms`, `stdout_contains`,
`stdout_not_contains`, `stderr_contains`, and `stderr_not_contains`. With no
`expect` at all, the check means "finish with code 0".

### file

Reads one file in the project.

| Field | Meaning |
|---|---|
| `path` | A path inside the project. A path that tries to leave the project is refused as soon as the suite is read, before anything runs. |

Its `expect` may use `exists`, `contains`, `not_contains`, `min_bytes`, and
`max_bytes`. A check may read the harness' own `.harness` folder. It may never
read anything inside `.git`, because that folder can hold credentials.

### http

Asks a local server a question.

| Field | Meaning |
|---|---|
| `url` | Must start with `http://` or `https://` and name a host in `qa.allow_hosts`. That list holds only loopback addresses to begin with. |
| `method`, `headers`, `body` | The rest of the request. |

Its `expect` may use `status`, `max_duration_ms`, `body_contains`,
`body_not_contains`, `json_fields`, `contract`, and `contract_file`.
`json_fields` reads dotted paths, so `{"data.0.name": "Ada"}` looks at the first
item of a list.

#### Saying what shape the answer must have

`contract` holds an ordinary JSON Schema, and `contract_file` names a file
holding one:

```json
{
  "id": "orders-answer-in-shape",
  "kind": "http",
  "url": "http://127.0.0.1:8080/orders",
  "headers": {"Authorization": "Bearer ${env.TOKEN}"},
  "expect": {
    "status": 200,
    "contract": {
      "type": "object",
      "required": ["orders"],
      "properties": {
        "orders": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["id", "total"],
            "properties": {"id": {"type": "integer"}, "total": {"type": "number", "minimum": 0}}
          }
        }
      }
    }
  }
}
```

A failure names the exact place: `the answer.orders[2].total must be number, but
it holds "12.50", which is string`.

Two rules keep this honest:

- A word in the contract that the harness cannot enforce is refused when the
  suite is read, naming the word. Nothing is quietly skipped, because a checker
  that skips rules while reporting success is worse than no checker.
- A `$ref` may only point inside the same contract. Nothing is ever fetched, so
  reading a contract never reaches the network.

If `contract_file` names a file that is missing or broken, the check fails. It
did not check anything, so it must not pass.

Tokens belong in named settings rather than in the suite file: put
`"Authorization": "Bearer ${env.TOKEN}"` in the headers and run with
`--environment staging`.

A redirect is treated as the final answer, because following it could leave the
allowed host.

### browser

Opens a real browser page and watches what happens.

| Field | Meaning |
|---|---|
| `url` | The site to open. The host rule is the same as for `http`. |
| `routes` | Paths to visit, such as `["/", "/about"]`. Defaults to `["/"]`. |
| `viewport` | `{"width": 1280, "height": 800}`. |
| `click_all` | Press every button on the page, reloading between presses. Submit buttons are left alone. |
| `check_accessibility` | Look for missing alt text, unlabelled form fields, buttons with no readable name, skipped heading levels, and a page with no stated language. |
| `steps` | A written-down user workflow. See below. |

Its `expect` may use `max_console_errors`, `max_page_errors`,
`max_failed_requests`, `max_accessibility_problems`, `max_duration_ms`,
`body_contains`, and `body_not_contains`. With no `expect`, it means "no browser
console errors and no page script errors".

The accessibility audit looks for missing alt text, form fields with no label,
buttons and links with no readable name, skipped heading levels, a page that
does not say which language it is in, and two links that read the same but go to
different places.

#### How fast and how heavy the page may be

| Name | Meaning |
|---|---|
| `max_load_ms` | Milliseconds until the page finished loading |
| `max_first_paint_ms` | Milliseconds until the page first showed anything |
| `max_requests` | How many files the page may ask for |
| `max_transfer_bytes` | How many bytes may come down the wire |

```json
{"expect": {"max_load_ms": 2000, "max_first_paint_ms": 1200, "max_requests": 40}}
```

Nothing is measured unless a case asks for it. When it does, a page the browser
would not measure is a failure, with a sentence saying so, and never a pass. The
older tool asked the browser a question the browser no longer answers, read the
zeros as a fast page, and reported every page as being inside its budget.

The same care applies to bytes: if the page pulled files from somewhere that
refuses to say how big they were, the check says the total cannot be judged
rather than reporting a number it knows is too low.

A browser check needs Node.js and Playwright:

```bash
npm install playwright
npx playwright install chromium
```

Without them the check is reported as skipped, with that instruction as the
reason. A skipped check never fails the run.

### visual

Takes a picture of the page and compares it with a picture you saved earlier.
This catches the changes no other check sees: a button that moved, a color that
went wrong, a panel that collapsed.

| Field | Meaning |
|---|---|
| `url` | The site to open. The host rule is the same as for `http`. |
| `routes` | One path, such as `["/pricing"]`. Defaults to `["/"]`. |
| `viewport` | `{"width": 1280, "height": 800}`. Change it and the picture changes, so keep it fixed. |
| `steps` | The same written-down actions as a browser check. The picture is taken after they all worked. |
| `selector` | Photograph one part of the page. It must match exactly one thing, or the check fails and says how many it found. |
| `full_page` | Photograph the whole page, including what you have to scroll to. Cannot be used together with `selector`. |
| `baseline` | Where the saved picture lives. It defaults to `.harness/qa/baselines/<check id>.png`. |

Its `expect` may use:

| Name | Meaning |
|---|---|
| `max_changed_percent` | How much of the picture may look different, from 0 to 100. It is already a percentage: `1` means one percent. |
| `max_changed_pixels` | A plain count of pixels instead of a share. |
| `allowed_color_drift` | How far one color value may move, from 0 to 255, before it counts. Use a small number like `2` when a page draws itself very slightly differently each time. |
| `max_duration_ms` | The usual time limit. |

With no `expect`, nothing may change at all.

The first run has nothing to compare with, so the check is skipped and tells you
what to run. Look at the page yourself first, then save it:

```bash
harness qa baseline                     # save a picture for every visual check
harness qa baseline --case home-page    # or just one
```

The saved picture is what every later run is judged against, so only save one
when the page is right.

When a check fails, the run folder holds `attempt-1-now.png`, the picture that
was just taken, and `attempt-1-difference.png`, where everything that moved is
marked in red and anything only one of the two pictures had is marked in pink.

Two things this check is strict about, because they are easy to get wrong:

- A page that changed size has changed, and is always reported, however
  generous the allowed share is.
- How see-through a pixel is counts as part of its color. A box that faded out
  is a change.

### secrets

Reads the project's own files and looks for credentials somebody left in the
code.

| Field | Meaning |
|---|---|
| `paths` | File patterns to read, such as `["src/**/*.py"]`. Defaults to everything. |
| `skip` | Patterns to leave alone, such as `["tests/fixtures/*"]`. |

Its `expect` may use `max_findings`, which defaults to zero.

```json
{"id": "no-keys-in-the-code", "kind": "secrets", "paths": ["src/**/*", "*.md"]}
```

It knows OpenAI, GitHub, Slack, Amazon and Google keys, private key files,
signed web tokens, passwords written straight into the code, and addresses with
a password inside them. A line that plainly shows somebody where to put their
own key, such as `os.environ["OPENAI_API_KEY"]` or `"your-key-here"`, is left
alone.

`.git`, `node_modules`, build folders, pictures and programs are never read.

A line you want to keep can be marked with `harness: allow secret` in a comment.
Marked lines are still counted and still listed, so nothing hides.

**Reading no files fails.** If the patterns match nothing, the check reports
that nothing was checked and fails. This is the whole point of the kind: the
older tool's security gate reported success whenever its scanner was missing or
its file list came back empty, so a project with keys in it looked clean for
months.

Nothing found is ever printed in full. The report gives the file, the line, and
what kind of thing it was, with the value itself taken out.

## Writing a user workflow

`steps` describes what a person would do, in order. Each step names one action.

```json
{
  "id": "sign-in-works",
  "kind": "browser",
  "url": "http://127.0.0.1:8765/",
  "steps": [
    {"do": "click", "target": "[data-view=\"checks\"]", "note": "Open the checks tab"},
    {"do": "expect_visible", "target": "#checksView"},
    {"do": "type", "target": "#search", "text": "readme"},
    {"do": "press", "target": "#search", "key": "Enter"},
    {"do": "expect_text", "target": "#results", "text": "README.md"}
  ]
}
```

| Action | Needs | What it does |
|---|---|---|
| `click` | `target` | Presses the first thing that matches. |
| `type` | `target`, `text` | Puts text in a box. An empty text clears it. |
| `press` | `target`, `key` | Sends one key, such as `Enter` or `Control+ArrowRight`. |
| `choose` | `target`, `value` | Picks an option in a dropdown. |
| `expect_text` | `target`, `text` | Waits until the thing holds that text. Boxes are read by their value. |
| `expect_visible` | `target` | Waits until the thing is on screen. |
| `expect_hidden` | `target` | Waits until the thing is gone. |
| `wait` | `ms` | Waits a fixed time. Use this last, when nothing better fits. |
| `expect_count` | `target`, `count` | Waits until exactly that many things match. |
| `run` | `script` | Runs a small piece of JavaScript in the page and checks what it gives back. |

`expect_count` is for "there should be three rows":

```json
{"do": "expect_count", "target": "#results tr", "count": 3, "note": "Three rows came back"}
```

`run` is the way out when no other step fits. The snippet runs inside the page,
exactly as if you had typed it into the browser's own console, and what it gives
back is compared with `text`:

```json
{"do": "run", "script": "return document.title", "text": "Shop", "note": "The page is the shop"}
```

With no `text`, the step passes when the snippet gives back anything true.

**A suite file already runs whatever it says.** A `command` check runs a program,
and a `run` step runs a piece of script in the page. Read a suite file you did
not write before you run it, the same as you would read a build file or a script
somebody sent you.

`target` is a CSS selector. Every step may set `note`, which is the name used in
the report, and `timeout_ms`, which defaults to ten seconds.

## Getting your first check without writing one

Three ways in, from least typing to most.

**Record what you do.** The fastest one:

```bash
harness qa record --url http://127.0.0.1:8765/
```

A browser opens with a small bar at the top. Do the thing you want to check:
click, type, choose, press Enter. Press Done, and the steps are written into
your suite as a check you can run.

What you type in a password box is never written down. The step asks for a
saved setting instead, so the check reads `${env.PASSWORD}` and you put the real
value in once with `harness qa env set`.

If a thing you clicked has no name of its own, that step is left out and the
reason is printed, rather than a name being invented that would break tomorrow.

**Take one off the shelf.**

```bash
harness qa starters                       # see what there is
harness qa add page-opens --url http://127.0.0.1:8080/
```

Twelve ready-made checks: a page opens with no errors, signing in works, a form
refuses to be sent empty, a page loads quickly enough, a page still looks the
same, an answer keeps its shape, a private address refuses a stranger, the same
check over a table, the project's own tests, no credentials in the code, every
page of the site opens, and the page works at phone size. Each one says what it
does and what to change.

If you give a plain address, the ready-made check keeps its own path: asking for
`http://127.0.0.1:8080/` on the "answer keeps its shape" check gives you
`http://127.0.0.1:8080/api/health`.

**Ask a model.** `harness qa generate` proposes checks for your project. Nothing
is added until you run `harness qa accept`.

Take any check out again with `harness qa remove <name>`, or the Remove button
next to it in the panel.

## Made-up data to run a check against

```bash
harness qa fake --rows 20 --column name --column email --column password --output people.csv
```

The same seed always gives the same table, so a check that fails can be looked
at again. Known column names get sensible values: name, first_name, last_name,
email, password, phone, city, number, word, id, date. A made-up password always
says "example" in it, so it is never mistaken for a real one, by a person or by
the credential scan.

Use the file with `"rows_file": "people.csv"`, and `${row.email}` in the check.

## Seeing the page at the moment it went wrong

A browser check keeps a picture of the page when a step fails, and the report
says which one:

```
Step 2 of 5 did not work: Press Buy. The browser said: ...
A picture of the page is in the run folder: step-02-went-wrong.png
```

The pictures sit with the rest of the evidence under `.harness/qa/runs`, and the
panel shows them under the failed check. To keep one for every step, not only
the one that failed:

```json
{"id": "sign-in", "kind": "browser", "pictures": "every_step", "steps": [...]}
```

`pictures` may be `failure`, which is the default, `every_step`, or `never`.

## What changed since last time

A list of results tells you how things are now. Most mornings the useful
question is what is different:

```bash
harness qa changed
```

It reads the last two kept runs and says only what moved: what started failing,
what got fixed, what is new, what went away, and what got a lot slower. A check
that has been failing all week is not news, so it is mentioned at the end and
not at the top.

You can also name two kept runs yourself:

```bash
harness qa changed --before 20260101-120000 --after 20260101-130000
```

Only the folder name of a kept run is accepted, so nothing outside your project
can be read this way.

## A step that runs whatever happens

Once a step fails, the rest are skipped: the workflow is broken, so checking
the rest of it proves nothing. That is right for most steps and wrong for one
kind — the step that puts back whatever the check changed.

```json
{"do": "run", "script": "...", "text": "put back", "always": true,
 "note": "Leave the project as we found it"}
```

A step marked `always` runs even when an earlier step failed. Use it whenever a
check writes something, signs in, or changes a setting. Without it, the one
thing that must happen after a failure is the one thing that never does.

If a tidy-up step fails, the report names it as one, because that means
something the check changed has been left changed.

## Two checks that change the same thing

Checks run several at a time. That is what keeps a big suite quick, and it is
also how a check that writes something ends up standing on another one's work:
one empties a folder while the other is counting what is in it, and the run
fails for a reason that has nothing to do with your code.

Say what a check changes, in plain words:

```json
{"id": "notes-can-be-written", "kind": "browser", "touches": ["the vault"]}
```

Two checks that name the same thing never run at the same time. Everything else
carries on running together, so this costs nothing where it is not needed. The
words are yours to choose — `the vault`, `the settings file`, `the test
database` — they only have to match between the checks that share the thing.

A check that fails one run in three, and passes on its own every time, is
almost always two checks sharing something. This is the fix.

## Which pages nobody checks

Your list of checks says what is watched. It does not say what is not, and that
is the part that bites. This walks your site the way a visitor would, follows
the links, and lines the pages up against your checks:

```bash
harness qa coverage --url http://127.0.0.1:8000/
```

Every page comes back in one of three groups:

| Group | What it means |
| --- | --- |
| checked | A check opens this exact page. |
| only walked over | A walk goes past it, so a broken page would show, but nothing you asked for is checked. |
| nobody looks at it | No check ever opens it. |

It ends with the percentage of pages that have a check of their own. To close
the gap in one go:

```bash
harness qa coverage --url http://127.0.0.1:8000/ --write-missing
```

That writes a plain "the page opens" check for every page nobody looks at, ready
for you to add steps to. Two pages that want the same short name both get a
check; the second one gets a number.

Useful extras: `--max-pages` for how far to walk (40 by default, 500 at most),
`--stay-under` to keep the walk inside one part of the site, and `--json` for
the whole answer.

In the panel this is the **Find pages nobody checks** button in the Checks view.
It draws one small block per page, green for checked, yellow for only walked
over, red for nobody looks at it, so the gap is something you see rather than
count. The button underneath writes the missing checks for you.

## What is taken out of anything you send on

Why a check failed is a program's own output, and a program prints whatever it
was given, keys included. So credentials are taken out of everything the harness
writes for a person to read or pass on:

| What | Cleaned |
| --- | --- |
| `qa run --format markdown`, `--format junit`, `--format html` | Yes |
| `qa share` (the one file, and the answer behind it) | Yes |
| `qa changed`, on screen and as JSON, and the panel's box | Yes |
| `qa explain`, before anything reaches a model | Yes |
| `harness bundle` | Yes |
| Everything the panel is told while a run is going | Yes |
| `qa run --format json`, and the run folder itself | No, on purpose |

The panel's live feed is on that list for a reason worth saying plainly: it is
not a second copy of the run folder. It arrives while the run is still going,
before anything has been written down, so it is usually the first place a
check's output exists at all. It goes onto a screen that gets shared or
recorded, and it stays in the page afterwards.

The last row is this machine's own record, like a log file. Hiding things in one
copy while the other still holds them would only give false comfort, so the
record is left as it is and every copy that leaves this machine is cleaned. If
you need the untouched output of a failing check, look in the run folder.

## One file you can send to anyone

A run leaves a folder. You cannot email a folder, and the person you send it to
should not have to install anything to read it.

```bash
harness qa share
```

That writes one web page next to the run, with the screenshots inside the file
itself. Open it anywhere, send it to anyone. Credentials are taken out before
anything is written, your own folder name is taken out with them, and the colour
codes a browser puts in its messages are stripped so the text reads plainly.

| Option | What it does |
| --- | --- |
| `--run 20260101-120000` | A particular kept run instead of the most recent. |
| `--output report.html` | Where to write it. |
| `--no-pictures` | Leave the screenshots out to keep the file small. |

Pictures over 3 MB are left out, and so is anything past 20 MB in total or past
60 pictures. Whatever is left out is named at the bottom of the page, so nothing
disappears quietly.

In the panel this is the **Make one file I can send** button in the Checks view.

## When a check fails and you do not know why

```bash
harness qa explain                 # the first failure of the last run
harness qa explain --case sign-in  # a particular one
harness qa explain --dry-run       # see the question without asking anything
```

It sends the check, what it reported, and what it saw to the model this project
is already set up with, and asks for three short parts: what went wrong, the
likely cause, and one thing to try. The answer is advice. Nothing is changed by
asking.

Credentials are taken out of the question before it leaves this machine, using
the same remover the rest of the harness uses. The panel has the same thing as
an "Ask why this failed" button beside every failed check.

## Handing the checks to a build server

```bash
harness qa ci github
harness qa ci gitlab --suite .harness/qa/nightly.json
```

That writes the file the build server needs, set up to run your checks on every
change, install a browser for the ones that need it, and keep the report. Look
at it and change what you like; it is a starting point, not a rule.

## Pointing at a thing instead of guessing its name

Naming the thing you mean is where most people get stuck. So let the harness
name it for you:

```bash
harness qa pick --url http://127.0.0.1:8765/
```

A browser window opens. Click the thing you want to check, and the names come
back best first, ready to paste into a step. Press Escape to give up.

Only a name that matches exactly one thing on the page is offered. A name that
matched nothing, or matched several things, is listed separately with the count,
so you can see why it was not good enough. That matters: a check built on a name
that matches six buttons passes or fails on whichever one the browser reaches
first, and nobody can tell which.

The order the names come in:

| Best first | Why |
|---|---|
| `[data-testid="save"]` | Put there for testing. It survives redesigns. |
| `#save` | The thing's own id. |
| `[aria-label="Save"]` | What the thing is and what it is called out loud. |
| `input[name="email"]` | The name a form field is sent under. |
| `input[placeholder="Your email"]` | The grey hint inside the box. |
| `button:text-is("Save")` | The words on the thing. |
| `button.save` | A style class. It changes when someone restyles the page. |
| `main > div > button:nth-of-type(2)` | Where it sits. It breaks as soon as anything moves. |

An id that looks like one the page invents each time it is built, such as
`mui-4821` or `:r3:`, is still offered but carries a warning, because a check
using it will start failing for no visible reason.

The window is opened by Playwright, so this needs the same Node.js setup as a
browser check, and it may only open a host in `qa.allow_hosts`.

When a step fails, the run stops that case and the report names the step number,
its note, and what the browser said. A case with steps visits one page only.

## Running

```bash
harness qa run                          # every check
harness qa run --tag fast               # only checks with that tag
harness qa run --case unit-tests        # one check by id
harness qa run --workers 8              # how many at a time
harness qa run --format html --output reports/checks.html
```

Report formats are `markdown` (the default when printing), `json`, `junit`, and
`html`. JUnit XML fits build servers. The command ends with code 0 when
everything passed or was skipped, and 1 when anything failed.

Evidence for each attempt goes to `.harness/qa/runs/<run id>/`. The last
`qa.keep_runs` runs are kept and older ones are removed.

## Running again whenever you save

```bash
harness qa watch
harness qa watch --tag fast
```

It runs the checks once, then watches the project. When a file changes it waits
half a second for the changes to settle, prints what moved, and runs the checks
again. Press Ctrl+C to stop.

Only the size and modified time of each file are read, never the contents, and
the same ignore rules the rest of the harness uses decide what is watched, so a
`node_modules` folder or the harness' own `.harness` folder never sets it off.

Watch mode follows 20,000 files at once. A project with more than that is
watched in part, and it says so as soon as it starts: a change to a file past
the limit is never noticed, and a tool that stayed quiet about that would look
exactly like a project where nothing is happening. Narrow what it looks at with
`project.ignore`. It says the same thing if the project moves underneath it
while it is reading, such as a build tool deleting a folder mid-scan.

| Option | Default | Meaning |
|---|---|---|
| `--interval` | `1.0` | Seconds between looks at the project. |
| `--quiet` | `0.5` | Seconds of stillness before a run starts. |
| `--every` | off | Also run this often in seconds, even when nothing changed. |
| `--tag` | none | Only run checks with this tag. May repeat. |
| `--skip-first` | off | Wait for a change before the first run. |
| `--max-runs` | none | Stop after this many runs. |

A save that writes several files in a row counts as one change, so the checks
run once, not once per file.

### Running on a timer

```bash
harness qa watch --every 300
```

That runs the checks whenever you save, and also every five minutes even if
nothing changed. It is useful when a check depends on something outside your
project, such as a server that has to stay up.

This runs only while the command is open. For a schedule that survives a
restart, use the scheduler your computer already has and point it at
`harness qa run`:

- Windows: Task Scheduler, running `harness qa run --format junit --output reports/checks.xml`
- macOS or Linux: a cron line such as `*/15 * * * * cd /path/to/project && harness qa run`

The harness deliberately installs no schedule of its own. Something that starts
your tests when you are not there should be set up by you, where you can see it
and turn it off.

## Retries and unstable checks

`retries` lets a case try again. A case that fails and then passes is reported
as **flaky**, not as a pass, because a check that only works sometimes cannot be
trusted.

Across runs, the harness also watches which checks change their mind:

```bash
harness qa flaky
```

A check is listed when it has run at least `qa.flaky_min_runs` times, has both
passed and failed, and its failure rate sits between `qa.flaky_threshold` and
one minus that number. A check that always fails is broken, not unstable, so it
is left out.

## What to do about your checks

```bash
harness qa advise
```

The harness watches how your checks behave across runs and says which ones need
attention, why, and what to do. It only speaks about a check with enough
history, and it says nothing about a check that is behaving.

| It says | It means |
|---|---|
| has never passed | It failed every recorded run. Fix what it checks, or fix the check. |
| keeps changing its mind | It has both passed and failed. Something differs between runs. |
| never actually runs | It was skipped every time. Install what it needs, or take it out. |
| got a lot slower | It takes at least twice as long as it used to, and at least a second more. |
| has never been run | It is in the suite but has no history yet. |

The same list appears in the Checks tab of the control panel.

## Asking a model for new checks

```bash
harness qa generate --focus "the export command"
harness qa candidates
harness qa accept export-writes-a-file
harness qa reject export-deletes-old-files
```

`generate` sends the project's detected stack, known commands, and existing case
ids to the configured model and reads back proposed cases. Every proposal is
validated in the same way as a hand-written case, then stored in
`.harness/qa/candidates.json`. Nothing runs until you accept it.

Each proposal carries warnings in plain words, for example when a command starts
with `rm`, when it holds `--force` or `install`, when it calls a site that is not
on this machine, or when the case checks nothing at all.

## Adding your own kind of check

A plugin can add a kind of check, such as one that asks a database a question.
Once it is turned on, that kind behaves exactly like a built-in one: it runs
side by side with the rest, retries, reports, and appears in the control panel.

See [PLUGINS.md](PLUGINS.md) and the working example in
`examples/plugins/sqlite_check.py`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `qa.suite` | `.harness/qa/suite.json` | Where the checks live. |
| `qa.workers` | `4` | How many checks run at the same time. |
| `qa.default_timeout_seconds` | `120` | Used when a case sets no timeout. |
| `qa.artifacts_dir` | `.harness/qa/runs` | Where evidence is written. |
| `qa.keep_runs` | `20` | How many past runs to keep. |
| `qa.max_evidence_chars` | `4000` | How much output the report shows. |
| `qa.max_response_bytes` | `1000000` | How much of an http answer is read. |
| `qa.allow_hosts` | loopback only | Hosts an http or browser check may call. |
| `qa.flaky_min_runs` | `5` | Runs needed before a check can be called unstable. |
| `qa.flaky_threshold` | `0.2` | How far from always-passing or always-failing counts as unstable. |

A checked-in project config may only name loopback hosts in `qa.allow_hosts`.
Naming any other host needs your own trusted local config, so a shared repository
cannot point your machine at a site you did not choose.
