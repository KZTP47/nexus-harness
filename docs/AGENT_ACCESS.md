# Agent access and command permissions

Team chats display **Agent access**. Pause the team before changing it; the
setting applies to every participant in that goal. The new-goal dialog also
offers the setting before work starts.

| Mode | Behavior |
| --- | --- |
| Read only | Inspect project files; no project edits or test command execution. |
| Ask before commands | Permit edits. Ask before newly discovered commands. Explicitly configured commands and existing exact approvals remain authorized. |
| Full project access | Permit project edits and supported project test commands without command prompts. This is the default when the user has not chosen a mode (`agents-lead-default-full-access/v1`). |

A goal records whether its mode was the user's choice or the default
(`agent_access.chosen_by`); a saved explicit choice of Ask or Read only is always
kept. The composer sends a mode only when the user picked one. A goal created
before Full became the default (it has no access record) keeps Ask, exactly as
before, and shows once: "This goal was created before Full access was the
default; it keeps Ask. You can switch it to Full."

Every mode retains Nexus's confined file operations, disposable verification
copies, protected runtime, independent review rules, and publication checks.
Full project access does not grant arbitrary access to the computer or change
permissions in an external provider application.

When a verification tool requires permission, the engine saves an actionable
command request and pauses at the tool boundary. The chat shows the command,
project, and the resolved package script where available. The choices are:

- **Deny**: retain the work and return the denial to the team. A pause caused
  solely by this command request resumes; an explicit user Pause, another
  question, or an unsettled effect remains stopped. The command stays denied
  until the user changes the decision or access mode. A denial applies only to
  the denied command, matched on its arguments whatever its timeout or
  spelling (`./dist` and `dist` are the same). Nexus refuses it for its own
  tools in every mode, including a private working copy. Claude Code receives
  it as exact `Bash(...)` and `PowerShell(...)` `--disallowedTools` rules (never
  prefixes: denying `git` does not block `git status`) for every spelling Nexus
  can derive (`npm`, `npm.cmd`, `npm.exe`, a full path's basename). A command
  whose arguments contain spaces, quotes, wildcards or the characters `cmd.exe`
  expands (`%`, `^`, `!`) cannot be written as an exact rule and is never turned
  into a different one: like any
  denial for a CLI that cannot enforce it (such as Codex CLI), the agent is told
  it must not run it, and the goal's `command_denials` report shows the user that
  the denial is advisory there. The rest of the agents' work, including native CLI
  work in Full project access, keeps its access.
- **Run once**: allow one verification attempt for the displayed command set.
  Consumption is atomic and durable, including across a restart or failed run.
- **Always allow this command in this chat**: remember the exact command
  definition for this goal. Other chats receive no grant.

Allowing resumes the team unless another user question or interrupted provider
call still needs a decision. Saved permission is retained in either case.
Older paused chats can open **Review command permissions** to recover the
missing decision flow. Conversational answers such as “yes” are not execution
grants; use the command card.

Access state is versioned and bound to the goal, project authority, engine
execution contract, and agent bindings. Command grants additionally bind the
canonical project path, argument lists and discovery manifests. Changed
commands need a new grant; changed access bindings fail closed to Read only.
An explicitly reviewed compatible provider reconnect rebinds validated access
and retains exact grants, including consumed one-use grants. The automatic
Codex strict-schema binding repair rebinds the saved access record and
collaboration roles the same way, so Full project access does not silently
become Read only. Changing the access mode is always available while the team
is paused, even when the saved provider setup has changed; the new record is
bound to the goal's current agent bindings, and the provider change is reviewed
separately through reconnect.
The context fingerprint changes for user decisions, but not when a one-use
grant is consumed, so successful execution evidence remains available.

## Machine limits for agent tools

These limits protect the machine only and are deliberately generous. When one
is reached the agent is told and continues with what it has.

| Limit | Value |
| --- | --- |
| `run_command` timeout | default 10 minutes, agent-requestable up to 60 minutes |
| `run_command` output returned, stored and shown | 200 KB (100,000 characters per stream in the agent's context); larger output keeps its beginning and end around a truncation marker |
| `write_file` content | 10 million characters |
| Files per applied proposal | 500 |
| Waiting for the project write lease | first come, first served; tools 2 minutes, proposals 5 minutes, native turns 30 minutes, then a "busy" observation. A stopped or handed-off turn leaves the queue at once, and a queue ticket not refreshed for 30 seconds no longer blocks anyone |
| Identical tool results in a row | 200, then the goal pauses with a note; all work is kept |
| Stored context steps per task | the latest 64 (full command output for the latest 8), plus a count, the requested files and snapshot/verification receipts of older ones |
| Tool results shown in one prompt | 200 KB in total, newest first; older results are compacted, then replaced by a reference the agent can re-request |
| Unacceptable closeout verdicts in a row | 5, then the goal pauses with a note |

Private working copies, agent copies and closeout snapshots share one
exclusion rule (`goal_workspaces.excluded_entry`), so their inventories of the
same files always agree. They never compare or publish dependency trees
(`node_modules`, `.venv`/`venv` or any folder with `pyvenv.cfg`) or regenerable
caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
`.hypothesis`, `.tox`, `.nox`, `.gradle`, `.parcel-cache`, `.turbo`,
`.pnpm-store`, `.yarn-cache`, `.coverage`, `.eslintcache`, `.ipynb_checkpoints`)
or `.pyc`/`.pyo` files, and these do not count toward the 100,000-file / 2 GB
copy limit. A private goal copy, and each agent's own copy, still *contains* the
project's dependency trees, as real copies (never links into the project), so
tests run there; caches are absent. Links inside a dependency tree are followed
only when they stay in the project (or in the project's own linked install); a
link out of the project (npm link, a planted junction) is not copied, a link cycle
is cut, and a tree over 100,000 files or 2 GB, or that takes over 2 minutes, is
not copied. Each such case is named in the goal note. A `.cache` folder is project content (copied, compared and published)
when it already existed in the project when the copy was made, or holds files
tracked in the project's git HEAD (read with every `GIT_*` variable removed). A
`.cache` folder that is new in the copy is tool output: it stays in the copy, is
not published, and the goal note says "N new cache files were not published". Deliverables such as `dist/` and `build/` are copied and published
normally. Symbolic links, junctions and special files are skipped with a note
(`execution_workspace.skipped_links`) rather than followed or failing the goal.
Hard-linked files (as uv and pnpm create) are ordinary files; publication still
refuses to write *through* a hard-linked project file, because that would also
change its other linked copies, possibly outside the project.

Simple npm/pnpm/yarn scripts using Node, Python, or Playwright resolve into
the existing protected runner profiles. Nexus retains the approved package
command in execution evidence. It rejects compound shell scripts and lifecycle
hooks with an explanation instead of omitting them. Use an explicit supported
runner command for those projects.

The conversation-level selector and decision-card pattern is inspired by
[T3 Code's permission modes](https://github.com/pingdotgg/t3code/blob/main/docs/user/permission-modes.md).
The implementation uses Nexus's own durable store and execution boundaries.

Verification: `python -m unittest tests.test_goal_access` exercises actual
contained Node and Playwright checks, HTTP decisions, restart, single-use
consumption, denial, changed commands and bindings, and file-write rejection.
`node --test desktop/goal-access.test.js desktop/team-goal-feedback.test.js`
checks the interactive controls and responsive chat layout in Chromium.

## Interrupted turns and Resume

Command approval and restart recovery are separate engine states. A lost
provider reply must not become an endless command-approval loop. The chat now
shows the engine's versioned `resume_recovery` projection beside the composer.

On explicit Resume, a provider-only interrupted call made under the exact
`codex-cli/isolated-exec/v1` + `codex-cli/effective-dispatch/v2` contract can be
superseded with a fresh read-only inference. That contract uses an ephemeral
directory, ignores user configuration and rules, rejects native tool contracts,
and enforces a read-only sandbox. This bounded compatibility path also covers
old authenticated calls admitted under that same contract. Startup alone never
resends them. The project identity, workspace and current provider binding must
still match, and the old worker must have stopped.

Other interrupted provider calls require the visible **Retry interrupted agent
call** choice, which explains possible duplicate remote actions/usage. Its
versioned fingerprint and goal revision bind the exact effects shown. Stale,
cross-chat, changed-configuration, and duplicate submissions fail closed.
Recovery commits together with Resume; a later failed Resume check rolls back
the recovery. Prior effect IDs remain in the audit trail, and old leases cannot
deliver replies into a new attempt. Completed work, access grants and spent
budgets are preserved. Pending file transactions/actions are never discarded by
this provider-only recovery path.

Regression checks: `python -m unittest tests.test_goal_recovery` and
`node --test desktop/goal-recovery.test.js`. They cover authenticated HTTP
controls through scheduler completion, real file publication, restart,
permission changes, stale choices, changed providers, file-effect rejection,
late replies, and responsive recovery controls. The packaged smoke check also
renders the recovery card using the installed renderer.

## What restricts agents in Work together

The "work together" engine (`src/our_harness/swarm_work.py`) follows the owner
rule in `AGENTS.md`, "Agents lead; Nexus only supports". Agents may create,
modify and delete any file in the selected project. Nexus restricts them only
when the user explicitly asked it to:

- **Read-only run**: only on explicit wording such as "read-only", "don't
  change anything", "make no changes", "no changes" or "just explain, don't
  change". A question ("Can you make the app faster?", "Why is login
  broken?"), a verb such as show/list/check/explain, "without breaking X",
  "avoid X", or a bug report phrased as a prohibition ("Files must not be
  deleted when the user cancels") never makes a run read-only. A prohibition
  with an exception ("don't change anything except app.py", "... However, fix
  the typo") is a scoped restriction, not read-only. In an explicitly
  read-only run, proposed changes are not applied, but they are recorded and
  both the agents and the user are told what was proposed.
- **Protected paths**: only explicit negation or preservation wording
  protects a path: "don't touch/change/modify/edit X", "never edit X",
  "without changing X", "leave X alone", "keep X unchanged", "preserve X",
  "X is read-only", "X must not be changed", "X must not change", "treat X as
  read-only", "hands off X", "freeze X", "no changes to X", "nothing in X may
  change", "refactor everything except X", list forms ("Do not modify:" with
  one bullet per path, "Protected files: a, b", "Don't touch: a, b",
  "Read-only: a") and per-file labels ("config/secret.txt: do not modify",
  "(LICENSE: read-only)"). A per-file label protects that file only; it never
  makes the whole run read-only. `./X`, `.\X`, `X:42` and absolute paths inside
  the selected project name the same file, a bare name such as "config"
  resolves to the matching project folder or file, globs ("do not touch
  *.md", "config/*.txt") protect every matching file, and "the tests" means
  the project's test files and folders. Absolute paths listed one per line
  under a read-only heading are protected too. Mentioning, reviewing,
  reading, using, consulting or copying a file never protects it ("The bug is
  in src/parser.py; please fix it" leaves `src/parser.py` writable).
- **Write scope**: files and folders named in the goal only add to what may be
  written; they never limit it, and a named file to fix or update may be
  created when missing. Writes are limited to named paths only when the user
  says so ("only change X", "change only X", "edit X only", "by editing only
  X", "limit changes to docs/", "change nothing except X", "touch nothing but
  X", "don't change anything except X", "X and nothing else", "everything else
  is read-only", "only change the tests", "only edit the docs"), or sets write
  destinations in the UI. An explicit "only" whose target is unusable
  (`../outside`, `/etc`, `.git`, a host:port, or a path the user also
  protects) is never widened to the whole project: the run stops before any
  agent is contacted and asks the user to name writable paths. Naming the selected project folder itself means full access, and
  naming a sub-folder ("put the guide in the docs folder") does not restrict
  writes elsewhere. A destination outside the selected project is reported to
  the agents; Nexus still cannot write there.
- **Tests**: test evidence is required only when the user explicitly asked for
  tests to be written or run ("add tests", "write a unit test", "make the tests
  pass", "run the test suite", "Tests are required", "It must have tests").
  "Add Stripe integration", "Fix the unit conversion bug", "Do not add tests",
  "without adding tests" and "No need to add tests" do not ask for tests. One
  shared classifier decides this for every engine
  (`goal_verification.tests_explicitly_requested` / `explicit_test_requests`,
  which also tells a request to write tests from a request to run them).
- **Agent file names**: a proposed path with a character Windows forbids
  (`<>:"|?*`), a control or tab character, a segment over 255 characters, or a
  parent that is a file is refused for that one entry; the rest of the agent's
  changes still apply and the run continues.

Everything else Nexus derives from goal wording or from the agents' plans is a
hint recorded with `mandatory: false`: named-file effects, file operations
(move/copy/rename/replace), artifact kinds, behaviour clauses and planned
`effect_paths`. Hints are shown to the agents and reported in the result
(`advisory_unmet`, `unchanged_hints`, `planned_effect_paths`), but they never
block completion and never stop a run. Only well-formed path tokens become
path hints: "Rename the Save button to Submit in index.html" names
`index.html`, not a file called "Submit in index.html"; host:port locations,
code scopes (`std::`), URLs and absolute/drive/UNC paths never become project
paths.

Completion follows the agents. When every agent agrees the goal is done:

- if the selected project's deterministic checks pass, the result is
  `verification_status: deterministically_verified`;
- if no check can run (no test command, an unapproved discovered command, a
  missing runner, or no containment profile for the toolchain such as go,
  cargo, dotnet, mvn, gradle, make or pwsh), the goal completes with
  `verified: true`, `machine_verified: false` and
  `verification_status: agent_verified`, and the reply says it was not
  machine-verified. No pass is fabricated;
- a failing check still keeps the goal open with the failure as feedback;
- "no change was needed", with the agents' explanation, is a valid completion;
- causal behaviour receipts are gathered when possible and reported as a
  bonus, never required, and an ambiguous probe target never pauses the run.

Any applied edit of a selected-project file counts as progress for renewing
the context-tool budget. The genuine protections stay: no writes outside the
selected project or through `..`, absolute or drive paths; `.git` and
`.harness` stay protected; secrets are redacted; explicit user wording and UI
write destinations are enforced; user-set budgets apply.

### Applied work is kept; one agent's slip never stops the team

- **Pauses keep applied work.** A provider outage, an unreconciled delivery or
  an exhausted context-tool budget pauses the run (resumable), but every
  transaction the agents already applied stays in the project. The mutation
  saga is closed as `kept`, the pause checkpoint records `changed` and
  `kept_transaction_ids`, and a resumed run continues from the project as it
  is now (the kept files are shown to the agents). The pause reason says what
  happened: `provider_turn_failed`, `provider_outcome_unknown`, or
  `invalid_structured_result` (a format slip, `stopped_because:
  provider_protocol_failure`), never "provider turn failed" for a schema slip.
- **An incomplete run keeps its edits** (`mutation_recovery.status: kept`) and
  is reported as incomplete with what was applied; it stays resumable.
- **A transaction that cannot be applied** (baseline conflict, write failure)
  is undone on its own by `FileTransaction`; earlier transactions stay, the
  agent is told, and the run continues (`transaction_failures`).
- **Format slips are per agent.** A reply that does not fit the format (after
  chat's one correction for CLI/API routes, or the web correction) skips only
  that agent's turn; the others continue and the agent is asked again next
  round/pass (`format_failures`). The run pauses only if no agent at all
  gives a readable reply three rounds running. In team discussion an agent
  whose provider fails a round (including the first answers) rejoins the next
  round; consensus waits for it until it has failed three rounds running.
  A verifier whose reply cannot be read never confirms completion: its turn
  blocks the pass (its words, or the reason, are shown to the others) until
  it has failed the format three passes in a row; after that it abstains and
  the final reply says its verification was not counted.
  An unreconciled delivery (`outcome_unknown`) is never re-sent.
- **Lenient reading of replies.** The schema sent to providers stays
  strict-compatible (closed objects, optional values nullable), but received
  replies are read tolerantly: unknown keys are ignored, omitted optional
  fields get defaults, the JSON object is taken from the whole reply, the
  last fenced block, or an object that ends the reply after some prose (an
  object quoted mid-prose is not the reply, so a correction is asked for), text
  fields longer than the (10x) caps are shortened with a marker, and
  `read_file {path}`, `list_tree {}` and `search_workspace {query}` get default
  arguments. Malformed and over-cap tool calls spend the session's call
  allowance like unknown tools, and one executor turn has at most
  `MOST_TOOL_ROUNDS_PER_TURN` (200) ask/tool rounds. `tool_calls` and `changes` may arrive together: the changes are
  applied first, then the tools run and their results come next turn. Up to
  200 changes and 64 tool calls per reply (`MOST_CHANGES_PER_REPLY`,
  `MOST_TOOL_CALLS_PER_REPLY`); excess entries are refused individually.
  Work together sends `EXECUTION_FORMAT` (`nexus_board_file_work_v2`);
  `WORK_FORMAT` v1 stays unchanged for long-horizon goals that embed it.
- **One bad change entry is refused alone** (duplicate path, invalid mode,
  protected path, outside the user's scope, `.git`/`.harness`, symlink); the
  rest are applied and the agent is told which entry and why
  (`refused_changes`).
- **No progress heuristics stop a run.** Only after six exactly identical
  rounds (every agent's full reply, tool calls, changes, project state) do the
  agents get a notice that they may be looping; they continue. User-set round
  limits still apply. One generous machine guard remains so an unlimited run
  cannot spend the user's accounts forever. It looks only at outcomes, never
  wording: completion/readiness flags, verification status and the content
  of the files the run changed. After `NO_CHANGE_GUARD_ROUNDS` (200) rounds
  without a new outcome state, or, in project work, when the run returns to
  an already-left project state twice (ping-pong edits A/B/A/B/A), the run
  stops (`stopped_because: no_change_guard`), keeping all work and resumable.
  Rewording a blocker does not count as a new outcome. There is no finite
  default round limit. In discussion, a peer that failed three turns in a
  row stops being asked; a peer whose delivery is unknown is never asked again
  for that turn, and if that happens in the first answers the collaboration
  finishes there with the healthy answers saved.
- **Consensus**: all agents saying `goal_complete` with remaining items that
  are "None"/"N/A" or start with an explicit `Optional:`, `Advisory:`,
  `Non-blocking:` or `Follow-up:` label completes (wording such as "Could not
  run the tests", "May still crash" or "Minor bug: ..." still blocks); all agents saying `goal_complete` two rounds running
  completes too, keeping any leftover items as `advisory_remaining`.
- **Participants**: up to 24 agents (`MOST_PARTICIPANTS`) take part; when the
  cap applies the user and agents are told who was left out
  (`participants_left_out`). On a project the cap applies after the works-on
  filter.
- **Unsafe path spellings** (`..`, `//`, a stream colon) never refuse the goal;
  they are reported to the agents (`ignored_path_tokens`) and grant nothing.
  An explicit protection or "only" scope spelled that way still applies to the
  collapsed in-project path (`cfg//secret.txt` protects `cfg/secret.txt`).
  Writes through such paths are still refused at apply time.
- **Earlier runs never lock the project.** A preserved `rollback_conflict`
  journal is reported with its journal path, the conflicting files, the files
  that undo had already rolled back and those still applied (compensation
  runs newest-first and stopped at the conflict), and is never retried: the
  user's current files are the truth. It is marked `conflict_acknowledged`
  only after the notice is saved in a run's ledger, so a run that stops early
  reports it again next time. A run whose process died keeps its applied transactions; only a
  half-applied (prepared/rolling-back) one is restored. An interrupted undo
  the user asked for is finished. A damaged journal is renamed
  `*.json.damaged` and reported.
- **Owner leases on Windows**: when even a limited query handle is refused
  (csrss, lsass, another user), the owner identity cannot be checked, so the
  lease counts as alive only while its heartbeat is recent (2 hours; the saga
  heartbeats at every turn). Pipeline runs use `updated_at_ms` the same way.
- **Hard links**: after its baseline checks pass, `FileTransaction` gives a
  hard-linked target its own copy (`shutil.copy2`: content, mode, times; on
  Windows also attributes and streams, but not an explicit ACL) before
  rewriting it, so other links (possibly outside the project) keep their
  content. A refused transaction never severs a link; the manifest records
  `hard_links_separated`. Rollback restores content but does not relink.
- **Heartbeats**: the saga heartbeats in every executor ask/tool round and
  verification turn; a polling pipeline owner (running or waiting for a
  decision) refreshes `updated_at_ms` every minute.
- **Prompt size**: the team plans in every execution prompt go through the
  same bounded projection as the conversation.

A user cancel still undoes the run's transactions (`compensate`), as before.

Regression checks: `python -m unittest tests.test_agents_lead_policy
tests.test_user_restrictions` (and the updated policy tests in
`tests.test_swarm_work`).
