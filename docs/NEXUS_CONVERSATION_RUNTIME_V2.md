# Nexus Conversation Runtime v2

Status: proposed normative architecture
Primary metric: verified goal completion attributable to useful cooperation
Companion gate: [NEXUS_CONVERSATION_RUNTIME_ACCEPTANCE.md](NEXUS_CONVERSATION_RUNTIME_ACCEPTANCE.md)

## 1. Purpose

Nexus must let two or more agents cooperate reliably on both one-turn requests and
long-running work. A shared conversation file is valuable, but a file alone is not
a transport, proof of delivery, authority boundary, scheduler, or lifecycle.

The runtime therefore owns one durable conversation record and produces the right
view of it for each agent. Inline prompts, provider threads, Markdown files, MCP
resources, and uploaded snapshots are capability-specific projections of the same
canonical conversation.

The ordinary user experience remains:

1. Open a chat.
2. Type a request.
3. Press **Send**.
4. See a useful result and a concise, truthful status.

Engine details appear only when recovery, consent, or diagnosis requires them.

## 2. Root-cause findings in the current system

The current ledger is a useful prototype, not the final authority.

| Current behavior | Root problem | Observable failure |
|---|---|---|
| `CollaborationLedger.projection_for()` advances a cursor while constructing text | Prepared context is treated as delivered context | A failed provider call can make unread peer context disappear on retry |
| Each orchestration call creates a new ledger session | Run identity is confused with chat identity | Shared state does not naturally span the complete saved chat |
| One shared Markdown mirror and one integer cursor | Delivery and visibility are not recipient-specific | Agents with different tasks, permissions, and context limits receive the wrong shape of history |
| Transcript, JSONL, cursor JSON, registry, attachments, and web bindings commit separately | No single transaction owns conversation truth | Crashes can produce conflicting authorities |
| Chat storage is selected through pair/route-derived `filed_as` | Mutable bindings participate in ownership | Route changes and partial cleanup can orphan history |
| `swarm_chats.delete()` hides the registry entry before deleting artifacts | Deletion is not a recoverable state machine | A failed delete can hide still-live data |
| In-flight responses have no chat generation or binding lease | Late work is not fenced | A response can append after reset, deletion, or route change |
| Progress is inferred mainly from model prose and similarity | Self-report is treated as state | Repetition can look productive and invented completion can look final |
| Full/recent conversation and ledger projection are repeatedly combined | Context is duplicated instead of compiled | Original requests reappear, prompts grow, and agents anchor on stale work |
| Provider history is implicitly trusted as continuity | Remote threads are treated as canonical memory | Thread replacement or manual provider messages can contaminate a Nexus chat |

The root architectural error is multiple partial authorities. The correction is one
conversation runtime with transactional state, recipient-specific context, explicit
delivery, and generation-fenced lifecycle transitions.

## 3. Non-negotiable invariants

1. One immutable `conversation_id` owns exactly one chat and all local runtime state.
2. Names, participant pairs, routes, projects, titles, and provider threads never select storage.
3. A chat may contain many user-initiated runs; a run never replaces chat history.
4. Only Nexus commits canonical state. Provider text proposes state transitions.
5. Every provider turn is attributable, idempotent, context-versioned, and generation-checked.
6. Preparing context never advances a delivered or acknowledged cursor.
7. Peer and external text remain quoted, provenance-bearing, untrusted content.
8. Agreement is not verification; completion is not based on agent self-report.
9. Provider threads are replaceable delivery caches, not canonical memory.
10. Pauses are resumable. Reset, rebind, and deletion invalidate late answers.
11. Unlimited rounds removes the numeric conversation-cycle ceiling only. It does not remove loop detection, cancellation, permissions, provider safety, or an independently configured unattended-use policy.
12. The default UI requires no knowledge of ledgers, cursors, leases, DAGs, hashes, or provider bindings.

## 4. Runtime topology

```text
User and UI
    |
Conversation service  -- project registry and tombstones
    |
Per-conversation actor -- serializes commits and lifecycle transitions
    |
Transactional capsule -- events, runs, tasks, claims, turns, receipts, ACLs
    |
Context compiler ----- recipient-specific, budgeted, provenance-preserving views
    |
Provider scheduler --- queues per physical binding; parallel across safe bindings
    |
Adapters ------------- API | CLI | web | MCP/resource | file-aware desktop
```

Provider calls may wait concurrently outside the actor. Only commits and lifecycle
changes serialize per conversation. Unrelated chats must not be globally serialized.
A physical provider connection that cannot handle concurrent turns has its own
exclusive dispatch queue.

The project-level control store is a small transactional SQLite catalog, not the
conversation-content authority. It owns project-wide ID-to-backend mapping, immutable
legacy aliases, physical provider-connection records, and provider dispatch leases.
It caches lifecycle status for indexing but does not own conversation generation,
turns, deliveries, context projections, or working state while a capsule exists.
Capsule SQLite owns those facts. After capsule detachment, a separately durable
content-free tombstone witness owns non-resurrection. These authorities are disjoint.

Its normative tables are `conversation_index`, `lifecycle_operations`, `legacy_aliases`,
`provider_connections`, and `dispatch_leases`. Foreign keys bind every operation to one
project and conversation. `conversation_index` never stores message content, generation,
or working state. Each catalog transaction uses full synchronous durability; directory
and rename barriers are recorded as separate saga steps because SQLite cannot atomically
commit filesystem operations. Catalog lifecycle rows are coordination hints that are
reconstructible from capsule state and tombstone witnesses, never a competing commit
fence.

## 5. Identity and ownership

Use distinct opaque identities:

```text
project_id       stable identity bound to the canonical project root
conversation_id  at least 128 cryptorandom bits; one chat
generation       monotonically increases on reset/delete/recovery boundaries
run_id           one user message that launches team work
turn_id          one logical request to one participant
participant_id   membership instance inside one conversation
membership_epoch increments when membership is revoked/re-established
binding_epoch    version of a participant's provider connection/thread
grant_revision   version of a participant's authorized history ranges
```

`board_agent_id`, display name, role, provider route, model, thread URL, and selected
project are mutable metadata. Changing any of them does not move or relabel history.

Every participant membership records join and leave sequences plus a history grant.
The same board agent in ten chats has ten independent participant memberships. Two
chats with the same pair have different conversation IDs, capsules, views, cursors,
provider bindings, and deletion boundaries.

A run also has a monotonically increasing `objective_revision` and `frontier_version`.
Every lease and response carries all four mutable fences: conversation generation,
membership epoch, binding epoch, and objective/frontier revision. A user edit, removal,
rebind, reset, or deletion can therefore revoke only the affected work.

## 6. Transactional conversation capsule

The canonical local unit is:

```text
.harness/chats/capsules/<validated shard>/<conversation_id>/
  manifest.json
  conversation.sqlite3
  views/
    full-transcript.md
    <participant_id>/context-v000042.md
  attachments/
  legacy/                 # migration evidence only
```

SQLite is the authority. WAL is used while open. Markdown and provider snapshots are
regenerable projections. Nexus is the sole writer. OS-level project and capsule locks
prevent a second Nexus process from becoming a writer during migration or deletion.

Required logical tables:

- `conversation`: ID, generation, schema, lifecycle, project, title, policy, last sequence.
- `participants`: stable board-agent reference, membership epoch, join/leave sequences, history grant.
- `events`: immutable ordered events, audience, authority class, provenance, keyed integrity chain.
- `runs`: objective revision, mode, policy, status, frontier, pause checkpoint, outcome.
- `turns`: recipient, generation, binding epoch, idempotency key, context version, status.
- `deliveries`: attempts, transport state, projection digest, receipts, timestamps.
- `cursors`: prepared, transport-accepted, response-acknowledged, and committed sequences.
- `provider_bindings`: participant, route, provider thread, epoch, verification marker.
- `tasks`: dependencies, owner, risk, artifact, acceptance criterion, verification method, state.
- `claims`: kind, author, evidence, opposition, verification state, supersession.
- `decisions`: alternatives, recommendation, authority, dissent, decision and supersession.
- `receipts`: Nexus observations and deterministic verification evidence.
- `access_grants`: participant/event-range visibility.
- `projection_versions`: exact included events, digest, token accounting, recipient, delivery state.
- `projection_outbox`: regenerable view publications committed with their source event.
- `dispatch_outbox`: logical turn dispatch intents committed with the planned turn.
- `attachments`: content digest, type, provenance, owning event, lifecycle.

Append-only events and typed materialized state update in one transaction. Materialized
state must be reproducible from events. A keyed MAC chain using a Nexus key outside
the project distinguishes Nexus commits from agent-authored imitations; it does not
prove an agent claim is true.

Open runs are recovered from committed state. Hundreds of idle chats do not keep
database handles open.

The project control store uses full durability, keeps a validated backup snapshot, and
never interprets corruption as an empty catalog. On startup it reconciles each cached
lifecycle row against the authoritative capsule or tombstone witness. Generated views
are updated after the capsule transaction through its own durable projection outbox;
projection failure cannot roll back or replace canonical events.

Creation is also a journaled saga:

1. Reserve a never-reused ID and validated final path in a durable `CREATING` catalog
   row; it is not yet visible as a chat.
2. Create an ID-specific temporary capsule on the same volume, initialize SQLite,
   write the signed manifest, flush files, and flush the containing directory.
3. Validate project, ID, and schema and atomically rename the temporary capsule to its
   final path.
4. In one catalog transaction record manifest digest/path and transition to `ACTIVE`.
5. Startup resumes a valid completed temporary/final capsule or removes only its exact
   invalid temporary directory. It never exposes an empty record or adopts an
   unverified orphan.

### 6.1 Durable-field ownership and lock protocol

| Durable fact | Sole authority | Mirrors/caches |
|---|---|---|
| Project ID, conversation-to-backend/path mapping, legacy alias, active migration token | Project control store | UI registry |
| Conversation generation/lifecycle while attached, events, runs, memberships, tasks, claims, decisions | Capsule SQLite | Catalog/UI status |
| Logical turns, deliveries, cursors, provider bindings, dispatch intents | Capsule SQLite | Ready index/Electron cache |
| Projection jobs and included-event digests | Capsule SQLite outbox | Markdown/MCP/uploaded views |
| Physical connection identity/capabilities and exclusive dispatch lease | Project control store | Adapter process state |
| Deletion/non-resurrection after witness handoff | External keyed witness log | Catalog tombstone index |
| Provider/Electron thread page state | No canonical authority; verified delivery cache | Rebuilt from active binding token |

Duplicate values outside their owner are explicitly labelled cached, include the owner
version/digest, and are never accepted as a commit fence.

One project-writer fencing token prevents a stale Nexus process from writing after a
new process takes ownership. The engine never holds a capsule SQLite transaction and
control-store transaction simultaneously. Cross-store work uses prepare/commit tokens
and idempotent reconciliation:

1. The conversation actor serializes and commits capsule state plus its outbox/intent.
2. After that transaction closes, a worker idempotently acquires or updates the
   control-store coordination record by immutable intent ID.
3. Before external dispatch, the worker holds the physical connection lease, briefly
   enters the actor, revalidates every capsule fence, and commits `dispatched`; it then
   leaves the actor before waiting on the provider.
4. Deletion/revision may subsequently revoke the turn; the provider call may finish,
   but its answer cannot commit.
5. Recovery scans authoritative incomplete capsule intents and upserts missing
   coordination, while orphan coordination records with no matching live intent are
   dropped.

Lifecycle and migration use the same pattern: prepare under the actor, close the
capsule transaction, compare-and-swap the catalog token, then reconcile derived caches.
No correctness claim depends on simultaneous cross-database commit.

## 7. Event, trust, and provenance model

Every content item has an audience, source event, and one authority class:

```text
system_policy
user_instruction
nexus_control
nexus_verified_state
tool_receipt
peer_claim
peer_message
external_content
legacy_unverified
```

Rules:

- Summarization never raises authority. A summary inherits the least-trusted relevant source.
- Disclosure provenance is transitive. A derived item may be shown only to the
  intersection of the access grants of all contributing sources. Redaction or
  summarization does not declassify a source. Broader sharing requires a separate
  user-authorized declassification event with a safe replacement value.
- Peer text stays attributed and quoted; it cannot alter policy or authorize tools.
- Provider-thread messages created outside Nexus are `external_content`, not user instructions.
- “Tests passed” is verified only when a Nexus receipt records the test and result.
- Proposed tasks, claims, and decisions become canonical only after schema and authority validation.
- Retrieval filters by conversation, access grant, and trust class before semantic ranking.
- Every summary assertion links to its source events, preserves unresolved contradiction, and is invalidated by relevant state changes.

A deterministic receipt contains the originating turn, confined working directory,
redacted arguments, timestamps, result/exit status, and output/artifact digests. It is
authenticated by Nexus and cannot be minted by placing plausible JSON in a project.

## 8. Runs, goals, and objective revision

The original request is durable authority, but it is the active question only on the
first turn of its run. A later user message creates an objective revision inside the
same chat. It may supersede, narrow, extend, or cancel earlier goals; it is never
silently appended as if every historical request were current.

Each run has a goal contract:

- Current objective and objective revision.
- Unsuperseded user constraints.
- Definition of done and acceptance criteria.
- Permission/project boundary.
- Active task frontier and dependencies.
- Required verification level.
- Round and unattended-use policies.

Later agent turns receive the current assignment and changed state, not a repeated
instruction to answer the first user question.

When a user revises the objective during active work, the actor transactionally
increments `objective_revision` and `frontier_version`, marks incompatible outstanding
turns `revoked_stale`, and preserves compatible accepted work with explicit provenance.
Old provider answers may be retained only in a quarantined diagnostic record; they
cannot update tasks, claims, transcript, or cursors unless the actor deliberately
reissues them under the new revision. The UI applies ordinary follow-up messages at the
next safe boundary and offers immediate interruption when the message explicitly stops
or replaces active work.

## 9. Context compiler and shared-file layer

Each recipient receives a deterministic context version containing:

1. Stable system policy and exact agent identity.
2. Current objective revision and unsuperseded constraints.
3. Nexus phase, role, and exact assignment for this turn.
4. Accepted decisions and unresolved material dissent.
5. Authorized unread delta since the acknowledged cursor.
6. Relevant cited evidence and verified progress.
7. Outstanding tasks, blockers, and acceptance criteria.
8. A reference to the authorized full conversation view.
9. A strict response and acknowledgement contract.

The full transcript remains available but is not injected every round. Escalation is:

```text
delta + current state
  -> cited retrieved history
  -> provenance-backed checkpoint
  -> authorized full snapshot/resource
```

The shared Markdown files are immutable per-version recipient views. External edits do
not mutate canonical state; Nexus regenerates the view and reports tampering. The full
human transcript is separately generated and paged in the UI.

Token budgeting uses provider-specific counting when available. Pinned identity,
objective, constraints, assignment, and response contract are never silently dropped.
If they do not fit, Nexus makes a cited checkpoint and rotates the provider thread or
asks the user to reduce scope.

## 10. Provider capability negotiation

The engine never assumes that “desktop AI” means file-aware.

| Adapter | Primary context delivery | Full-history option | Acceptance evidence |
|---|---|---|---|
| API | Native system/developer/user roles | Inline retrieval or uploaded resource | HTTP acceptance and response envelope |
| Subscription CLI | Bounded serialized prompt | Permitted local file or snapshot when supported | Successful prompt handoff and response envelope |
| Codex CLI boundary | Bounded serialized prompt | Inline/retrieved projection | Process acceptance and response envelope |
| Electron web chat | Serialized user turn through controlled browser | Uploaded or inline snapshot | New unique Nexus marker visible in the bound thread |
| MCP-capable agent | Prompt plus read-only resource URI | Conversation resource | Resource read receipt and response envelope |
| File-aware desktop adapter | Prompt plus exact immutable view path | Recipient view/full transcript | Adapter-confirmed file open plus response envelope |

A logged-in desktop app does not imply it watches Nexus files. Authentication,
provider execution, and context-ingestion capability are negotiated separately.

Each adapter registers a stable engine-owned `connection_id` for the actual physical
isolation domain: browser profile/session, CLI credential/profile plus process lane,
API account/endpoint lane, or MCP connection. Dispatch serialization is keyed by that
ID and its probed concurrency scope—not by display name or route string.

Capabilities have `unknown`, `declared`, `probed`, `temporarily_failed`, and
`unsupported` states with probe version and expiry. Login probing is separate from
context delivery, file access, tool isolation, thread history, marker reconciliation,
structured response, cancellation, and concurrency probes. Unknown/expired capability
falls back to inline bounded context, one in-flight turn, and honest unconfirmed status.
An adapter cannot enter file-aware or concurrent mode until the exact capability probe
passes. Every fallback is recorded and contract-tested.

The planned turn and its dispatch intent commit together in the capsule
`dispatch_outbox`. The scheduler upserts that intent into an in-memory/global-ready
index and then acquires one expiring control-store lease keyed by
`connection_id + concurrency_scope`; the lease uniqueness constraint is the
cross-process mutex. The control store does not decide whether the turn exists or has
committed. Missing ready-index entries are rebuilt from capsule outboxes; entries with
no matching live capsule turn are discarded. Thus a crash can cause an idempotent
rescan but cannot lose or invent a logical turn.

The lease records writer identity, owner-process start nonce, deadline, turn ID, and
reconciliation state. A process recovers an expired lease only after adapter-specific
reconciliation. Browser profile/session identity and CLI credential/profile isolation
are canonicalized so route aliases cannot create two connection IDs for the same
composer or process lane.

Capability probes are invalidated by adapter/provider version, profile/login, thread,
or configuration change; probe TTL expiry; or a contradictory runtime failure. The
failing capability immediately enters conservative fallback and cannot remain enabled
until a later scheduled probe.

Where provider roles are flattened into one CLI/web message, rigid serialization,
unique delimiters, and schema validation reduce ambiguity, but prose delimiters are
not treated as a security boundary.

## 11. Turn envelope and delivery state machine

Every request includes at least:

```json
{
  "protocol": "nexus-collaboration/2",
  "conversation_id": "cv_...",
  "generation": 7,
  "run_id": "run_...",
  "objective_revision": 4,
  "turn_id": "turn_...",
  "attempt": 1,
  "recipient_participant_id": "part_...",
  "membership_epoch": 2,
  "binding_epoch": 3,
  "grant_revision": 5,
  "phase": "peer-review",
  "frontier_version": 11,
  "task_ids": ["task_12"],
  "context_version": 42,
  "context_digest": "sha256:...",
  "through_sequence": 126,
  "required_ack": ["turn_id", "context_digest", "through_sequence"]
}
```

The response separates human prose from proposed state changes and echoes the required
acknowledgement. Nexus rejects a response from the wrong conversation, generation,
run, objective revision, frontier version, turn, recipient, membership epoch, binding
epoch, grant revision, or context digest. Revocation and validation happen under the
same conversation-actor transaction as commit, eliminating a leave/revision race.

```text
planned
 -> projection_prepared
 -> dispatched
 -> transport_accepted
 -> response_observed
 -> response_validated
 -> committed
```

Exceptional states are `delivery_unknown`, `quarantined_invalid`, `failed`, and
`revoked_stale`.

Rules:

- Projection preparation advances only `prepared_seq`.
- Provider acceptance advances `delivered_seq`; a valid committed response advances acknowledged/committed cursors in the same transaction.
- Retry uses the same logical `turn_id`, idempotency key, and projection digest unless a new context version is explicitly prepared.
- Nexus guarantees one logical commit, not impossible exactly-once network delivery.
- Web retries first reconcile the unique turn marker in the exact bound thread.
- Mutation-authorizing work is never silently re-executed after ambiguous acceptance.
- A late response fails if generation, objective/frontier revision, membership epoch,
  grant revision, binding epoch, turn lease, or lifecycle changed.
- The UI says “context delivered” only for proven transport acceptance and never claims the model understood it.

### 11.1 Web-adapter submission contract

Consumer web pages are a weaker transport than an API or CLI and must expose a
stricter, provider-versioned adapter contract. A web attempt progresses through:

```text
prepared -> inputting -> filled_verified -> activating
          -> acknowledged | delivery_unknown | rejected
acknowledged -> streaming -> completed
```

Authentication-required, challenge, cancelled, quarantined, and provider-changed are
separate typed states. Immediately before activation, the adapter revalidates that the
composer contains the exact unique turn marker and that the composer-scoped send
control is visible, enabled, topmost at its click point, and still has the same
fingerprint observed during stabilization. Activation uses a native pointer or
keyboard event through the browser automation boundary; a DOM `.click()` is not a
delivery receipt.

Only the exact marked provider-side user turn is strong submission acknowledgement.
Composer clearing, a Stop control, a reply-count change, or apparent streaming may
corroborate acceptance but cannot independently advance the acknowledged cursor.
After activation without the marked turn, the attempt becomes `delivery_unknown`:
Nexus reconciles the exact bound thread and never auto-resends. A retry is offered only
after Nexus proves that no matching provider turn exists. Provider-specific locale,
zoom, responsive layout, SPA remount, overlay, login/challenge, restart, and delayed
acknowledgement fixtures are release-blocking adapter tests.

A visible Stop control means the provider still reports generation in progress.
Apparent text stability never authorizes Nexus to press Stop or commit a response:
reasoning, tool use, and provider backpressure can all leave valid partial text static
for long intervals. At a bounded deadline Nexus may stop the remote generation, but
the partial response remains uncommitted and the run pauses with an incomplete turn.

Transport retries are independently bounded even under Unlimited rounds. The default
is one reconciliation plus at most two adapter retry attempts; a provider-specific
policy may be lower. Exhaustion rotates a safe binding or creates a resumable provider
pause. It cannot consume infinite attempts inside one team cycle. Mutation turns stop
after ambiguous acceptance until reconciled or explicitly resolved.

## 12. Provider-thread binding and divergence

A binding is scoped to:

```text
conversation_id + generation + participant_id + binding_epoch
```

Before web delivery, verify connection identity, exact thread/URL, and last Nexus
marker. Unexpected remote messages are not silently imported. The user may import
them as external conversation content or start a clean provider thread.

If a provider thread is missing, oversized, or corrupt, Nexus commits a cited
checkpoint, increments the binding epoch, opens a fresh thread, and sends the
checkpoint plus unread delta. The Nexus conversation ID remains unchanged.

Two logical agents sharing one physical provider connection can produce sealed
independent drafts, but their calls are serialized when the adapter is not concurrency
safe. Nexus labels these honestly as separate drafts, not model diversity.

## 12.1 Enforceable action boundary for flattened providers

Trust labels and prompt delimiters are not sufficient when a CLI or desktop agent can
modify files or invoke tools. Context-bearing collaboration turns therefore default to
**advisory mode**: no side effects, read-only authorized resources, and a structured
proposal response.

Execution uses a separate Nexus-authorized turn containing only the validated task,
required evidence, and minimum cited inputs. It does not forward raw peer/external
prose as executable instructions. The action broker enforces the user/project authority,
path confinement, `.harness` denial, command/tool policy, network policy, and
confirmation requirements independently of model output.

For agents that need to produce file changes, the adapter uses an OS-confined staging
workspace or isolated worktree. Nexus verifies an explicit change manifest and applies
accepted changes through the broker. If the adapter cannot prove read-only advisory
mode or confined staging, it is marked advisory-only and may not perform project
mutations. Web providers without tools remain advisory; provider-integrated remote
tools are disabled unless a brokered adapter can enforce the same policy.

## 13. Adaptive collaboration protocol

There is no mandatory debate loop. The scheduler selects the smallest protocol likely
to improve verified outcome.

Protocol selection is a deterministic, versioned policy decision recorded with its
inputs and reason. Inputs include task risk, ambiguity, decomposability, verification
availability, requested independence, provider capability/diversity, permissions,
estimated context cost, and previous failures. A model may recommend a protocol but
cannot select one. A policy matrix chooses fast review for low-risk bounded work,
sealed exploration for ambiguity/anchoring risk, task-DAG execution for decomposable
work, and evidence-driven dispute resolution for material contested claims. Users can
override the collaboration/round policy at a safe boundary, but not waive project
authority or deterministic failure.

The selector begins with a falsifiable rule table:

- Use a **direct single-agent path** when the output is deterministic and low-risk,
  no hidden information is distributed across peers, a local oracle exists, and the
  expected peer benefit is below the calibrated threshold. No ceremonial peer call.
- Use **fast review** when one bounded independent check can catch a plausible error.
- Use **sealed exploration** when ambiguity, anchoring, or alternative generation is material.
- Use **task-DAG execution** when two or more independently verifiable frontier tasks exist.
- Use **evidence dispute** when a material claim is contested.
- On uncertain features, choose the more conservative review/exploration path for high
  risk; otherwise use direct execution plus deterministic verification.

Thresholds and features are versioned, benchmark-calibrated, emitted in the trace, and
tested for stable decisions. Post-run collaboration lift updates evaluation data but
does not let a model rewrite the live rule table.

### Fast path

For trivial or bounded work: lead answer, independent peer check, deterministic Nexus
verification where possible, and synthesis only if the peer adds material value. The
normal cost is one team cycle.

This fast-review path is selected only after the direct-path rule declines. Truly
deterministic trivial work may finish with one agent and a local oracle.

### Exploration path

For ambiguous, high-risk, or intentionally diverse work: collect sealed independent
proposals. Record commitment digests and reveal proposals only after all eligible
participants respond or time out. This prevents first-answer anchoring.

### Planning and execution path

Convert the goal to testable criteria; build a validated task dependency graph; assign
ready work by capability, permission, and load; verify outputs; and replan only the
unresolved frontier. Safe independent work may run in parallel across safe provider
bindings.

### Critique and dispute path

Agents critique claims, evidence, assumptions, and artifacts. A material dispute must
trigger a deterministic check, new evidence, a blind verifier, or a user preference
decision. Repeated debate without a new test or evidence source is not scheduled.

### Independent verification

Verification precedence is:

1. Deterministic oracle: tests, schemas, hashes, runtime observation.
2. Cross-agent artifact/evidence review.
3. Blind independent judge where deterministic proof is unavailable.
4. User decision for preference or new authority.

An agent cannot judge its own artifact. A judge sees objective, criteria, artifacts,
receipts, unresolved high-risk claims, and dissent—not a persuasive full transcript by
default. A judge failure adds concrete tasks; it never restarts the original request.

Independence is evaluated from contribution lineage, not participant ID. The judge
record includes provider/model family and version, system-role template, physical
connection/account, context exposure, artifact ancestry, evidence sources, and prior
author/reviewer roles. A judge may not author, synthesize, or previously approve the
judged artifact. High-risk model-based judgment requires separation in model/provider
or an explicit user-approved limitation; otherwise Nexus uses a deterministic verifier,
obtains a genuinely independent provider, or reports that independent judgment is
unavailable.

## 14. Tasks, claims, decisions, and completion

Task states:

```text
proposed -> ready -> leased -> running -> produced -> verifying -> done
                                      \-> blocked | failed | superseded | cancelled
```

A task cannot be `done` without its acceptance condition being checked. Dependencies
form a validated DAG; circular dependencies and assignments are rejected.

Claim states distinguish proposed, contested, verified, refuted, unresolved, and
superseded. Decision state records alternatives, authority, recommendation, decision,
and material dissent. “Both agents agree” and “verified” are different facts.

Completion requires:

- All required tasks accepted.
- Critical acceptance criteria verified.
- No unresolved high-risk claim or unaddressed material dissent.
- No required turn left uncommitted or delivery-unknown.
- A judge pass when risk policy requires it.

Deterministic failure overrides an AI judge pass.

## 15. Progress, loops, and resumable pause

Progress is a committed state transition, not novel wording.

Primary progress includes a task passing acceptance, a material claim becoming
verified/refuted, a blocker being removed, a decision resolving, or an artifact
passing verification. Secondary progress includes genuinely new evidence, an
actionable newly discovered blocker, or useful task decomposition.

Paraphrasing, reasserting the same blocker, agreement without evidence, unverified
file changes, repeated unchanged failures, and completion claims are not progress.

Every cycle receives a fingerprint of task, claim, decision, evidence, artifact,
blocker, and provider-failure state. Suggested response:

1. First no-progress repetition: one useful retry.
2. Second: require replan, changed evidence source, or assignment.
3. Third, or a proven A/B oscillation: create a resumable pause checkpoint.
4. Resume without changed conditions allows one diagnostic cycle before pausing again.

Waiting on a real external operation, rate limit, or user input is not counted as a
failed progress cycle. Stagnation is never presented as completion.

A new claim counts as secondary progress only when it is relevant to an acceptance
criterion, survives canonical duplicate/contradiction checks, and cites a new source or
creates a testable state change. Secondary progress is provisional: within two team
cycles it must lead to primary progress, a concrete blocker, or a scheduled independent
check, otherwise its progress credit expires. The fingerprint uses canonical IDs,
normalized evidence/artifact digests, task transitions, and verified receipts; agents
cannot keep a run alive by inventing differently worded claims. Replans and verifier
retries are also bounded between primary transitions.

Concretely, a secondary transition must change a canonical frontier edge: attach a new
content digest or receipt to a pending criterion, change a claim's contested or
verification state, produce a new executable verification route, or replace a blocker
with a narrower blocker whose precondition was deterministically checked. New labels,
uncited assertions, child tasks with unchanged acceptance coverage, and semantically
duplicate sources receive zero progress credit.

## 16. Round and resource policy

One **team cycle** means one opportunity for each currently eligible participant to
make at most one committed contribution to the active frontier. Phase changes do not
reset the count. Provider retries remain inside a turn and do not create hidden cycles.

Visible choices:

- **Auto**: recommended; continue while verified progress occurs.
- **One review**: lead plus one independent peer check.
- **Fixed...**: exact total team cycles.
- **Unlimited rounds**: no numeric team-cycle ceiling.

Round policy is separate from unattended-use policy such as “ask after 30 minutes or
50 provider calls.” A user may explicitly set both to unlimited. Loop detection,
permissions, cancellation, rate limits, and system-resource protection still apply.

Changing a policy during a run applies at the next safe turn boundary.

## 17. Lifecycle, deletion, branching, and reset

Lifecycle is journaled and idempotent:

```text
CREATING -> ACTIVE <-> ARCHIVED
ACTIVE -> DELETION_REQUESTED -> TURNS_REVOKED -> WRITERS_DRAINED
       -> KEY_REVOKED -> CAPSULE_DETACHED -> PHYSICALLY_DELETED -> TOMBSTONED
```

Deletion first increments generation and revokes turn leases in the capsule transaction.
Only then does Nexus durably witness deletion outside the capsule, drain writers,
verify the validated capsule path and manifest, close/checkpoint SQLite, detach the
exact capsule atomically, and remove an explicit allowlist of owned artifacts. A crash
resumes from capsule state plus the witness. A Windows file lock
leaves a visible **Deleting...** state and safe retry; it never hides live data.

The registry never reports deletion before physical local deletion completes. A
tombstone witness permanently rejects ID reuse and late responses. Deleting one chat never
uses a pair, route, participant name, or wildcard and cannot touch another chat.

Remote provider deletion is a separate explicit option. Nexus never claims local
deletion erased data already sent to a provider, export, or backup.

User-facing operations are unambiguous:

- **New chat**: new conversation ID and no inherited history.
- **Restart provider connection**: new binding epoch, same Nexus history.
- **Branch from here**: new conversation ID with a self-contained cited snapshot.
- **Delete from Nexus**: delete only the selected local capsule.

A branch survives parent deletion and is never cascade-deleted by default.

There is no overloaded engine “reset”:

- **Restart this run** cancels the active run, increments conversation generation,
  retains prior history as cited context, and creates a new run/objective revision.
- **Clear chat and start fresh** creates a new conversation ID and then runs the exact
  deletion saga for the old chat. If old-chat deletion cannot complete, the UI shows
  both the usable new chat and the old chat in **Deleting...**; it never hides the old
  data or routes new turns to it.
- Legacy `start_again` is mapped to one of these explicit operations by caller intent;
  it may not delete files and silently recreate state under the same identity.

Restart-run itself is journaled: in one capsule transaction increment generation,
revoke outstanding turns and leases, terminally mark the old run
`cancelled_by_restart`, persist its final checkpoint, and create the new run/objective
revision. No filesystem deletion occurs. Startup therefore observes either the
complete old generation or complete new generation, never a half-reset transcript.

Cross-filesystem deletion is a durable saga with explicit authority handoff:

1. Acquire the actor/lifecycle lock. In one capsule transaction validate ID/generation,
   increment generation, write `DELETION_REQUESTED` with an operation nonce, and revoke
   every turn and capsule dispatch intent. While attached, this capsule state is the
   sole response fence.
2. Append and flush a content-free `deletion_requested` witness containing project ID,
   conversation ID, new generation, and operation nonce to the keyed append-only
   per-project witness log in Nexus application state outside project backups. Mirror
   it into the catalog only as an index. The witness is the non-resurrection boundary.
3. Drain writers, release physical dispatch leases, close/checkpoint the exact capsule,
   and verify its signed manifest and operation nonce.
4. Atomically rename the capsule to an ID-specific staging name on the same volume and
   update only cached catalog status. Responses cannot open the attached capsule and
   also fail the witness check.
5. Delete the explicit child allowlist and generated mirrors, recording idempotent
   witness states. Remote cleanup remains separate.
6. Append and flush `deletion_complete`; then remove the catalog index row while
   retaining the content-free witness. Missing files alone never imply completion.

The catalog and capsule are never claimed to commit atomically. Before witness creation,
the capsule generation is authoritative. After the flushed witness, the witness is
authoritative even if capsule or catalog copies are stale. Startup scans non-terminal
witnesses and exact staging paths to converge the saga.

The live control store records a witnessed-through sequence. A validated catalog backup
is only a cache: recovery first replays the higher valid sequence from the external
keyed witness log. If that log is missing or corrupt while a backup/manually restored
project is detected, Nexus fails closed into **Recovery required** and activates no
restored conversation automatically. Nexus-managed backups exclude key material and
must consult the live witness log. Arbitrary user/provider copies already disclosed
outside Nexus cannot be promised erased.

## 18. Participant and privacy changes

New participants receive the current objective, future events, and only accepted state
whose complete source-grant intersection includes them—not raw or derived disclosure
from restricted pre-join messages. Restricted fields appear as “Earlier context not
shared.” One plain-language choice offers earlier history. The resulting access grant
is recorded.

Changing to a new provider trust domain shows what will be shared and offers current
summary, full permitted history, or a fresh chat. Re-login to the same route does not
repeat this prompt. Removing an agent revokes future delivery but does not falsely
claim a remote provider forgot previously disclosed content.

Optional encryption uses one random data key per capsule, wrapped by a Nexus key held
outside project backups. Key revocation precedes physical deletion. Backup restore
honors tombstones and cannot silently resurrect a deleted chat. Encryption follows,
and must not delay, transactional correctness.

## 19. Recovery and corruption

Corruption is never interpreted as an empty chat. On integrity failure Nexus seals the
capsule read-only, shows **Needs repair**, preserves the original, recovers a valid
authenticated prefix into a new generation where possible, compares counts/digests,
and reports any lost suffix. A newer schema opens read-only rather than being
downgraded.

Startup recovery:

- Expires stale turn leases.
- Reconciles delivery-unknown turns.
- Resumes only unresolved work.
- Never resends a committed logical turn.
- Keeps successful peer work when one provider fails.
- Resumes interrupted migration/deletion journals.

## 20. UX and accessibility

The main composer contains message, **Send**, attachment control, and one unobtrusive
**Team: Auto** policy control. Expert numeric/unlimited/resource settings live in its
popover. File-changing work retains explicit confirmation.

Running status answers only:

- What is happening?
- Who is active?
- Is verified work progressing?

Primary control is **Pause after this turn**. Stop is a secondary terminating action.
Pause cards explain the exact reason and offer a small set of meaningful actions such
as continue once, change direction, retry provider, ask the user, or finish here.

Activating Pause immediately fences all new dispatches and commits a durable pause
request/checkpoint; **after this turn** describes graceful handling of an already
accepted call. Nexus attempts adapter cancellation with a short bounded deadline. If
the call hangs or cannot cancel, status becomes **Paused — waiting for Claude to
finish** and offers **Detach this turn**. Choosing immediate detach revokes its lease
and rejects any eventual response. Keyboard and screen-reader users receive the same
immediate transition and recovery actions.

The default status vocabulary is **Working**, **Recovering**, **Paused safely**,
**Needs attention**, and **Stopped**. Transport attempts and team reasoning rounds are
shown separately: an attempted or failed delivery never increments reasoning progress.
The ordinary view shows one sentence and the relevant actions; raw selectors, markers,
leases, and stack traces stay in expandable diagnostics.

Normal status examples:

- “Latest shared context sent to both provider chats.” This appears only when both
  exact transport receipts exist; details say it does not prove understanding.
- “Sending updated context to Claude...”
- “Claude may have received this turn. Nexus paused safely and did not resend.” Actions:
  **Open provider**, **Reconcile**, **Use another provider**, or **Stop**.
- “Nexus proved this turn was not submitted — Retry.”
- “This provider chat contains messages Nexus did not send — Review.”

Protocol details are behind **How the team reached this** and **Context details**.

The deletion command is **Delete from Nexus**. Its confirmation says that the exact
local Nexus chat and files will be deleted and provider history may remain. **Also
delete provider chat** is a separate unchecked option only when the adapter can verify
support; its success or failure is reported independently.

Accessibility requirements include semantic controls/headings, keyboard operation,
text in addition to color, reduced motion, 200% reflow, high contrast, stable focus,
polite concise live announcements, no forced scrolling, and accessible transcript
virtualization. Background scheduler events must not flood screen readers.

## 21. Observability and evaluation

Each run stores a redacted local trace of objective revisions, context versions and
digests, cursor stages, binding and retry decisions, accepted/rejected state changes,
tasks, claims, evidence, progress fingerprints, loop reasons, judge inputs/verdicts,
budgets, pauses, resets, cancellations, and stale-response rejection.

Diagnostic export is explicit and sanitized; nothing is uploaded automatically.

Success metrics prioritize:

- Verified goal completion for trivial and long-horizon tasks.
- Collaboration lift over the best single-agent baseline.
- Material peer contribution to successful outcomes.
- Original-question re-answer rate.
- Unnecessary peer-call rate on trivial tasks.
- Loop-pause precision/recall and productive-long-run false-pause rate.
- Cross-chat leakage, stale/duplicate commit, wrong-chat deletion, and permission breach rate.
- Restart/provider-failure resume success.
- Unverified completion-claim rate.
- Time, provider calls, user interventions, and accessibility.

Agreement rate and message count are not success metrics.

## 22. Migration plan

### Phase 0: instrument and fence

Add stable run/turn IDs, generation, binding epoch, context digest, delivery states,
response acknowledgement, and progress receipts. Stop cursor advancement on context
preparation. Move delete-time cancellation and provider-binding cleanup into the engine.

### Phase 1: lifecycle facade and durable scheduler

Make `ConversationStore` the only chat persistence/lifecycle API. Add per-binding
queues, idempotent turn state, pause/resume recovery, and tombstone fencing while
preserving the present UI and transcript.

### Phase 2: capsules in shadow mode

Create a durable one-to-one migration mapping keyed by project ID, legacy registry
schema, legacy chat ID, and validated `filed_as`; its value is one new 128-bit
conversation ID. Re-running migration must reuse this mapping.

Lazily import each idle legacy chat into a temporary capsule as `legacy_unverified`,
preserving source hashes, `filed_as`, and web-thread metadata. During shadow mode the
legacy store is the sole authority. A catalog outbox records each legacy mutation and
updates a disposable capsule replica in sequence; the replica is never read for user
answers or lifecycle decisions. Failed import leaves the legacy chat intact and
visible and may discard/rebuild the shadow.

Historical v1 ledger cursors are not imported as delivered evidence because v1
advanced them during preparation. Each becomes a `legacy_prepared_upper_bound`. Nexus
recovers the last independently evidenced provider marker or response where possible;
the remaining range is `delivery_unknown`. On first v2 contact it sends one cited,
idempotent checkpoint containing that range under a new context digest. Stable event
IDs and the current assignment prevent duplicate state commits. Migration prefers safe
context replay over silent context loss.

### Phase 3: canonical capsule reads and recipient context

Switch canonical reads only after event/attachment counts and digests match. Generate
legacy JSON/Markdown views from the capsule. Cutover acquires the exact chat lock,
drains/revokes old turns, catches the outbox up, verifies counts/digests, and atomically
changes one catalog `storage_backend` pointer. From that commit onward the capsule is
the sole authority and legacy files are read-only replicas. Enable distinct projections
and four-stage cursors.

Rollback is phase-specific:

- Before cutover: discard/rebuild the shadow; legacy remains authoritative.
- During the cutover transaction: rollback the catalog transaction; no provider turn
  can run under both backends.
- After cutover but before capsule-only features: export a verified legacy replica,
  compare it, then atomically switch the catalog pointer back under the chat lock.
- After capsule-only typed events exist: older code may open the chat read-only; rollback
  requires a tested down-migration and may never silently discard events.
- After legacy retirement: restore only from a verified capsule backup and tombstone-aware
  catalog snapshot.

Provider-binding cutover is staged, not cross-database atomic. Before cutover, the
capsule stores an inactive prepared binding, immutable cutover token, expected physical
connection, and last-marker evidence. Electron/provider state remains a cache. Under
the actor lock, with old turns drained, the catalog atomically changes
`storage_backend` from legacy to capsule and records that exact prepared token. This
catalog pointer is the sole activation decision: a capsule binding is usable only when
its token matches the active pointer.

A crash before that catalog commit continues legacy. A crash after it opens the
capsule, validates the matching token, and idempotently rebuilds Electron/provider
caches before dispatch. No second capsule commit is required for activation. If the old
thread cannot be verified, the prepared record instead requires a fresh binding epoch
and migration checkpoint; Nexus never guesses continuity from a route string.

### Phase 4: typed cooperative work

Introduce tasks, claims, decisions, evidence receipts, adaptive fast/exploration/work
protocols, progress fingerprints, and risk-based independent judging. Run new loop and
completion logic in shadow evaluation before it controls stopping.

### Phase 5: lifecycle completion and UX

Enable crash-safe deletion, branching, repair, provider rotation/divergence handling,
the single Team policy control, resumable pause cards, and accessible details.

### Phase 6: optional encryption and managed backup

Add per-chat keys, tombstone-aware backup/restore, retention reporting, and separately
verified provider-side deletion.

### Phase 7: rollout

Feature-flag per conversation, canary new chats, retain downgrade-safe read behavior,
compare old/new outcomes, and promote only after every critical release gate passes.

## 23. Decision summary

The shared-file idea is retained and strengthened:

```text
one Nexus conversation ID
 -> one transactional, independently recoverable capsule
 -> many runs, memberships, and provider-binding epochs
 -> immutable typed event history
 -> bounded recipient-specific views/files/resources
 -> explicit delivery and acknowledgement
 -> verified adaptive cooperation
 -> one exact lifecycle and deletion boundary
```

This gives desktop agents access to durable chat context without pretending that file
existence proves delivery, understanding, authority, or completion.

## 24. Primary design references

These sources inform the architecture; Nexus does not claim protocol compliance until
the relevant adapter is implemented and conformance-tested.

- [SQLite write-ahead logging](https://www.sqlite.org/wal.html) and
  [atomic commit](https://www.sqlite.org/atomiccommit.html) inform capsule transaction,
  checkpoint, crash-recovery, and close-before-delete rules.
- [MCP server resources](https://modelcontextprotocol.io/specification/2025-06-18/server/index)
  support treating files and other resources as application-controlled context rather
  than canonical model memory.
- [A2A protocol context, task, message, and artifact semantics](https://a2a-protocol.org/dev/specification/)
  support distinct conversation context, stateful task, message, and output identities;
  Nexus adds local transaction, authority, deletion, and provider-adapter guarantees.
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
  and [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) support
  durable per-thread checkpoints and resumable pause semantics, including the need for
  idempotent side effects when work can replay.
