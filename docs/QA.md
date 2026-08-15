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
share one. Tags let you run part of the suite.

## The four kinds of check

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
`body_not_contains`, and `json_fields`. `json_fields` reads dotted paths, so
`{"data.0.name": "Ada"}` looks at the first item of a list.

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

A browser check needs Node.js and Playwright:

```bash
npm install playwright
npx playwright install chromium
```

Without them the check is reported as skipped, with that instruction as the
reason. A skipped check never fails the run.

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

`target` is a CSS selector. Every step may set `note`, which is the name used in
the report, and `timeout_ms`, which defaults to ten seconds.

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

| Option | Default | Meaning |
|---|---|---|
| `--interval` | `1.0` | Seconds between looks at the project. |
| `--quiet` | `0.5` | Seconds of stillness before a run starts. |
| `--tag` | none | Only run cases with this tag. May repeat. |
| `--skip-first` | off | Wait for a change before the first run. |
| `--max-runs` | none | Stop after this many change batches. |

A save that writes several files in a row counts as one change, so the checks
run once, not once per file.

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
