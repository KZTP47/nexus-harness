# Nexus facilitator mode

New project conversations default to **Facilitator mode · direct project work**.
In an expanded chat, open **collaboration settings → Facilitator mode**, then **Project work mode**, to
choose it or **Private copies · verified delivery**. The same choice remains in
the permissions menu and goal composer. **CHAT** opens by default and gives the
conversation its own pane; team settings, questions, command approvals and
recovery live in **collaboration settings**, with an **Input needed** indicator
when attention is required. The Facilitator mode card always shows whether it is on or off. A paused private-copy goal offers **Switch to facilitator mode**, which explicitly recovers its files and resumes in the selected project. Chat setup and saved files are always expanded and share the settings page scroll; they have no inner scrolling pane. Existing API clients that omit
`policy.execution_mode` retain isolated execution; `facilitator` selects direct
work and `isolated` selects private work. In direct mode, native CLI tools,
Nexus file tools, and structured changes all use that folder. An agent's saved
files are available immediately; there is no later test-gated delivery step.

Nexus coordinates the conversation and records what happened. The user chooses
participants, including reviewers on the same provider. Reviewer feedback and
test results are separate from an agent saying its work is finished. Failed,
unavailable, and unconfigured checks retain their actual status; Nexus does not
start an automatic repair loop or hide files because a check failed.
Success criteria and missing referenced files remain visible as observations;
finishing agent turns does not certify those criteria. A final check needing a
new command grant pauses for the existing permission controls. Denying that check
allows completion with its unavailable result recorded.

Agents choose tools, divide work, and address their own messages. The optional
`summary_delivery` action field accepts `kind` (`agent`, `team`, `user`, or
`auto`), `agent_id` (a selected teammate ID for `agent`, otherwise empty), and
`reply_requested`. Agents set the latter to `true` for a request or `false` for
an FYI, acknowledgement, or announcement. Requests give the recipient another
turn, including after they finished. Informational messages and user-directed
reports do not reopen teammates. Older actions without the flag retain their
previous routing behavior. Omitted or `auto` routing retains cyclic turns.
File edits no longer force finished teammates into another agreement round.
Messages remain in the shared conversation; addressing one is not private delivery.

Requests remain pending in the existing authenticated conversation archive until
a provider response receipts the batch actually included in its prompt. Claiming
a task or starting a provider call is not delivery. Later status messages, short
conversation projections, and restart do not remove pending requests. Bounded
batches are supplied over subsequent turns; a finished contribution gets another
turn if requests remain unreceived. Receipt means the provider received the
message, not that it agreed with or fulfilled it. Historical messages copied
into a fork are context rather than new requests.

A `work` action with tools and `reply_requested: true` yields after the requested
tools finish. Tool results and file changes are saved before the teammate's turn.
The sender's next turn retains those receipts, without replaying completed tools.
Expired effect observations are labelled historical so they are not mistaken for
fresh verification. This is cooperative yielding at tool boundaries, not concurrent
access by independent writers.

Repeated discussion and tool observations are advisory in facilitator mode.
Agents receive repetition counts and tool errors and decide what to do next.
Nexus still observes cancellation, explicit call budgets, provider failures,
and access permissions. Private-copy mode retains its progress guards.
Routing survives restart in the authenticated conversation archive, and
continuation fingerprints invalidate comparison state from earlier behavior.

Read only, Ask before commands, and Full project access remain user choices.
Explicit fixed writer/reviewer roles still apply. New commands in Ask mode use
the existing Deny / Run once / Always allow controls, bound to the project,
working directory, arguments, and timeout. Full mode allows native CLI work;
saved command denials switch native work to inspection so command requests go
through the permission controls. Ordinary explicit shell commands and nested
working directories are supported. Exit failures and timeouts are recorded.
Nexus-run commands retain configured executable/argument denials, built-in
prohibitions, and the chosen process or Docker backend. Changing this execution
policy invalidates native-command grants. Docker mode uses native inspection
and routes commands through Nexus. Native CLI work in Full/process mode uses the
provider's own permissions and is not an operating-system security sandbox.

Facilitator conversations can run alongside other conversations on the same
project. A paused or waiting goal does not reserve the project. Existing
facilitator goals queued behind project ownership become eligible automatically;
explicit pauses and cancellations are preserved.

Nexus coordinates mutations with short cross-process leases covering overlapping
project roots. File proposals check their observed baseline under the transaction
lock. A busy operation or stale proposal returns feedback to the agent for
inspection and replanning. It does not open a project-permission request.
Read-only context tools and agent messages do not require this lease.

A native CLI can modify files anywhere during its invocation, so writable native
invocations hold the operation lease until they return. If another writer is
active, that invocation uses native inspection and can continue communicating or
propose edits through Nexus. Saved permissions are unchanged. API/browser replies
do not hold a native lease. Direct goal writers and private-copy publication
also participate in write coordination. Older external execution engines retain
their admission fence because they cannot participate in operation leases. Only changes observed during protected
operations are credited to the agent. Coordination does not certify correctness.

## Existing private work

Historical replies have **Open working files** and a separate **Open destination**.
Their working-files button finds the private folder from saved goal metadata,
including after a goal has migrated. An old reply remains an old working-copy
report, not proof that its files were delivered.

Normal Resume keeps an existing private goal's execution mode. The separate
**Recover files and resume in selected project** action on a paused goal recovers its combined changes to
the selected project using the existing journalled publication transaction.
Tests do not gate this recovery. Conflicting destination changes and unsettled
operations must be reconciled first; neither version is discarded. Private
folders are retained, and subsequent work uses the selected project. Existing
user access and fixed role choices survive migration. Completed historical goals
are not silently rewritten.

## Integration

Adapted from [Naphal3000's facilitator implementation](https://github.com/Naphal3000/nexus-harness-fork/commit/b8d8ca2e1b907ade50c6119a4d155dc4caded9de).
Nexus retains its source tests, CI workflows, legacy isolated workflows and
versioned project/access bindings. Mode-specific regression coverage lives in
the goal-access, facilitator-conversation, peer-delivery, archive, and native-input
test modules. These deterministic tests establish transport and lifecycle
behavior; they do not measure the quality of a real multi-provider collaboration.
