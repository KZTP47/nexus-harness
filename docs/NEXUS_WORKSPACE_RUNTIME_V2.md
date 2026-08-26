# Nexus workspace runtime v2

## Scope and evidence threshold

This design covers the **AI Agent Swarm orchestrator** and **Visual test
automation** workspaces. It is based on current source inspection, focused test
runs, browser inspection, and deterministic counterexamples. It does not treat
provider text, UI labels, or design intent as proof of engine behavior.

Release success means that every acceptance test in this document passes and
independent judges find no reproducible release blocker. It does not mean that
all possible future bugs have been proved absent.

## Proven failures to eliminate

1. Automation execution has no shared, cross-process coordinator. Panel, agent,
   timer, CLI, and editor entry points can overlap or disagree about the active
   project.
2. Automation runs, events, stop requests, human approvals, and results have no
   durable run identity. Stale controls and events can affect or appear on a
   different run.
3. Step deadlines and Stop are advisory for most node kinds; a late success can
   still be reported as passed.
4. Empty, skipped, and flaky work can aggregate to a false pass. Allowed-failure
   semantics also disagree between gates and final aggregation.
5. Raw QA evidence can persist credentials in node details, artifacts, API
   responses, timer history, and CLI output.
6. Artifact nodes can overwrite existing project files without a revision-bound
   approval or transaction.
7. Timer occurrence claims and history are not one transaction. A long run can
   become immediately due again, and a crash can duplicate work or lose its
   report.
8. Nested automation definitions are loaded during execution instead of being
   frozen with the accepted run.
9. Visual failure messages can promise evidence that was deliberately not
   written.
10. The desktop-agent selector supplies `undefined` as the saved automation
    value. Its contract also polls global state rather than the submitted run.
11. Swarm execution and chat work use incompatible engines. Both rely on
    process-local lifecycle state and daemon threads.
12. Swarm reset, archive, provider rebind, membership edits, and objective
    changes do not generation-fence late provider results or file commits.
13. Swarm project work applies per-agent file transactions before the whole
    candidate is verified. A later failure leaves an unreported partial result.
14. “Nexus verification” is provider consensus, not a Nexus-owned test, build,
    file-hash, tool, or explicit-user receipt.
15. Collaboration projection truncation can permanently acknowledge past
    omitted events. The primary transcript can also delete its own prompt and
    early rounds after 40 turns.
16. The legacy shared page has no current-goal or immutable-author provenance,
    so historical work and renamed identities can enter a new goal.
17. Prose-overlap progress detection both stops genuine checkpoint progress and
    accepts irrelevant novel wording.
18. Renderer reload, server restart, transient polling failure, or HTTP
    disconnect can lose observation and recovery state while work continues.
19. Full rerenders destroy keyboard focus. Pipeline tabs, edge deletion, skip
    navigation, status announcements, and narrow-screen reflow do not meet the
    required accessible interaction model.
20. UI wording can overstate which agents will run, which checks passed, what
    Stop accomplished, and whether evidence is verified.

## Invariants

These rules are engine invariants, not UI conventions.

1. **One accepted action, one immutable identity.** Every execution has a
   server-generated `run_id`; every retried submission has a client-generated
   `request_id`; every dispatched unit has an `attempt_id`.
2. **Immutable authority.** A run snapshots canonical project identity, objective
   revision, permission grant, participant membership, provider binding epochs,
   policy, budgets, and a recursively resolved definition digest.
3. **One state owner.** Every run snapshots one immutable `authority_store_id`.
   A project run is owned by its `WorkspaceRunStore`; a projectless or
   multi-project coordinator run is owned by its `ConversationStore`. UI fields
   and in-memory flags are projections only.
4. **Fenced commits.** A provider answer, human decision, timer claim, file
   promotion, or terminal result commits only when every captured generation and
   lease still matches.
5. **At-most-one unsafe writer.** A project-writer fencing lease and OS-visible
   transactional store coordinate all processes and all entry points.
6. **No result without evidence.** `passed`, `verified`, `cancelled`, `timed_out`,
   `interrupted`, `delivery_unknown`, and `incomplete` are distinct terminal or
   paused states. None is inferred from absence of an error.
7. **No acknowledged omission.** A recipient cursor advances only through the
   exact contiguous event range and digest included in an accepted projection.
8. **Untrusted code never possesses a secret.** Workers, providers, plugins,
   browser pages, and model prompts receive opaque credential handles, not raw
   credential values. A trusted broker applies a handle only to an authorized,
   bounded request and sanitizes its typed response before release. A finite,
   versioned scanner for canonical encodings is defense in depth; an input whose
   safe capture cannot be proved is rejected rather than persisted.
9. **No silent destructive write.** Project-file mutation uses a durable
   transaction or staged workspace. Existing files require a matching baseline
   hash and an authorized mutation capability.
10. **No unsafe automatic replay.** Restart resumes only explicitly idempotent,
    checkpoint-safe work. Ambiguous external side effects become
    `delivery_unknown` for delivery or `outcome_unknown` for any other effect,
    never `interrupted` merely because evidence is missing and never a blind
    retry.
11. **Bounded does not mean lossy.** Canonical events and transcripts are
    append-only and paginated. Prompt/UI projections are bounded and cite their
    source ranges.
12. **Accessibility follows semantic identity.** Renders preserve focus by
    `{authority_store_id, project_authority_id_or_conversation_id, workspace,
    surface_kind, view_mode, run_id, definition_id,
    definition_revision, accepted_digest, entity_id, action}` and every pointer
    action has keyboard and screen-reader parity. A
    deleted identity falls back to its documented semantic ancestor and the
    move is announced.
13. **Failures are not speech.** `conversation_content` and `operational_event`
    are disjoint types. Only a validated, accepted, committed provider response
    can create an authored utterance, recipient delivery, or advance a
    turn/round/context cursor. Transport, schema, timeout, cancellation and
    recovery events belong to run status/activity/diagnostics and never appear
    as an agent bubble or model conversation evidence.

## Runtime architecture

### 1. Trusted runtime and conversation stores

Add a WAL-mode SQLite `WorkspaceRunStore` in an ACL-restricted per-user control
directory keyed by a stable registered `project_authority_id`, outside every project tree,
container mount, plugin path, test cwd, provider worktree, and artifact root. A
project may hold only a non-authoritative opaque project-ID descriptor. Store
opens reject symlink, junction, reparse-point, copied-project, and descriptor
substitution. The trusted AuthorityService binds that stable ID to verified
filesystem identity and the current canonical location; it is not derived anew
from a mutable path. Relocation/rebind takes a global CAS barrier, discovers and
fences the old authority, rejects copied descriptors, and atomically forwards
the existing store or pauses until all old leases/effects settle. Unsupported
filesystem identities or shares fail closed. Workers never receive the DB, WAL, SHM, lease secret, signing key,
credential values, or anchor journal. Keyed-MAC event-chain heads use a
protected append-only anchor journal. Each record contains `{store_id, sequence,
prior_head, new_head, db_tx_id, phase}`. The trusted store service fsyncs a
prepared anchor record, commits the matching SQLite transaction, fsyncs a
committed/finalized anchor record and directory metadata, and only then
acknowledges the transaction. Recovery compares both sides and deterministically
finalizes a proven committed pair or rolls back an uncommitted prepare; it never
blesses an unanchored DB head. A deployment unable to provide this ordering must
put DB and anchor in one trusted transactional service.

Reuse `resident.py` path/device/inode checks only to verify or rebind a registered
authority, never to derive its stable ID, and do not reuse its split command
claim/result transaction. Submission atomically inserts the unique command
claim, immutable run, accepted event, and exact replayable `202` response in one
transaction. Uniqueness is universally `(authority_store_id, request_id)` across UI,
agent, CLI, editor, and timer. The same digest replays the response; a different
digest is a typed conflict. Project authority and verified filesystem identity
remain in the immutable request digest/snapshot, not the idempotency namespace.

Conversations are opaque authorities spanning many runs. They are never derived
from names, pair order, provider routes, remote threads, or mutable project
membership. A separate user-scoped, ACL-restricted `ConversationStore` is the
canonical owner even for projectless and multi-project discussions. Each
conversation has an immutable `home_store_id`. It owns strictly ordered
`conversation_events(conversation_id, conversation_sequence, event_id, type,
source_run_id, redacted_payload, audience_epoch, prior_hash)`.

Project stores publish committed child outcomes through durable outboxes; the
conversation store consumes them through an idempotent inbox keyed by `event_id`.
No cross-store action is described as atomically committed: a crash may leave a
truthful `publication_pending` state that reconciliation completes exactly once.
Removing or moving a discussed project never changes conversation ownership.

Required project-store tables are:

- `runs`: identity, kind, immutable project/objective/definition snapshot,
  policy, state, version, lease, timestamps, terminal outcome, and redacted
  summary;
- `units`: run task/node/turn identity, dependencies, state, attempt, deadline,
  checkpoint safety, effective outcome, and evidence digest;
- `events`: append-only run-scoped sequence, type, redacted payload, provenance,
  and prior hash;
- `effect_dispatches`: globally unique effect ID, attempt-local ordinal,
  canonical operation/effect class/payload digest, external idempotency key,
  owning outbox/inbox/ack state, receipt/outcome digest, and recovery state;
- `commands`: client ID, request ID, route, request digest, and exact prior
  response for idempotent replay;
- `decisions`: run ID, unit ID, wait-attempt nonce, decision, authority, and
  committed version;
- `timer_occurrences`: timer ID, scheduled instant, run ID, lease, notification
  state, and terminal result in the same transaction;
- `mutation_sagas` and `mutation_files`: saga phase, staged workspace, baseline,
  staged, promoted and observed-current digests, ordered promotion/compensation
  steps, commit point, conflicts, and recovery requirement;
- `migration_journal`: source artifact/digest, deterministic destination IDs,
  phase, validation, cutover token, and quarantine reason.

Required conversation-store tables include `conversations`, `objectives`,
`memberships`, `provider_bindings`, `conversation_events`, publication
inbox/outbox state, `conversation_commands`, and recipient-specific `deliveries`.
Conversation command admission is unique on `(conversation_id, request_id)`;
the request digest conflicts on reuse and the exact prior response replays.
Command claim, lifecycle/objective/generation change, canonical event, delivery,
and publication intent commit atomically.

The ConversationStore also implements the same `runs`, `units/attempts`,
`commands`, `decisions`, run-events, control epochs and lifecycle schema for
projectless/multi-project coordinator runs. Submission uniqueness is
`(authority_store_id, request_id)`. A coordinator and each single-project
mutation child have immutable parent/child run IDs and independent fencing;
creating a child never moves ownership of its parent.

Coordinator-to-child dispatch uses durable
`child_dispatches(parent_run_id, parent_unit_id, dispatch_generation,
child_request_id, project_authority_id, spec_digest, state, response_digest)`.
The child request ID is stable/deterministic for that dispatch generation. A
parent outbox/project inbox exchange replays the exact child `202` or conflicts
on another digest; the parent advances only after atomically linking the child
response. No crash window may create a second mutation child.

Each delivery has its
own contiguous `delivery_sequence` and references one canonical source event.
It contains authorized content or a non-sensitive visibility record. Recipient
cursors and digests cover delivery entries, never inaccessible payloads, so a
private canonical event cannot block a later public delivery.

The user-scoped Authority/ProviderResource store holds globally unique
`provider_resource_claims`: physical account/window/thread or API concurrency
domain, capability snapshot, fair queue position, attempt, lease, heartbeat,
takeover epoch, rate-limit backoff, and pressure budget.
It also holds `effect_permits` keyed by immutable globally unique `effect_id`
(including attempt-local ordinal), attempt, physical resource, owning
`run_id`, `authority_store_id`, owning run control/effect epoch, project
authority/mutation epoch, conversation/binding generations, and optional parent
coordinator epoch. Permits transition
`reserved -> activation_intent -> outcome_known|outcome_unknown -> closed`.
Each stores canonical operation/effect-class/payload digest, external idempotency
key, and trusted receipt/outcome digest. The owning store atomically writes the
effect dispatch intent before central reservation. An idempotent owning-outbox
→ central-permit → owning-inbox/ack bridge replays/conflicts by effect ID and
digest. The permit is never closed or collected until the owning run atomically
commits its unit event and acknowledgement; crash/reorder/duplicate bridge
messages therefore converge on one canonical effect record. This guarantees
at-most-once automatic native activation, not exactly-once physical execution.
Exactly-once eventual effect is claimed only when the sink proves stable
idempotency or reconcilable receipts. An unobservable non-idempotent sink after
activation intent truthfully remains possibly-zero/possibly-one outcome unknown.
The activation-intent transaction atomically revalidates authority and registers
in-flight actuation before the native effect. Authority changes close new
admission and cannot report success until every earlier permit settles; otherwise
they remain `fencing/authority_unknown`. A crash or pause after activation intent
is `delivery_unknown` for message/notification delivery and `outcome_unknown`
for any other effect; recovery observes/reconciles it and never actuates again.

A user-scoped protected `DraftStore` serves every authority, including
projectless ConversationStores. Keys are `(authority_store_id,
project_authority_id_or_conversation_id, client_id, workspace,
artifact_or_chat_id)`. CAS revisions own text, selection, conflict records and
content-addressed attachment blobs. Blob metadata records digest, size, media
type, permission, provenance and retention; a missing/replaced source is explicit
and can never silently change staged content.

The store transactionally issues strictly increasing fencing tokens. Dispatcher
ownership has a boot identity, random lease identity, heartbeat, and takeover
phase; wall time and PID are diagnostic only. Durable command admission is
separate from dispatcher ownership. Stop can increment a run-scoped cancellation
generation while an owner is frozen. Every dispatch, brokered effect, response
commit, file promotion, and terminal transition revalidates fencing token and
control epoch.

A run may discuss several projects, but every mutation child run and saga owns
exactly one canonical project root. A coordinator may wait for child outcomes;
it never attempts a distributed atomic commit or holds several project-writer
leases.

A user-scoped trusted `AuthorityService` owns conversation fences and global
provider-resource claims. Reset, archive, rebind, membership/grant or objective
change first enters `fencing`, blocks new dispatch, revokes short-lived broker
capabilities atomically, and durably CAS-fences every active child project store.
It returns completed only after all child acknowledgements. Missing authority
leaves `fencing/authority_unknown`, never a successful reset/change. Every send,
effect and file promotion validates a current central capability immediately
before the effect and commit. The globally unique provider claim key is physical
resource ID; separate project stores can never lease the same composer/window/
thread independently.

### 2. Shared lifecycle

Both workspaces use this state model:

`submitted -> queued -> running -> waiting | paused | stopping -> terminal`

Terminal states are `succeeded_verified`, `succeeded_unverified`, `failed`,
`cancelled`, `timed_out`, `interrupted`, and `abandoned_unknown`. Paused states
include `waiting_for_person`, `waiting_budget_authorization`,
`provider_unavailable`, `delivery_unknown`, `outcome_unknown`,
`publication_pending`, `rollback_required`, and `mutation_conflicted`.

Control-plane admission and dispatcher transitions have different authority.
An authenticated Stop or other authorized control command is admitted without
the dispatcher's lease by compare-and-set on `(run_id, control_epoch)`, increments
the control epoch/cancellation generation, and appends its event atomically.
Dispatcher, unit, effect, file and terminal transitions compare-and-set
`(run_id, version, fencing_token, captured_control_epoch)`. Thus a frozen owner
cannot block durable Stop admission, and its old work cannot commit afterward.
API events always include `run_id`, sequence, object identity/version, control
epoch, and frozen node/participant identity.

Objective operations are typed `append`, `replace`, and `cancel`. They create a
new objective/frontier revision and fence incompatible outstanding units. Old
outputs may be adopted only by a recorded rebase decision citing their evidence.
Budget exhaustion enters `waiting_budget_authorization`; extension creates a new
policy revision. Required/optional roles, quorum, veto/judge rules, and degraded
mode are snapshotted, and required participants are never silently omitted.

### 3. Submission and control API

- `POST /api/runs` accepts a typed swarm or automation definition plus a
  `request_id`, chooses and freezes its `authority_store_id` and dependencies,
  and atomically returns `202 + run_id` from that authority store.
- `GET /api/runs/{run_id}` returns, from one transaction, `{snapshot,
  snapshot_version, last_included_sequence}`. Event pages name contiguous
  `from/through`, digest, and `has_more`; clients gap-check and deduplicate them.
- `POST /api/runs/{run_id}/stop` is monotonic and idempotent by request ID. The
  server CAS-retries current nonterminal state, so stale observer versions cannot
  lose an emergency stop. For every standalone or child run it first atomically
  increments/closes central effect admission for that owning run/control epoch,
  then fences undispatched units and requests every active attempt's
  cancellation. It remains `stopping/authority_unknown` until the central permit
  authority and owning store acknowledge, and reports the explicit
  Stop/completion race winner. Activation intent compares all owning and optional
  parent epochs in one central transaction.
  For a coordinator it first atomically revokes central child-effect
  capabilities, then enqueues idempotent child Stop/fence commands keyed by the
  parent control epoch. It remains `stopping/fencing` until every child
  acknowledges or becomes explicit `authority_unknown/outcome_unknown`, and
  never reports cancelled while a child effect is ambiguous. Every child send,
  effect and promotion validates the live parent capability before effect and
  commit.
- `POST /api/runs/{run_id}/decisions/{unit_id}` requires the exact current
  wait-attempt nonce. Replays return the prior result; stale/future decisions are
  rejected.
- Legacy endpoints become thin compatibility adapters and never own state.

Paused recovery is typed: `reconcile`, `retry_safe_unit`, `authorize_failover`,
`authorize_budget_extension`, `reconcile_publication`,
`resolve_mutation_conflict`, `resume_rollback`, `abandon`, and
`fork_from_checkpoint`. Every command is
idempotent, authority-checked, and versioned. Failover creates a new binding and
policy epoch that fences old work. `delivery_unknown` cannot become success
without a trusted external receipt.

Compatibility boundary (current Swarm runtime): `board_progress` and
`response_ready` are read projections, not executable continuation checkpoints.
`board_progress` does not preserve provider response bodies, local inbox state,
or the exact next scheduler unit, while `response_ready` exists only after the
entire chat workflow has completed. Therefore an interrupted compatibility run
MUST be terminal and labelled `start_over`; it MUST NOT expose Resume or
`retry_safe_unit`. Delivery/outcome-unknown runs remain reconcile-only and are
never resent automatically. Resume becomes conforming only after the runtime
durably commits, after every acknowledged provider result and before any next
effect, a complete step program containing run/generation/attempt, unit and
recipient identity, response/evidence bodies, inbox/frontier state, and the
exact next action; execution must then start a new fenced attempt at that next
unit without iterating completed turns.

Every recovery request has a closed discriminated schema with common
`{command, run_id, paused_state_version, command_nonce, request_id}`. Identity
and authority are derived from the authenticated channel; a client-supplied
actor is rejected. Command-specific bodies are: reconcile
`{effect_or_delivery_id, trusted_receipt_digest}`; retry
`{unit_id, expected_evidence_digest}`; failover `{unit_id, failed_binding_epoch,
replacement_binding_id}`; budget `{expected_policy_revision, added_budget}`;
publication `{publication_event_id, outbox_digest}`; mutation conflict
`{saga_id, ordered_files:[{path, observed_digest, choice}]}`; rollback
`{saga_id, expected_saga_version}`; abandon `{reason}`; and fork
`{checkpoint_id, checkpoint_digest, replay_frontier_digest,
duplicate_risk_authorizations}`. Unknown/missing/extra fields reject. Every
response names command, run/state versions and committed event, and returns the
exact prior response on replay. Its committed event names the old/new state,
new version, control epoch and resulting attempt/saga/checkpoint. The normative
command matrix is: `reconcile` only for unknown delivery/effect; `retry_safe_unit`
only for a checkpoint-safe failed/interrupted unit that is also side-effect-free
or uses a stable external-effect idempotency key with trusted non-delivery or
reconciliation proof; `authorize_failover` only
for a paused unavailable binding; `resume_rollback` only for rollback-required;
`authorize_budget_extension` only for budget wait; `reconcile_publication` only
for pending publication; `resolve_mutation_conflict` only for a mutation
conflict with exact file digests and explicit keep-external/restore-saga choice;
`abandon` for any nonterminal paused state; and `fork_from_checkpoint` only from
a verified immutable checkpoint whose entire replay frontier is side-effect-free,
stable-key idempotent, or reconciled. A still-ambiguous/non-idempotent effect is
excluded unless a specifically authorized duplicate-risk record is attached;
the old run remains unknown. All other state-command pairs reject without
mutation. A stale version/nonce/digest rejects. A race with Stop, provider
completion or rollback has exactly one compare-and-set winner and records the
loser; failover always fences the old result. Budget extension creates a new
policy revision; publication reconciliation is outbox/inbox-idempotent; conflict
resolution never overwrites a digest it did not authorize. Abandon revokes
capabilities and fences future commits, but a dispatched ambiguous effect becomes
immutable terminal `abandoned_unknown`, never cancelled or safe. A later receipt
creates a linked adjudication/successor record and never rewrites that terminal
truth. Non-abandoned `outcome_unknown` remains paused and reconcilable.

`abandon` is rejected while a mutation saga owns a promoting, rollback,
conflict, or recovery barrier; saga recovery must reach a stable project epoch
first. A committed publication intent is independently durable: abandoning its
parent never deletes or disguises it, and outbox reconciliation continues under
its own identity until known or explicitly adjudicated.

Every panel, timer, CLI, editor, desktop agent, and swarm action submits through
the same service. Project switching checks this service, not a server-local flag.

### 4. Dispatch, deadlines, and cancellation

A typed execution context supplies run/attempt identity, immutable config,
deadline, cancellation token, evidence sink, and resource lease to every handler.
Process handlers create a process group, register tree termination, kill, wait,
and close pipes. Browser handlers close their exact context. Provider and
Electron bridges use logical turn/attempt IDs and explicit cancellation messages.

Every provider bridge persists the attempt phases `prepared -> filled ->
activated -> acceptance_proved -> streaming -> response_received -> committed`.
Acceptance requires a remote receipt or exact attempt marker attributable to the
expected conversation, binding epoch and physical resource. Filled text, an
enabled Send button, an activation call, disappearing composer text, a spinner,
or an unattributed DOM mutation is not acceptance. Ambiguous activation/ack
enters `delivery_unknown`, retains or quarantines the physical-resource claim,
and forbids resend until reconciliation. Web adapters revalidate the
composer-local native target immediately before native activation; a synthetic
click/Enter is never itself an acknowledgement. Marker text left in a composer
is detected and quarantined rather than submitted as a duplicate.

When a deadline or Stop wins the terminal compare-and-set, later completion is
diagnostic evidence only and cannot replace `cancelled` or `timed_out`.
Every mutating unit runs in an OS-enforced isolated worktree/sandbox or through a
fenced capability broker; providers are advisory unless granted such a brokered
capability. Live project and control paths are denied. A handler that cannot prove
cancellation declares its external side-effect class. Once an ambiguous effect is
dispatched, Stop stays `stopping` or becomes `outcome_unknown` until reconciled;
it never claims `cancelled`. Process cancellation proves descendant-tree
termination and inherited-handle closure.

The immutable execution manifest includes handler/plugin code, resolved
executable or container image, command, QA suite/baseline/dataset, browser/runtime,
environment allowlist, provider/tool/model epochs, and every local input digest.
Inputs are content-addressed or copied into isolation. Dispatch pauses on an
unapproved mismatch.

Evidence ingestion is typed. Untrusted components receive only opaque secret
handles; a trusted credential broker applies credentials to allowlisted request
fields and sanitizes structured responses before release. Defense-in-depth
scanning covers a finite, versioned set of canonical encodings across chunk
boundaries. It bounds and decompression-checks binary inputs, sanitizes
filenames/diagnostics, excludes secret DOM fields, and OCR/masks screenshots or
fails closed when safe capture is unproved. Receipts bind sanitized evidence to run,
attempt, source tree, execution manifest, command, environment, timestamps, and
output digest before the external chain anchor.

### 5. Automation semantics

At acceptance, recursively resolve every nested automation and persist its
canonical snapshot/digest. Reject cycles and an empty executable selection.
Use one canonical effective outcome for dependency blocking, gates, and final
aggregation:

- `pass`: executed and satisfied;
- `warning`: flaky or explicitly allowed failure;
- `incomplete`: skipped/not-run/no evidence;
- `fail`: executed and unsatisfied;
- `cancelled` or `timed_out`.

Default gates require real executed passes and reject `warning`/`incomplete`
unless the saved policy explicitly permits them. DAG branches remain serialized
unless their handlers declare compatible read/write resource claims; the UI must
not claim parallel execution until that proof exists.

QA subruns always receive the parent `run_id`, persist redacted full reports and
visual current/diff artifacts, and expose stable evidence links. Artifact nodes
default to a run-owned artifact namespace with atomic no-clobber creation.
Writing elsewhere requires a mutation grant, matching expected hash, and a
rollback transaction.

Timer scheduling claims `(timer_id, timer_revision, scheduled_at_utc, fold)` and
run in one transaction. It snapshots IANA timezone, tzdb version, DST fold/gap
meaning, and explicit skip/coalesce/catch-up misfire policy. The cursor advances
from the claimed occurrence, not scan time. Notification uses a durable outbox
of immutable intents containing `(occurrence_id, notification_action_id,
destination_revision, sanitized_payload_digest, credential_handle_id)` and a
globally unique deterministic provider idempotency key. Retries replay the exact
sanitized bytes and destination captured with the occurrence even if settings
later change. If the provider cannot prove idempotency-key semantics, one
ambiguous attempt becomes `delivery_unknown` and is never automatically retried.

### 6. Swarm semantics

Converge legacy “Set them going,” direct chat, relay, collaboration, and project
work on durable run/task/turn records. Before dispatch, show and snapshot the
exact included/excluded participants, physical provider bindings, projects,
communication grants, finite/unlimited round policy, and independent wall-time,
transport-retry, file, byte, transaction, disk, and spend budgets.

Every dispatch captures opaque conversation ID/generation, objective/frontier revision,
membership and grant revisions, provider binding epoch, and project authority.
Reset, archive, rebind, topology edits, and objective changes increment the
appropriate fence before returning. Late results cannot commit.

Provider contribution is a `peer_claim`, never a deterministic receipt.
Project work is applied in a staged workspace or recorded run-level mutation
saga. Nexus runs configured deterministic checks and file/hash acceptance
criteria before promotion. Without such receipts the truthful result is
`succeeded_unverified`; it is never labelled “Nexus verified.” Failure or
cancellation rolls back the whole saga or exposes the exact conflict and
recovery action.

Progress is based on canonical task transitions and evidence hashes—resolved
dependency, changed artifact digest, new test receipt, user decision, or task
state—not lexical novelty. Physical provider/account/thread identity is part of
judge independence; two display names on one provider thread are not independent.

All stateful provider dispatch passes through durable provider-resource claims.
The claim key is the physical account/window/thread or declared API concurrency
domain. Stateful web bindings default to one active attempt. A fenced FIFO/fair
queue owns attempt leases, heartbeat/takeover, bounded pressure and rate-limit
backoff; generation/binding epochs are revalidated immediately before send and
before commit. A crashed composer owner cannot permit another prompt to enter
until takeover has fenced and reconciled the old attempt.

Mutation sagas use the executable phases `prepared -> applying -> applied ->
verifying -> promoting -> promoted`, plus `rollback_required -> rolling_back ->
rolled_back`, `conflicted`, and explicit failed/interrupted terminals. Every file
persists baseline, staged, promoted and observed-current digests and an ordered
step receipt. Promotion has one durable commit point after all staged checks and
before the first canonical replacement. Recovery has one prescribed action for
every phase. Compensation changes a canonical file only if its current digest
still equals that saga's promoted digest; otherwise the external edit is
preserved and the saga becomes `conflicted` with exact paths/digests.

Each project also has a durable `mutation_epoch` and recovery barrier. Run
admission and read manifests pin only a stable epoch. Before the first canonical
replacement, the broker holds the exclusive writer lease, revalidates every
baseline, increments the epoch into `promoting`, and blocks all trusted readers
and runs from starting or continuing on that project. Prefer an atomic root
swap. If individual replacement is unavoidable, the project remains quarantined
through every partial `promoting`, `rollback_required`, `rolling_back`, or
`conflicted` phase and is exposed again only after a fully `promoted` or
`rolled_back` stable epoch. No trusted consumer observes a half-promoted tree.

Resolving a mutation conflict creates a new candidate/baseline and mutation
revision. Any chosen digest invalidates all earlier verification receipts, and
the complete configured acceptance suite reruns before a stable epoch is
published. A mixed or keep-external result is `succeeded_unverified` only with
explicit authorization, or verified only by fresh receipts. Failed checks keep
the project quarantined in rollback/recovery.

The shared page and transcript become projections of canonical conversation and
run events. Each
entry carries immutable author ID, goal/objective revision, audience grant, and
source event. Legacy page material is historical/unverified and excluded from a
new goal unless explicitly retrieved.

That combined view preserves type boundaries: conversation content is rendered
as authored speech; operational events are a separate status/activity stream.
A failed invocation consumes no conversational turn/round/progress and enters no
recipient prompt. Successful reconciliation may commit exactly one attributable
utterance; the preceding failure remains non-conversational diagnostics.

Membership is epoch-based: join/leave boundaries and explicit history grants
control pre-join and rejoin visibility. Derived artifacts inherit the intersection
of all source audiences. Revocation fences future delivery without rewriting
historical audit evidence.

Remote provider threads are noncanonical caches. Requests and receipts carry
Nexus conversation/run/attempt/generation/binding markers; mismatches rotate or
quarantine the binding. Manual remote messages are `external_content`, never user
authority.

The deterministic context compiler always frames conversation, current objective,
generation, membership, binding, policy, and budget revisions. Per recipient it
separates prepared recipient-delivery range, transport acceptance, response
receipt, and committed delivery cursor. Only whole contiguous delivery entries
advance; each references its canonical conversation event and contains authorized
content or a visibility record. A single over-budget entry is losslessly chunked
with subrange digests. Summaries cite source ranges/digests and
never replace unresolved dissent or decisions. Raw events remain paginatable.

### 7. UI projection and information architecture

Maintain three explicit identities:

- `editing={authority_store_id, project_authority_id_or_conversation_id,
  definition_id, revision, digest, draft_revision}`: the board or
  automation being changed;
- `following={authority_store_id, project_authority_id_or_conversation_id,
  run_id, accepted_definition_revision,
  accepted_definition_digest}`: the immutable snapshot whose live state is
  displayed;
- `history={authority_store_id, project_authority_id_or_conversation_id,
  run_id, accepted_definition_revision,
  accepted_definition_digest}`: a selected terminal report.

Definition IDs are scoped, never globally unique. Every focus key, renderer
cache, reconnect cursor, notification deep link, draft key and control request
also includes the same authority-store and project-authority/conversation scope.

Never paint a run on a different definition or revision. If the definition
changes while its run is active, freeze the run canvas on its immutable snapshot
or remove the overlay and show “Run uses revision N — View snapshot / Compare
changes.” When another object is active, show a compact “Automation A is running
— View run” banner.

Both workspaces use a shared run header with plain state, last confirmed time,
current unit/checkpoint, next automatic action, primary action, recovery action,
elapsed time, last meaningful progress, retry attempt, consumed/remaining budgets,
and collapsed diagnostics. Transient polling failures enter `Status unknown —
reconnecting` with bounded backoff and authoritative rehydration; they never
clear observation or infer completion.

Projection-channel and command/control-channel health are independent. If only
projection is down, stable-request-ID Stop remains available and retryable. If
command authority is unreachable, the UI says “Stop not accepted — work may
continue.” A separately labelled local-isolation action can close a local
browser/process or revoke a local capability, but never claims remote
cancellation.

The default Visual automation path is template/selection, Run, and activity.
JSON, desktop-agent contract, schedules, notification setup, and history move
under Advanced. The Swarm inspector orders connection, project authority,
communication, run policy, then collapsed appearance. Destructive actions use
danger styling and separation.

Draft text, attachments, caret/selection, unsaved definition changes, and
selected chat survive reload, switch, and restart in the separate optimistic
draft store. Two editors surface a conflict; neither overwrites silently.

Run never ambiguously chooses between a dirty editor and its saved definition.
The primary action atomically save-and-runs the exact visible draft digest (with
optimistic saved revision), or the user explicitly chooses “Run saved revision
N.” A stale save conflict starts no run. Scope preview and accepted event cite
the exact draft/saved digest and authority edits used.

Implement the ARIA tab pattern: linked tabs/tabpanels, one tabbable tab,
Left/Right/Home/End, and focus preservation. Add a real active-workspace main
landmark and “Skip to main content.” Edge deletion must have a focusable named
button/menu action. Announce only meaningful run transitions once.

Each graph has a synchronized semantic structure view listing every entity,
state, inbound/outbound dependency or membership/grant, and action. Dialogs and
drawers define focus entry, trap, Escape, inert background, and return target.
Forced colors, contrast, target size, reduced motion, and secret-safe local
notifications for waits, pauses, conflicts, and completion are release criteria.
Notifications deep-link to the exact run and accepted revision. A stale action
cannot target another run and dismissing one never changes orchestration state.
An independent durable activity inbox groups `Needs attention`, `Running`, and
neutral `Finished` runs and shows last confirmation/progress, budget and provider
health, exact run/revision links and filters. It remains complete when OS
notifications are denied or dismissed. Bulk actions are read-only/navigation;
control commands always target and confirm one run.
The state-to-bucket table is exhaustive: submitted/queued/running/stopping are
`Running`; every waiting/paused/recovery state plus failed, timed_out,
interrupted, and abandoned_unknown are `Needs attention`; succeeded_verified,
succeeded_unverified, and cancelled are `Finished`. Badges remain exact:
Verified, Unverified, Cancelled, Failed, Timed out, Interrupted, or “Abandoned in
Nexus — external effect may have occurred.” Finished never means correct, and
each Needs-attention row has one truthful primary action and alternatives.

Draft conflicts are durable records containing both revisions, digests,
attachments and provenance. Accessible actions are Compare, Accept theirs,
Duplicate mine, and CAS Merge/Keep mine. No action discards either side before
an audited resolution or export; restart/replay resumes the same conflict and
optimistic versions prevent a third editor from being overwritten.

At 320/375 px, 200%, and 400% zoom, navigation wraps, the canvas owns its own
scrolling, sidebars become drawers/overlays, timeline columns reflow, and the
document has no horizontal overflow. Fullscreen collapses rather than reserves
fixed sidebars.

## Migration and delivery order

1. Add regression tests for every proven counterexample before changing behavior.
2. Ship a phase-zero bridge that persists current in-memory composer/settings
   drafts before updater restart. If it cannot authenticate an exact text,
   attachment, caret, selection, and unsaved-definition handoff, block restart
   and offer explicit export/import; never silently discard it. Inventory and
   version every legacy artifact. Create checksummed immutable
   backups, deterministic legacy-to-v2 IDs, validation/quarantine rules, and a
   restartable migration journal. Import is copy-and-verify. One exclusive
   cutover transaction writes an ownership/format token. After cutover, only
   version-negotiated v2 service clients are supported. The installer acquires
   an OS-visible exclusive legacy-namespace lock, moves imported legacy roots to
   immutable backup, and installs a non-writable tombstoned namespace/ACL where
   the platform can enforce it. Already-running pre-v2 binaries are not claimed
   to understand the token: their delayed writes are denied when enforceable,
   otherwise detected as post-cutover drift and quarantined, and can never be
   imported into canonical v2 state. Old clients contacting v2 receive a typed
   incompatible-version response. Rollback is allowed before cutover; recovery
   is forward-only after it, with tombstones preventing resurrection across
   downgrade/re-upgrade.
   An accessible Migration recovery surface lists each quarantined item's
   source, digest, time and reason and offers safe View, Compare, Export, Discard,
   or resubmit-as-new-draft actions. It never auto-imports post-cutover output.
   Ambiguous legacy state maps to non-success.
3. Introduce the shared durable store, immutable snapshots, run-scoped API,
   redaction boundary, and compatibility adapters.
4. Route every automation entry point through the service; fix result semantics,
   scheduler occurrences, evidence, artifacts, cancellation, and agent contract.
5. Route swarm work through durable run identity and generation fences; fix
   projection cursor ranges, append-only transcripts, current-goal provenance,
   mutation saga, deterministic receipts, and progress transitions.
6. Separate UI editing/following/history state; add reconnect and local command
   status; then complete keyboard, focus, responsive, and information-architecture
   work.
7. Remove old global state and legacy runner ownership only after compatibility
   and migration tests prove no user data is orphaned.

## Release gates

Independent judges must reject release for any of the following:

- a stale event/control/result changes another run;
- any entry point bypasses the coordinator or immutable project binding;
- a credential canary reaches any durable or external sink;
- Stop/deadline permits a late pass or unlabelled continuing side effect;
- empty, skipped, flaky, or optional-failure work is misreported;
- restart/disconnect silently repeats work or loses a human wait;
- a late provider answer commits after reset/rebind/objective revision;
- project changes remain partially applied without an explicit recovery state;
- provider consensus is labelled verified;
- a projection cursor passes omitted content or canonical history is truncated;
- an automation run appears on another automation;
- a polling failure freezes false UI state;
- keyboard focus is lost on refresh or a pointer-only operation remains;
- document-level overflow appears at required widths/zoom;
- any scoped deterministic counterexample remains reproducible.

The detailed executable acceptance matrix and independent judge protocol live in
`NEXUS_WORKSPACE_RUNTIME_ACCEPTANCE.md` and
`NEXUS_WORKSPACE_RUNTIME_JUDGE.md`.
