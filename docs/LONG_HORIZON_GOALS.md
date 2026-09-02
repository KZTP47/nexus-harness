# Long-horizon goals and Mission control

Nexus project work is an event-driven task system, not a required meeting
between every connected agent. The board defines who is allowed to work on a
project. A goal then assigns the next concrete ready task to one useful agent.
A single ready agent is enough to begin and finish.

## Default behavior

**Work on project files** and **Work until the goals are achieved** use the
long-horizon engine by default. When **Work on project files** starts from a
saved two-agent chat, that exact pair is a required team: each named participant
gets its own serialized contribution task and at least one provider-call slot is
reserved for every initially required task. Untouched required tasks are chosen
before repeat turns. The final named contribution receives a bounded durable
packet of the earlier outcomes and performs the safe fan-in before deterministic
verification. **Send** remains a faithful direct message and **Ask connected
agents** remains explicit conversational collaboration. The older paired
plan/review/execute workflow is available only through **Use legacy paired
workflow**.

An agent can return one structured next action:

- do or complete the current task;
- delegate bounded independent subtasks;
- hand the task to another authorized agent;
- request a targeted independent review;
- ask the user a structured question for a permitted blocking reason; or
- report a concrete blocker.

Nexus does not require every agent to propose a plan, review the plan, execute
the same phase, or vote unanimously. A required participant cannot hand its
named contribution to somebody else. A known refusal, malformed reply, or
known provider failure is recorded truthfully and does not prevent the other
named participants from being attempted; an unknown provider outcome, pending
file effect, changed provider/account contract, Pause, or user question remains
fail-closed. If any required contribution failed, the goal pauses after the
remaining safe attempts and never claims complete. Independent tasks may run
in parallel when their resource paths and provider identities do not conflict.

## Durable state and restart behavior

Each goal has a stable ID, immutable original objective, revisioned active
steering, explicit success criteria, bounded budgets, a dependency-aware task
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
contract in the admission digest. It covers claim fairness, call reservation,
non-transferable named work, failure continuation, fan-in, and completion. A
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
A paused goal deliberately continues to own its project; cancel or finish it
before starting another project-writing workflow on the same tree.

## Completion and review

Agent prose alone cannot complete a task. Completion needs an artifact or a
concrete evidence marker, and the goal still remains incomplete until
deterministic project verification passes. Each success criterion receives a
recorded result and basis. Verification failure creates one bounded repair
task; repeated no-progress or exhausted budgets pause the goal instead of
manufacturing progress. Verification infrastructure that is unavailable
before a test launches pauses immediately with the completed work resumable;
it does not spend provider calls asking an agent to repair the Windows sandbox
or a missing runner.

No-progress fingerprints compare semantic evidence and before/after content,
not fresh transaction IDs or timestamps. Repeated identical questions,
handoffs, delegations, verification failures, and unchanged work are bounded,
while genuinely changed proposals reset the relevant counter.

Review is risk based. Broad, destructive, sensitive configuration/security,
or previously failing changes trigger review. An independent review requires
a different provider identity. If none is available, Nexus asks the user
whether deterministic checks alone are acceptable rather than forcing a fake
second-agent ceremony.
