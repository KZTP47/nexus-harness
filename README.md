# Our Harness

Our Harness is a local programming-agent CLI. It scans a project, builds an indexed context, asks a configured model to plan and edit, runs project checks, reviews the result, and performs bounded repair attempts.

The core uses Python 3.11+ and the standard library. Hosted providers and local model servers are optional.

## Fast install

Python 3.11 or newer must already be installed and available to the installer. With that prerequisite in place, a local install normally completes in under 60 seconds and needs no package download, pip, compiler, or network access.

Download or clone this folder, then run one command from its root.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

macOS or Linux:

```bash
sh ./scripts/install.sh
```

The installers build a single `harness.pyz` and place a relative launcher beside its `app` directory. The installed tree can be moved as a unit. Launchers find Python from `PATH` at run time; Windows also accepts the `py` launcher. They do not install Python. On macOS and Linux, `${HARNESS_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/our-harness}/bin` must be on `PATH`; the installer prints it. On Windows, open a new terminal after installation so the updated user `PATH` is visible.

You can also install the Python package:

```bash
python -m pip install .
```

## Start a project

```bash
cd path/to/your/project
harness init
harness doctor
harness run "Fix the failing parser test"
```

`harness init` detects the stack, writes shareable `.harness/config.json`, and puts local provider routes and detected commands in `.harness/config.local.json`. Init records the local file hash in the user config directory. A copied or downloaded `config.local.json` has no executable authority without that out-of-project trust record. Runtime data stays under `.harness/memory`, `.harness/runs`, `.harness/backups`, and `.harness/checkpoints`; the generated `.harness/.gitignore` excludes it.

For non-interactive setup:

```bash
harness init --yes --provider ollama
```

## Provider credentials

| Provider | Credential | Default endpoint |
|---|---|---|
| Ollama | None | `http://127.0.0.1:11434` |
| OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| Anthropic | `ANTHROPIC_API_KEY` | `https://api.anthropic.com/v1` |
| Gemini | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta` |
| Codex CLI (optional named profile) | Existing `codex login` with ChatGPT | Local subprocess |
| OpenAI-compatible | Configured by that server | `http://127.0.0.1:8000/v1` |
| Local process | None unless the process requires one | Configured argv |

The OpenAI adapter calls the OpenAI API and needs an API key. It cannot turn a ChatGPT subscription into an API credential. The separate optional `codex-cli` named profile can reuse an existing local ChatGPT sign-in through `codex exec`. It remains subject to ChatGPT plan, workspace, model, and rate limits. Its usage is recorded as `subscription-unpriced`, with no dollar-cost estimate. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#optional-codex-cli-profile).

Never put keys in `.harness/config.json`. Config stores the environment-variable name, not its value.

## Commands

```text
harness init [path]                 Scan a project and create config
harness run <task>                  Plan, edit, test, review, and repair
harness run --detach <task>         Queue a task in the workspace daemon
harness daemon start|status|stop    Manage the workspace resident process
harness jobs list|status|attach     Inspect resident jobs and durable events
harness jobs cancel|resume <id>     Control a checkpoint-safe resident job
harness jobs message|receipts ...   Queue bounded node-boundary steering
harness test [--lint] [--build]     Run configured project checks
harness qa init                     Write a starter check suite from detected commands
harness qa list                     Show every check in the suite
harness qa run [--tag|--case ...]   Run the checks side by side and report
harness qa flaky                    Name checks whose result keeps changing
harness qa generate|candidates      Ask a model for new checks and read them back
harness qa accept|reject <id> ...   Decide on a proposed check
harness doctor                      Check provider, tools, config, and stack
harness index                       Refresh workspace search and dependency edges
harness memory search <query>       Search prior episodes and indexed files
harness refine list                 Show active reviewed supplemental state
harness refine candidates           Show staged improvement candidates
harness refine review <id> ...      Attach verification and a review verdict
harness refine promote <id>         Activate a reviewed passing candidate
harness refine reject <id> <reason> Reject a candidate explicitly
harness mcp list <server>            List tools from a configured MCP server
harness mcp call <server> <tool>     Call an allowed configured MCP tool
harness graph validate <file>       Validate a workflow graph
harness graph simulate <file>       Run a local graph simulation
harness benchmark                   Run deterministic and optional agentic benchmarks
harness recovery list              List interrupted file transactions
harness recovery rollback <id>     Restore a safely classified transaction
harness recovery finalize <id>     Accept a fully applied transaction
harness runs list                  List resumable durable runs
harness runs show <id>             Inspect one frozen run checkpoint
harness runs resume <id>           Continue from the last completed node boundary
harness runs cancel <id> ...       Cancel and roll back a retained run
harness runs approve <id> ...      Record an explicit approval decision
harness runs reject <id> ...       Record an explicit rejection decision
harness ui                          Open the loopback graph console
harness audit                       Scan the distribution for fixed paths
```

See [docs/RESIDENT_RUNTIME.md](docs/RESIDENT_RUNTIME.md) for the resident process security boundary, crash rules, and mailbox limits.

## Deterministic benchmark

`harness benchmark` runs a versioned deterministic suite against the same file transaction, recovery, context, index, graph, stream, and command APIs used by normal harness workflows. Every case uses a temporary workspace; the evaluator and its expected outcomes remain outside that workspace.

The default JSON result includes the deterministic seed, exact weighted cases, per-case elapsed time and evidence, source and artifact hashes, environment metadata, and the score out of 100:

```bash
harness benchmark
harness benchmark --seed 20260814 --format markdown
harness benchmark --format json --output benchmark-result.json
harness benchmark --provider-profile provider-profile.json --repetitions 3
```

Without `--provider-profile`, the command does not contact a model service and reports `agentic_score` as `not_run`. A trusted provider profile runs isolated repair tasks. Public tests run inside the repair loop; external hidden evaluators grade the submitted tree afterward. Resolution depends on behavior, path scope, evaluator isolation, and a completed workflow. Byte equality with the reference patch remains a diagnostic and does not decide resolution. Failed attempts retain bounded, redacted trajectory and public-test evidence. Hidden evaluator code and output are never retained. The result reports Agentic Resolution Score, Harness Quality Score, provider calls, token counts when supplied by the provider, tool discovery, elapsed time, and per-attempt results. It is not a SWE-bench or cross-harness score. The versioned manifest, fixtures, and result schema ship with the package.

See [docs/BEST_IN_CLASS_EVALUATION.md](docs/BEST_IN_CLASS_EVALUATION.md) for the evidence required before describing a release as best in class.

Current measured release evidence is in [docs/BENCHMARK_RESULTS_2026-08-15.md](docs/BENCHMARK_RESULTS_2026-08-15.md). A provider startup, authentication, quota, or evaluator failure is reported as infrastructure failure and is not presented as model quality.

The deterministic score grants a case's full weight only when every assertion passes. The manifest weights total 100. A failed critical safety, recovery, graph, stream, or execution case caps the displayed score at 49 while retaining the uncapped score for diagnosis.

Release builds must pass `python scripts/verify_dist.py`. The gate compares every packaged source and resource byte against `src/our_harness`, then probes the zipapp and isolated wheel for benchmark, durable-run, review-panel, and tool-loop capabilities.

## Common work

Debug a failure:

```bash
harness run "Find the cause of the failing checkout test, fix it, and add a regression test"
```

Add a feature:

```bash
harness run "Add CSV export to the report command and cover invalid output paths"
```

Write tests without applying edits:

```bash
harness run --dry-run "Plan tests for the cache invalidation rules"
```

Refactor:

```bash
harness run "Split the parser from transport code without changing public behavior"
```

## How a run works

1. Detect project manifests, standards, tests, linters, and build tools.
2. Incrementally index text and dependency edges.
3. Retrieve matching episodes and workspace evidence.
4. Let the planner use bounded read-only discovery tools, then require acceptance criteria, non-goals, file scope, and checks.
5. Let the coder inspect bounded source evidence, then require baseline-bound file changes.
6. Hold the project transaction lock, reject Windows path aliases and nested harness or Git control components, checkpoint a preallocated transaction intent, reread each planner-approved target immediately before backup and replacement, checkpoint the prepared backups, and apply an atomic transaction.
7. Run only configured or detected checks.
8. Send the canonical cumulative patch and its exact hash to the reviewer in a packet-only request with an immutable review policy and no author context.
9. On failure, send the trace to the repair node and lower temperature.
10. Stop on success, repeated failure, the workflow deadline, or a graph loop limit.

If the run fails, the default policy restores its file transactions in reverse order. Before the first restore write, rollback verifies that every record matches its exact before or after hash and mode, then verifies every backup's manifest-bound SHA-256 and byte count. It persists rollback intent and progress around each atomic restore. A retry skips records already at the before boundary and restores records still at the after boundary. It refuses to overwrite any third state or use a damaged backup. The cumulative frozen scope is verified again around success recording and immediately before return. `harness recovery list` reports interrupted apply and rollback states. Only a fully applied transaction can be finalized; ambiguous state is never changed automatically.

Every completed graph-node boundary has a versioned compare-and-swap checkpoint. Coder changes also checkpoint their transaction ID and candidate before backup creation, then checkpoint the prepared manifest before mutation. `harness runs resume <id>` continues the frozen graph without repeating a completed node or coder call, reconciles its bound transaction, and refuses changed configuration, expired time, altered applied files, unrelated interrupted transactions, or a concurrent resume. A graph may include an `approval_required` node; it records a durable pause, and `runs approve` or `runs reject` requires an explicit JSON-object decision through `--decision-json`. Terminal results remain idempotently readable by run ID after their checkpoint is removed.

## Memory and context

`.harness/memory/harness.db` contains:

- append-only run events;
- episodic successes and failures;
- FTS5 text retrieval;
- optional embedding vectors;
- indexed workspace documents;
- dependency edges;
- prompt versions and pending refinement candidates;
- hash-bound discovery-tool results for crash replay;
- review packets and verdicts.

Memory is advisory. Current disk content, task rules, and fresh command results take priority.

Set `memory.enabled` to `false` for an ephemeral run: source indexing and retrieval are skipped, no episodes or run/review/refinement history is retained, and the configured database is not created. Only process-local workflow events remain until the command exits.

The request compiler keeps the base policy, provider execution boundary, and output grammar in a byte-stable prefix. Planner and coder rounds may request only the supplied read-only discovery tools: bounded tree/file reads, workspace or memory search, dependency context, and explicitly allowlisted MCP calls. MCP tools must set `annotations.readOnlyHint` to the JSON boolean `true` and must not set `destructiveHint` to `true`; idempotence does not grant discovery authority. Other calls are refused. No shell or file-write tool exists in this loop. Each bounded result is labelled as untrusted data, recorded as a run event, and retained in a hash-bound per-call journal for restart replay. OpenAI Responses tool rounds pass typed function outputs with retained response state instead of copying tool data into prompt prose. Later workflow stages separately validate and apply proposed changes or configured verification commands. Repository evidence, memory, and recent events follow in a bounded suffix. The run manifest records the prefix hash, section sources, sizes, and cacheable ratio.

## Checks: the test lab

A check says what to do and what a good result looks like. Checks live in one
JSON file, run side by side, and need no model at all.

```bash
harness qa init      # write a starter suite from the commands already detected
harness qa run       # run them and print what happened
```

There are four kinds. A **command** check runs a program and looks at how it
finished. A **file** check reads a file. An **http** check asks a local server a
question. A **browser** check opens a real page, watches the console and network,
audits accessibility, and can follow a written-down user workflow:

```json
{"do": "click", "target": "[data-view=\"checks\"]", "note": "Open the checks tab"}
```

Checks run in parallel, retry when told to, and mark a case that only passes
after a retry as flaky rather than as a pass. `harness qa flaky` names the checks
whose result keeps changing across runs. Reports come out as Markdown, JSON,
JUnit XML for a build server, or a self-contained HTML page. Evidence for every
attempt is kept under `.harness/qa/runs`.

`harness qa generate` asks the configured model for new checks. Every proposal is
validated, carries plain-language warnings, and does nothing until you run
`harness qa accept`.

Browser checks need Node.js with `npm install playwright` and
`npx playwright install chromium`. Without them those checks report as skipped
with that instruction, and the rest of the suite runs as usual.

See [docs/QA.md](docs/QA.md).

## Agents that talk to each other

A run can use several agents. The arrows of a workflow already say who works
next. Team notes cover the rest: an agent can write a short note to another
agent in the same run, or to everyone.

```json
{"to": "coder", "subject": "The parser caches by file name",
 "body": "Two files with the same name share a cache slot. Key on the full path."}
```

A note is text. Reading one never runs anything, so talking does not widen what
an agent can do to your project. The board is bounded, an agent may only write
to an agent that is really in the run, and every note shows up in the **Team
notes** panel and in the stored run history.

See [docs/TEAM_NOTES.md](docs/TEAM_NOTES.md).

## Control panel

Run:

```bash
harness ui
```

It opens on **Start here**, which lists the few steps left before the project is
ready, and lets you ask for a change in your own words. **Checks** runs the test
lab and shows unstable checks. **Workflow** is the graph editor for later, once
you want to rewire the agents yourself. **Memory** and **Prompt history** show
what the harness has learnt.

There is also a desktop window that starts the server for you and closes it
again on quit. See [docs/DESKTOP.md](docs/DESKTOP.md).

```bash
cd desktop && npm install && npm start
```

## Workflow graph editor

The server binds only to loopback. The canvas supports drag, pan, zoom, keyboard node movement, keyboard connection creation, edge conditions, selected state fields, and bounded cycle settings. A text connection list provides the same graph information without the canvas. **Simulate** explores graph state without project commands. **Start run** validates the current canvas as a production graph, submits its exact hash, and uses its reachable tool roles and repair-edge limits for real configured/detected checks.

The built-in Gauntlet template runs:

```text
Coder -> Syntax Checker -> Security Auditor -> Performance Profiler -> Unit Test Gate -> Reviewer
```

Select `workflow.name: "gauntlet"` for non-UI runs, or edit and submit the canvas through **Start run**. Configure `project.security_commands` and `project.performance_commands`; a Gauntlet run fails closed when either stage has no command. Standard non-UI runs also use an explicit built-in graph. The production interpreter starts at `entry`, visits outgoing edges in declared order, evaluates the restricted typed condition grammar, transfers named variables, and executes each visited planner, coder, tool, evaluator, and end node. Loop edges enforce attempt, temperature-decay, and timeout values. A false entry route or any state with no matching edge fails explicitly. A submitted graph is frozen for that run, so later canvas edits cannot alter it.

## Execution boundary

The default process runner is not an operating-system security sandbox. It uses argv execution, project-root cwd checks, a filtered environment, timeouts, output limits, process-tree cancellation, and denied command patterns. Model-generated code still runs with your user account permissions.

Set `execution.mode` to `docker` for optional container execution. The configured image and project commands must exist inside that container.

Read [docs/SECURITY.md](docs/SECURITY.md) before running on an untrusted repository.

## More documentation

- [Checks and the test lab](docs/QA.md)
- [Team notes: how the agents talk](docs/TEAM_NOTES.md)
- [The desktop app](docs/DESKTOP.md)
- [Source binding audit](docs/AUDIT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Configuration](docs/CONFIGURATION.md)
- [Deterministic benchmark](docs/BENCHMARK.md)
- [Capability parity](docs/PARITY.md)
- [MCP](docs/MCP.md)
- [Accessibility](docs/ACCESSIBILITY.md)
- [Security](docs/SECURITY.md)

## License status

Legal review requested. A license has not been selected. See [LICENSE_REVIEW_REQUESTED.md](LICENSE_REVIEW_REQUESTED.md) before public distribution.
