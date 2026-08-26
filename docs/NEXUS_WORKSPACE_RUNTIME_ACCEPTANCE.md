# Nexus workspace runtime v2 acceptance matrix

Every case records source revision, project identity, run ID, request ID, exact
fault point, terminal state, event digest, durable evidence paths, and cleanup.

## Identity and coordination

- Submit the same request ID concurrently from UI, agent, CLI, editor, and timer:
  exactly one run exists and every caller receives its ID.
- Hold each entry point mid-run and attempt every other entry point and a project
  switch: unsafe overlap is rejected or safely resource-serialized; no report is
  written under another project.
- Run A while viewing definition B: no A node, log, summary, or event appears on
  B. The banner links to A.
- Run revision 1 while revision 2 of the same definition moves, deletes, renames,
  and reuses node IDs. Revision-1 state never paints on revision 2; View snapshot
  and Compare changes bind to the accepted revision/digest.
- Delay Stop and a human decision from A until B starts: both are rejected and B
  is unchanged.
- Crash after every submission SQL statement/commit: retry atomically finds the
  one run/response or creates it once; no command is detached or permanently
  uncertain.
- A malicious command/provider/plugin attempts trusted-store files and key read,
  rewrite, delete, link swap, and copied-project replay. State remains unreachable
  and tampering/replay is detected.
- Freeze/kill the owner, change wall clock, reuse a PID, take over, then release
  stale work. Old effects/commits are fenced while Stop still advances control.
- Freeze the dispatcher, admit Stop from a second process, and crash the
  submitter before/after every SQL boundary. Release/take over the owner: Stop is
  observable and no old effect or terminal success commits.
- Stop a standalone and direct child run at reserved/activation-intent permit
  boundaries while either central authority or project store is frozen. Owning
  control/effect epochs close centrally; stale activation cannot occur and Stop
  remains explicitly unknown until acknowledgements settle.
- Crash before/after DB commit and every anchor-journal write, fsync, finalize,
  and directory-sync boundary. Recovery distinguishes pending commit from
  tamper/truncation and never accepts an unanchored DB head.
- Rename/move a project during queued/running/waiting/promoting work and submit
  through old/new paths and aliases from two processes. Relocation forwards or
  pauses one stable project authority; it never creates a second writer or
  wrong-path artifact/history. Unsupported identities/shares fail closed.
- Submit one stable request ID immediately before/after path or supported-volume
  relocation. The exact prior `202/run_id` replays from the authority store; a
  changed request digest conflicts and never creates another run.

## Restart and delivery fault injection

Crash before dispatch, after durable dispatch intent, after provider acceptance,
after response, before commit, during retry wait, at a human gate, during Stop,
and during terminal persistence. Each case resumes only if safe or ends in an
explicit paused/interrupted/unknown state; no completed side effect is silently
repeated.

For the current Swarm compatibility runtime, crash an accepted board and chat
run between effects. Assert the result is terminal `interrupted`,
`resumable=false`, `recovery_action=start_over`, and the UI explicitly says to
start a new request. It must not expose Resume or describe `board_progress` or
`response_ready` as safe continuation state. A future Resume implementation
passes only when a complete step checkpoint is committed after every
acknowledged provider result, recovery creates a new fenced attempt/generation,
starts at the exact next unit, and deterministic tests prove no completed
provider turn is repeated. Delivery/outcome unknown remains reconcile-only with
no automatic resend.

For every web/provider phase, reproduce filled-but-not-sent,
accepted-but-response-lost, marker left in composer, duplicate/virtualized Send
controls, localization, slow generation, and renderer/owner restart. Only an
attributable remote receipt/attempt marker proves acceptance; ambiguous
activation holds the physical claim in `delivery_unknown` and never resends.
- Pause after authority validation but before native activation, and crash after
  activation before phase persistence. The durable activation-intent permit
  blocks a successful reset/rebind and recovery never re-clicks.
- Emit two non-idempotent effects from one attempt and inject crash, reorder and
  duplication before/after owning intent, central reserve, activation intent,
  native effect, central outcome, owning inbox/event commit, acknowledgement and
  permit close. Unique effect IDs/ordinals and digests yield one canonical record
  and no automatic duplicate; known outcomes are not stranded, delivery vs
  other-effect uncertainty is typed, and permits remain until owner acknowledgement.
  An unobservable non-idempotent sink truthfully permits zero-or-one physical
  effect; eventual exactly-once is asserted only for sinks proving stable
  idempotency or reconciliation.
- During one team round inject filled-not-sent, delivery unknown, provider
  unavailable, malformed/schema error, timeout and recovery. Conversation and
  recipient cursors, round count and progress do not advance; no error is an
  agent bubble or prompt content, one plain operational status appears, and a
  successful recovery creates exactly one attributable utterance.

Reload the renderer and sever the observing HTTP connection at each point. A new
renderer reattaches by run ID and cursor. One or ten transient poll failures show
status unknown, keep controls conservative, and recover automatically.

Independently fail GET/projection, POST/control, and both channels. Stop remains
retryable when observation alone is down. Lost Stop requests/responses before
and after commit replay by stable request ID. When control authority is
unreachable the UI states that work may continue; local isolation never claims
remote cancellation. Repeat through coordinator restart.

Commit between snapshot read/serialization; lose, duplicate, reorder, and
overflow reconnect pages. Recovery remains gap-free and unsafe controls stay
conservative until snapshot and event tail reconcile.

## Automation correctness

- Empty or all-nonexecuted selection is rejected.
- Pass, fail, skipped, flaky, allowed failure, timeout, cancellation, and nested
  outcomes feed ordinary dependencies, every gate kind, and final aggregation
  through the same effective-outcome table.
- Freeze nested definitions, edit/delete the saved nested automation mid-run,
  and prove the active run uses its accepted digest.
- For every node kind, stop and deadline tests prove transport/process closure or
  the explicit non-cancellable/unknown state. A late success never wins.
- Cancel file, descendant-process, browser, plugin, provider, notification, and
  brokered effects before/during/after effect and at promotion. Ambiguous work
  never becomes cancelled or success.
- Two timer scans claim one occurrence once. Crash around occurrence/result/
  notification commit and prove no blind duplicate and no missing report. A run
  longer than its interval does not immediately claim skipped historical slots.
- Artifact writes are no-clobber by default. Authorized overwrite requires the
  exact baseline and produces a verified rollback transaction.
- Visual failure always has redacted report, current image, diff image, and
  stable run-scoped links.
- Mutate plugin code, PATH target, image digest, QA suite/baseline/dataset,
  browser/runtime, environment, and provider/tool version after submit. Frozen
  content runs or a manifest mismatch pauses.
- Exercise DST gap/fold, timezone/tzdb/schedule edits, clock rollback, downtime,
  concurrent scanners, and every notification-outbox crash boundary.
- Edit/delete/rebind notification settings between claim/send/retry, use two
  actions/channels, and test providers with/without proven idempotency. Each
  intent replays the exact captured bytes/target/key; ambiguous non-idempotent
  delivery is never retried automatically.

## Redaction

Emit distinct canary credentials through passing/failing command stdout/stderr,
HTTP/browser evidence, provider replies, plugin results, nested runs, artifacts,
events, last-run/status APIs, CLI JSON, timer history, notifications, shared
pages, transcripts, and crash messages. Scan durable files and responses; no
canary or reversible encoding is present.

Include split chunks, encoded strings, filenames, compressed/binary payloads,
secret fields, screenshot pixels, and crash diagnostics. Forged, replayed,
rewritten, and copied receipts/store records fail integrity checks.

Give malicious workers/providers/plugins only opaque credential handles and
attempt arbitrary transform/exfiltration. Prove those processes never possessed
the raw canary. Exercise every encoding in the versioned defense scanner; any
unprovably safe binary, DOM or screenshot capture fails closed. Do not require a
scanner to recognize an unbounded universe of reversible encodings.

## Swarm authority and long horizon

- Block a turn, then reset/archive/rebind/remove membership/revoke project
  authority/revise objective. Release the old turn and prove it cannot commit.
- Run simultaneous chats for the same pair; rename/rebind/archive/reopen and
  prove opaque conversation identity, generations, history grants, and isolation.
- Join, leave, revoke, and rejoin participants. Mixed-audience summaries never
  broaden source disclosure.
- Interleave private and public canonical events, revoke/rejoin, change a grant
  during prepared delivery, and crash around visibility-entry/cursor commit.
  Each recipient's delivery stream remains contiguous without revealing private
  payloads or blocking later public entries.
- Use duplicate display names and renamed agents across goals. Only immutable
  current-goal, authorized events reach a recipient.
- Generate projections larger than the context budget. Repeated accepted
  projections deliver every event exactly once in contiguous order and never
  advance past omitted data.
- Chunk one event larger than a prompt budget, crash between prepare/accept/
  response/cursor commit, and vary recipient cursors independently.
- Run more than 40 turns and beyond every projection page. The original request
  and all contributions remain paginatable/searchable; prompt projections remain
  bounded and source-linked.
- Fail/cancel each executor and verifier after earlier file proposals. The full
  mutation saga is rolled back or exposes exact resumable transaction IDs; no
  silent partial implementation remains.
- Script unanimous false provider claims against broken code. The run cannot
  become `succeeded_verified` without matching Nexus receipts.
- Vary checkpoint/task/evidence IDs while keeping prose similar, and vary prose
  while state is unchanged. Only canonical state/evidence transitions count as
  progress.
- Put two agents on the same physical provider thread and require independent
  judgment. The engine rejects independence or allocates isolated bindings.
- Persist a finite run policy, restart, and prove it never broadens to unlimited.
- Exhaust every budget and test required/optional roles, quorum/veto, extensions,
  objective append/replace/cancel, and recorded rebase of old output.
- Replace/redirect a provider thread, use a wrong account, insert manual remote
  text, and vary localized UI. Mismatch quarantines the binding; external text
  never gains authority.
- Mutate two project roots from one request. Each mutation stays a separately
  fenced single-project saga; restart cannot create hidden cross-project success.
- Create a projectless conversation, remove/move its original discussed project,
  and publish multi-project child completion while crashing before/after the
  conversation inbox/outbox commit. Conversation lifecycle events retain one
  canonical sequence and publication completes exactly once.
- Concurrently submit/retry/Stop/restart projectless and multi-project coordinator
  runs. One ConversationStore authority creates one coordinator run/control
  epoch; linked single-project children remain independently fenced.
- Freeze/crash each child before/after central revocation and child Stop commit;
  lose/retry the parent Stop response, then release stale sends/promotions. No
  effect commits after accepted Stop and parent aggregation stays stopping or
  explicitly unknown until every child is truthful.
- Crash before/after every parent-outbox/send/project-commit/response-inbox step.
  The stable child request ID creates and links exactly one mutation child.
- Block conversation A mid-composer and queue B/C on the same stateful physical
  binding. Crash/take over the owner and apply rate limiting. Prompts/replies do
  not interleave or cross-attribution, and the bounded fair queue does not starve.
- Crash and independently edit files before/after every saga promotion and
  compensation step. Rollback changes only content matching the saga's promoted
  digest; an external edit is preserved and produces an exact `conflicted` state.
- Resolve one conflict with mixed keep-external/restore choices whose combined
  tree fails checks. Prior receipts are invalid, the full suite reruns, and the
  project remains quarantined instead of becoming verified/stable.
- Freeze a child project store, then reset/archive/rebind/revoke/revise objective
  and attempt stale sends/promotions at every authority-barrier crash point. The
  command remains `fencing/authority_unknown` until every child is fenced; no
  stale effect commits after a successful response.
- Submit projects A/B to the same physical provider tab through separate project
  stores. The user-global claim serializes them without prompt/reply mixing.
- Lose/retry every conversation command response at each SQL boundary. Same
  request/digest returns the prior result; a different digest conflicts; no turn,
  generation, objective, delivery or outbox duplicates.
- Start a trusted run/read at each file-promotion crash boundary. It sees a
  stable pre/post mutation epoch or is quarantined; never a partial tree.

## Recovery command protocol

- For each paused-state × recovery-command pair, assert the normative allowed or
  rejected result and prove a rejected command performs no mutation.
- Execute every discriminated recovery payload with missing/extra/wrong-type
  fields, unauthenticated/unauthorized actors, same/different replay digests and
  races. Server-derived identity is recorded; command-specific amounts, IDs,
  digests, file choices and checkpoint frontier are exact.
- Exercise lost response after commit, same nonce with same/different digest,
  stale version/nonce, two competing renderers, and races with provider
  completion, rollback completion, and Stop.
- Prove failover fences old results, `delivery_unknown` cannot become success
  without a trusted receipt, and every paused state exposes exactly one truthful
  primary action plus explicit alternatives.
- Cover budget wait, publication pending, mutation conflict, outcome unknown and
  abandoned unknown in the state-command matrix and every crash/race boundary.
  Terminal abandoned uncertainty is never rewritten; late evidence creates a
  linked adjudication record.
- Mark an effectful unit checkpoint-safe, commit its external effect, and lose
  the response. Retry is rejected unless stable-effect idempotency plus trusted
  non-delivery/reconciliation proof exists; no duplicate effect occurs.
- Abandon unknown delivery/effect before a late receipt. The run says immutable
  terminal `abandoned_unknown` and may still have occurred, never cancelled/safe;
  late evidence creates a linked adjudication and cannot rewrite the terminal.
- Attempt abandon/restart during every promoting/rollback/conflict phase. It is
  rejected until the saga exposes a stable epoch; a publication intent remains
  independently visible/reconcilable and no terminal run hides the barrier.
- Commit a non-idempotent effect after a checkpoint and lose its receipt. Fork
  rejects until the replay frontier is reconciled or an exact duplicate-risk
  authorization is recorded; the old run remains unknown and no risk is hidden.

## Accessibility and usability

- Keyboard-only and NVDA: create/select, connect/disconnect, preview, run, stop,
  recover, answer a gate, inspect evidence, and return to the editor.
- Tabs implement roving focus, linked tabpanels, and Left/Right/Home/End.
- Every edge action has keyboard/screen-reader parity. Skip-to-main targets the
  active workspace.
- Use the semantic structure view to reconstruct a nontrivial graph and identify
  a broken relationship without sight. Verify forced colors, contrast, reduced
  motion, target size, drawer/dialog focus trap, Escape, and return focus.
- Preserve semantic focus through 100 live events, background provider refresh,
  save, zoom, fullscreen, deletion, and recovery. Deleted targets use an
  announced deterministic fallback.
- Switch between two definitions with identical entity IDs and delete/recreate
  an ID across revisions. Focus never jumps across definition/revision identity.
- Clone the same definition/node IDs/revision/digest into projects A/B, create
  conflicting drafts and runs, then switch/reload/back/reconnect. Overlay, focus,
  draft, cursor, notification and control stay in the exact authority scope.
- Switch live/history between two runs of the same accepted definition revision
  with conflicting state, evidence, and budgets. Focus, controls and overlays
  remain bound to the exact view mode/run ID/digest.
- At 320, 375, 768, and 1024 CSS px and at 200%/400% zoom: no document horizontal
  scrolling; primary state/action/recovery remain visible; dialogs and drawers
  remain operable.
- A ready-made automation starts within three primary interactions. A fully
  ready swarm starts in one primary interaction after an exact scope preview.
  Exclusions always require acknowledgement.
- First-time users connect a provider, grant one project, and create/run one
  useful automation and two-agent board without documentation. After eight
  simulated hours they identify completion, blockers, evidence, budgets, and
  safe next action without opening diagnostics.
- With a preregistered novice participant profile, at least 90% complete a simple
  first run within five minutes and without a critical authority/error-state
  misconception. After an eight-hour absence, at least 90% identify what
  completed, what failed/paused, whether work may still run, and the safest next
  action within two minutes. Report task time, comprehension and critical errors,
  not only completion.
- Secret-safe notifications open the exact run and accepted revision. Stale
  notification actions cannot control another run; dismissal has no orchestration
  side effect.
- Draft text, attachments, caret, selected chat, and unsaved definition survive
  reload/restart/navigation and delayed answers. Two windows surface conflicts.
- Dirty-edit nodes, participants and grants while the saved revision changes in
  another window. Save-and-run either accepts the exact visible digest atomically
  or starts nothing with a conflict; explicit saved-revision Run names it.
- Create two-window text, attachment and definition conflicts, restart midway,
  replay a resolution, and introduce a third edit. Keyboard/NVDA users can
  Compare, Accept theirs, Duplicate mine, or CAS Merge/Keep mine; both versions
  remain recoverable/exportable until the audited resolution commits.
- Repeat draft/attachment/conflict/restart tests for a projectless conversation.
  The user DraftStore preserves exact authority scope and CAS behavior without a
  project store.
- Stage attachments, then delete/move/replace same-named temp sources and restart
  or migrate. The protected content digest survives exactly, or the draft shows
  a precise missing/permission/retention conflict.
- With notifications denied/dismissed, restart after eight hours with 50 mixed
  runs. The durable activity inbox lets the novice find every needs-attention and
  still-running item and its safest next action within the registered threshold;
  no bulk control can accidentally target several runs.
- The 50-run fixture includes every lifecycle state and asserts exact group and
  badge counts. Active states are Running; every paused/recovery state plus
  failed/timed-out/interrupted/abandoned-unknown needs attention; verified,
  unverified and cancelled are neutrally Finished with no correctness inference.
  Abandonment plainly warns that an effect may have occurred.
- Reload on a duplicate/reordered 100-event backlog. The live region announces
  one current-state summary, then each new meaningful transition exactly once by
  run ID/sequence; historical diagnostics/progress chatter remains silent.

## Migration and compatibility

- Upgrade every supported legacy pipeline/version/history, timer, swarm board,
  registry, archive, transcript, page, mailbox, last-run, and draft fixture,
  including renamed/duplicate identities, corrupt files, and stale backups.
- Crash after every inventory/import/validation/cutover transition. Restart is
  idempotent, dual writers never coexist, and deletions do not resurrect.
- Exercise old UI/CLI and legacy boolean callers around cutover. Stale writers
  cannot alter canonical v2 state and ambiguous outcomes never map to success.
- Hold the actual prior packaged UI/CLI open across cutover, including delayed
  rename/write handles, then downgrade/re-upgrade. OS-denied writes or detected
  drift enter quarantine and never become canonical; old clients cannot report a
  successful v2 save. Do not claim an old process self-refuses a token it cannot
  understand.
- Have that stale binary write unique content after cutover. A keyboard/NVDA
  user finds its source/digest/time/reason in Migration recovery and can View,
  Compare, Export or resubmit it as a new draft without changing canonical state;
  Discard is explicit and audited.
- Upgrade with active composer text, attachments, caret/selection, settings and
  unsaved definition. The phase-zero handoff restores them exactly or blocks
  restart with authenticated export/import, including a concurrent-definition
  conflict.
- Roll back before cutover and forward-repair after cutover without orphaning or
  duplicating canonical data.

## Regression and soak

- Run all Python, desktop, packaging, and project checks without warnings caused
  by leaked processes/pipes.
- Soak mixed swarm/pipeline observation and safe scheduling for 24 simulated
  hours with injected delays, reordered replies, duplicate commands, renderer
  reloads, and controlled process restarts.
- Run model-independent property tests for state transitions, idempotency,
  cursor ranges, fencing, effective outcomes, and timer occurrence arithmetic.
