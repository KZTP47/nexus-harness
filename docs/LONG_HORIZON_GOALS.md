# Work together on a project

Choose two connected agents and their shared project, type a goal, and click
**Work together on project files**. The agents take turns in that chat, see
each other's real messages and current files, and work toward the goal. Type
in the same composer to steer them. **Pause team** retains the work and
**Resume team** continues it. Questions, errors, and progress appear inline;
Mission control provides optional detail.

You can open another saved chat and start **Work together on project files**
while the first chat continues. Each goal keeps its own conversation, progress,
and pause/cancel controls, including when the chats share an agent and select
the same project folder. Each saved chat works in an independent copy of the
selected project, including its uncommitted files. This works without Git.
Provider account capacity still limits simultaneous requests. A confirmed chat
switch enables its composer and controls while history loads in the background.

Nexus synchronizes unrelated project changes into the copy before final checks.
If this changes the files the team agreed on, the team inspects them again.
Only the final check-and-apply step is serialized; other chats keep working.
Changes are applied back to the selected project with exact baseline checks
and rollback backups. A conflicting edit pauses that chat and names the paths;
it never silently replaces the other chat's result. Goal details show the
retained working-copy folder. Reconcile the conflicting files in the selected
project, then Resume. A completion message appears only after verification
and successful application to the selected project.

If the team creates a new test command in its copy, open goal details and choose
**Review this chat's test commands**. Approval names the exact command and is
bound to this goal, selected project and test manifest. It does not approve
another chat's checks. Resume runs the approved checks. Changes to the command
or its discovery manifest require another review.

After upgrade, settled saved chats can adopt an independent copy while keeping
their files, questions, history and spent budgets. An active provider call,
unknown outcome or unfinished file transaction must settle before migration.
Legacy board-only and legacy paired work still own the original project
exclusively. Working copies are retained for inspection and recovery; they
require disk space for the project (up to 100,000 files and 2 GB). Git metadata
and Nexus control folders are excluded. Linked paths and substituted folders
are rejected rather than copied outside their authority.

A completed goal appears as a distinct **Task completed** message with the
Nexus Harness icon and a link to its saved result and verification details.
Pasted files and images use the same attachment tray as files added with
**Attach files or screenshots**. Images show previews, and you can remove any
attachment before sending. Plain text paste still inserts text into the composer.

Chat includes public progress messages and tool activity with expandable input,
output, and error details. The saved history retains these entries across
restart. These are provider-supplied public summaries and observed tool calls;
private model reasoning is not available.

## Default behavior

**Work on project files** and **Work until the goals are achieved** use the
long-horizon engine by default. When **Work on project files** starts from a
saved two-agent chat, that exact pair is a required team: each named participant
gets serialized turns on the same goal and at least one provider-call slot is
reserved for every initially required participant. Useful turns alternate;
the ordered conversation and current verification results go to the next
agent. A new file change reopens the other agent's previous completion so it
can inspect the latest work. **Send to team** steers this exact active goal;
before work starts, **Send** is ordinary direct chat. The older paired
plan/review/execute workflow is available only through **Use legacy paired
workflow**.

New shared goals have no cumulative provider-call or tool-call ceiling by
default. An explicitly supplied `max_provider_calls` or
`max_context_tool_calls` is honored exactly; zero means no cumulative limit.
Older saved goals retain their recorded finite limits because those records
cannot distinguish a default from a user's choice. Pause and cancellation
remain available throughout the conversation.

An agent can return one structured next action:

- do or complete the current task;
- delegate bounded independent subtasks;
- hand the task to another authorized agent;
- request a targeted independent review;
- ask the user a structured question for a permitted blocking reason; or
- report a concrete blocker.

Agents choose useful work rather than following a fixed plan/review ceremony.
Both participants must finish against the latest shared changes. A required
participant cannot hand its named contribution to somebody else. A known refusal, malformed reply, or
known provider failure is recorded truthfully and does not prevent the other
named participants from being attempted; an unknown provider outcome, pending
file effect, changed provider/account contract, Pause, or user question remains
fail-closed. If any required contribution failed, the goal pauses after the
remaining safe attempts and never claims complete. Independent tasks may run
in parallel when their resource paths and provider identities do not conflict.

## Durable state and restart behavior

Each goal has a stable ID, immutable original objective, revisioned active
steering, explicit success criteria, recorded call usage and user budgets, a dependency-aware task
ledger, agent ownership, evidence, artifacts, verification, and structured
interrupts. State and an HMAC-authenticated, hash-chained typed event journal
are stored outside project mutation authority. LangGraph SQLite checkpoints
preserve the exact scheduling and user-interrupt boundary.

Provider dispatch and acknowledgement are separate durable states. If Nexus
restarts after dispatch without a known acknowledgement, it records an unknown
outcome and will not resend until the user explicitly reconciles it. A result
that arrives after Pause is retained as pending work and cannot mutate files
until Resume. Every real transport call—including a structured-output repair
call—consumes the goal's provider-call budget before dispatch. Steering changes
the objective epoch: an in-flight or acknowledged-but-unapplied result from the
older objective is durably marked superseded and cannot touch project files.
File transaction identity and intended paths are journalled before the atomic
transaction is applied.

The initial **Work on project files** click also has an authenticated admission
journal. Prepare is persisted before Start, and a terminal receipt stays fenced
until the renderer has cleared its exact browser/desktop record and explicitly
acknowledged the matching outcome. Authenticated terminal rows created by
`0.2.1` did not yet record client acknowledgement; after an upgrade they are
treated as unknown and unconsumed, never as permission to resend. Nexus exposes
the newest one for each chat, verifies its exact goal or discard outcome, and
then reveals any older row. A fresh request for that chat remains blocked until
every such outcome has been acknowledged.

Required-team scheduling is itself a versioned, non-secret collaboration
contract in the admission digest. It covers turn order, conversation history,
call reservation, named work, failure continuation, and completion. A
pristine legacy goal can adopt the current contract without provider or file
effects. Once a legacy goal has crossed either boundary, a missing or changed
contract stays inspectable but cannot dispatch under silently different rules.

Each admitted agent also carries a hash-only binding to its exact route,
provider profile, selected model/command semantics, and adapter transport
contract. A settings reload may change the current board, but it cannot
silently redirect an unfinished goal through the new setup. Mission control
marks **Provider setup changed**, disables Resume and other provider actions,
and offers **Prepare a new goal with current setup**. The old task ledger,
events, evidence, and artifacts remain inspectable; starting fresh creates a
new goal identity instead of rewriting that history.

Agents can inspect the project through bounded tree, file, search, proposed-
change, and selected-verification tools. Every request and result is saved as a
context step before the next provider call. Web-chat providers use a strict
fenced structured reply with one budgeted format-only correction; malformed
prose never becomes a file action. File proposals are bound to the exact hashes
the agent observed, and no-op plans are rejected instead of being counted as
fresh work.

## Mission control

Mission control rebuilds from the goal snapshot plus ordered event deltas and
shows:

- the objective, criteria, state, progress, and remaining budgets;
- tasks grouped by state with dependencies, owner, attempt, blocker, and
  evidence;
- agents, provider routes, assignment counts, and current activity;
- structured user questions with recommended choices and custom answers;
- file transaction patches and hashes, deterministic tests, repairs, and
  reviews; and
- a filterable typed event timeline with durable cursors.

The controls pause, resume, cancel, retry, steer, message an assigned agent,
reassign or request review, and fork the saved checkpoint into an isolated Git
worktree. Controls operate on stable goal/task identities; stale task leases
and late results are rejected.
Decision submissions are bound to the exact displayed goal revision and full
set of pending question IDs, so a stale card cannot answer a changed goal.
Cancel voids pending cards, and a goal with a pending decision cannot be forked.
A paused isolated chat retains its own working copy; other isolated chats can
continue on the same selected project. A paused legacy goal still owns the
original project exclusively until it finishes or is cancelled. Forking requires
a settled checkpoint and a clean Git source; unpublished working-copy changes
must be retained in the original chat rather than silently omitted from a fork.

## Completion and review

Agent prose alone cannot complete a task. Completion needs an artifact or a
concrete evidence marker, and configured or discovered deterministic checks
must pass. Each success criterion receives a
recorded result and basis. Verification failure creates one bounded repair
task; repeated no-progress or exhausted budgets pause the goal instead of
manufacturing progress. Verification infrastructure that is unavailable
before a test launches pauses immediately with the completed work resumable;
it does not spend provider calls asking an agent to repair the Windows sandbox
or a missing runner.

Work together runs the selected project's real test commands and retains their
results. Its shared verification profile requires evidence for configured tests,
both agents' completion, and task evidence for the user's criteria. It does not
turn arbitrary wording into a fixed set of inferred test scenarios. Configured
commands retain their execution scope; discovered commands still require the
user's approval and run in a disposable, protected copy of the project.
Executable deliverables such as games, apps and scripts require executed,
meaningful checks. If none are configured or discoverable, Nexus reports the
missing evidence and gives the team a bounded repair task to author checks and
expose a test command at the selected project root. File existence, an unchanged
snapshot and agreement between agents cannot prove that an application works.
Static document work and read-only inspection can still complete from relevant
artifact evidence without inventing tests or unnecessary edits. Explicitly
requested testing always needs actual execution evidence. If discovered checks
need approval, approve them in the project's settings and press **Resume team**. Resume
adopts the current settings for that exact project, records their new fingerprint,
and clears obsolete test observations while retaining the agents' work and chat.

For browser work, the agents receive a supported recipe for the bundled,
contained Playwright runner: navigate a project route, perform a concrete
click/fill action and assert the resulting DOM state. Checks must exercise the
requested launch method and behavior. A passing HTTP-route check is not proof
that an ES-module application can be opened directly through a `file:` URL.
See [orchestration quality and verification](ORCHESTRATION_QUALITY.md) for the
capability boundaries and regression examples.

No-progress fingerprints include public discussion, semantic evidence, and
before/after content. New discussion can continue without artificial file edits.
Fresh transaction IDs or timestamps alone are not progress. Repeated identical questions,
handoffs, delegations, verification failures, and unchanged work are bounded,
while genuinely changed proposals reset the relevant counter.

Review is risk based. Broad, destructive, sensitive configuration/security,
or previously failing changes trigger review. An independent review requires
a different provider identity. If none is available, Nexus asks the user
whether deterministic checks alone are acceptable rather than forcing a fake
second-agent ceremony.
