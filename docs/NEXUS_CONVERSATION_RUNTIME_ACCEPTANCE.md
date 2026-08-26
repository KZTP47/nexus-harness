# Nexus Conversation Runtime v2 — Acceptance and Judge Gate

This document is release-blocking. A design or implementation passes only when every
critical criterion is satisfied. “Mostly,” “planned later,” model consensus, or a
convincing demo cannot waive a critical failure.

## 1. Independent judge contract

The judge must be independent of the artifact authors and blind to agent prestige. It
reviews the normative architecture, current repository constraints, produced evidence,
and unresolved dissent. It assumes crashes, retries, stale responses, corrupted files,
provider drift, prompt injection, and accidental user actions will occur.

Required verdict format:

```json
{
  "verdict": "pass | fail | needs-user",
  "domains": {
    "identity_and_isolation": {"verdict": "pass", "findings": []},
    "storage_and_lifecycle": {"verdict": "pass", "findings": []},
    "delivery_and_provider_capabilities": {"verdict": "pass", "findings": []},
    "trust_privacy_and_provenance": {"verdict": "pass", "findings": []},
    "collaboration_and_long_horizon_quality": {"verdict": "pass", "findings": []},
    "loops_rounds_and_recovery": {"verdict": "pass", "findings": []},
    "ux_and_accessibility": {"verdict": "pass", "findings": []},
    "migration_observability_and_testability": {"verdict": "pass", "findings": []}
  },
  "failed_criteria": [],
  "counterexamples": [],
  "missing_evidence": [],
  "required_revisions": [],
  "confidence": 0.0
}
```

Overall `pass` requires every domain to pass with no blocker or high-severity finding.
A deterministic contradiction always overrides a judge pass. After a failure, authors
must make concrete revisions and submit the changed artifact to the independent judge
again. An unchanged objection may not be answered with persuasion alone.

## 2. Design-completeness gate

Fail the architecture if any statement is true:

- Conversation ownership depends on names, route, pair, title, or provider thread.
- Storage, provider binding, cursor, or deletion can become an untracked second authority.
- Any durable field lacks exactly one named authority or relies on cross-database atomic commit.
- A context cursor advances before confirmed delivery/valid response stage.
- A stale response can commit after reset, deletion, participant leave, or route change.
- A provider thread is reused across a participant/chat boundary without an explicit binding epoch.
- Objective revision, participant leave/rejoin, and access-grant change are absent from the commit fence.
- Physical provider connection identity or concurrency scope is inferred from a display route.
- Capability fallback can occur without a recorded probe state and contract test.
- File presence is described as proof that an agent received, read, or understood context.
- Full transcript injection is the normal every-turn strategy.
- Summary text can promote peer/external content to trusted instruction or verified fact.
- Completion depends on consensus, novelty, or provider self-report.
- Fixed round limits secretly reset per phase.
- Unlimited progressive work hits an undocumented numeric ceiling.
- Loop prevention cannot distinguish verified state progress from paraphrasing.
- A stalled or limited run cannot resume from an exact checkpoint.
- Deletion hides a chat before exact artifact removal is recoverably complete.
- Late work can recreate a deleted/reset chat.
- The design claims local deletion erases remote provider history.
- New participants or provider trust domains receive old raw history without a grant.
- Same-connection agents may collide in concurrent provider turns.
- Agent/judge prose can forge deterministic evidence.
- A flattened CLI/web/file-aware turn can act on raw peer/external prose without an enforceable broker/sandbox.
- Unlimited rounds also makes per-turn transport retries unlimited.
- Protocol choice or claimed progress is controlled by model prose rather than deterministic policy and canonical transitions.
- The fast path always calls a peer when a deterministic direct path is sufficient.
- Judge independence is inferred only from participant name or ID rather than contribution lineage.
- Derived state can disclose content to a participant who lacks access to one of its sources.
- Pause must wait forever for a hung provider turn before it fences new work.
- A user must understand internal protocol vocabulary for ordinary chat.
- The happy path adds mandatory orchestration steps or persistent expert controls.
- Accessibility, migration rollback, corruption, observability, or chaos testing is omitted.
- Team performance is not compared with the best single-agent baseline.

## 3. Release-blocking engine properties

### Identity and isolation

1. Ten thousand randomized overlapping chats produce zero cross-chat context events.
2. Two same-pair chats have different capsules, projections, bindings, cursors, and attachments.
3. One agent in many chats receives only the selected chat’s authorized projection.
4. Rename/remove/rebind cannot relabel existing history.
5. Every manifest ID equals registry ID and validated path-derived ID.
6. Provider-route change preserves history and increments only its binding epoch.

### Delivery and idempotency

1. Fault injection at every delivery transition loses no acknowledged event.
2. Every committed response matches conversation, generation, run, objective revision,
   frontier version, turn, recipient, membership epoch, grant revision, binding epoch,
   and context digest.
3. Preparing but failing to deliver leaves content unread.
4. Delivery-unknown retry can create at most one logical commit and no duplicated side effect.
5. Provider acceptance evidence is adapter-specific and honestly labelled.
6. API, Claude/Gemini/Copilot CLI, Codex CLI, embedded web providers, external browser providers, and file/MCP resources have contract tests where supported.
7. Two agents sharing a physical binding are safely serialized while sealed drafts remain logically independent.
8. Objective, frontier, membership, grant, and binding changes racing response commit always revoke the stale answer.
9. Each physical connection has a stable ID, concurrency scope, versioned capability probe, expiry, and tested conservative fallback.
10. Every transport retry policy is finite and independently observable under Auto, Fixed, and Unlimited round modes.
11. Web submission acknowledgement requires the exact marked provider-side user turn;
    composer clear, Stop controls, reply counts, and streaming indicators are never
    sufficient alone.
12. After native activation without acknowledgement, `delivery_unknown` reconciliation
    never auto-resends; retry becomes available only after absence is proven.
13. Web adapters pass locale, zoom, responsive layout, SPA remount, overlay,
    authentication/challenge, restart, and delayed-acknowledgement fixtures.
14. Static partial text while Stop remains visible is never auto-stopped or committed
    as a complete agent response, including across long provider-side pauses.

### Enforceable action boundary

1. Collaboration context is advisory/read-only unless a separately authorized execution turn is created.
2. Raw peer and external prose never enters a tool-enabled execution instruction channel.
3. Project mutations occur only in broker-confined staging and pass authority, path, manifest, and verification checks.
4. An adapter that cannot prove confinement is advisory-only.
5. `.harness`, unrelated project roots, credentials, and ungranted network/tool actions remain unreachable under prompt-injection tests.

### Lifecycle and exact deletion

1. Deleting chat X changes no byte, row, binding, cursor, attachment, or run belonging to Y.
2. Fault injection at every deletion transition converges to intact visible state or completed tombstone—never hidden live data.
3. Late responses after delete/reset/rebind cannot commit or recreate an artifact.
4. Windows file locks leave an actionable visible deleting state and safe retry.
5. Renderer failure cannot prevent engine-owned binding cleanup.
6. Branches remain self-contained after parent deletion; deletion never cascades by default.
7. Backup restore cannot silently resurrect a tombstoned chat.
8. Control-store corruption or a crash between catalog and filesystem steps cannot hide, revive, or misroute a capsule.
9. Restart-run and clear-chat semantics cannot reuse a revoked generation or ambiguous identity.
10. Crash injection at each creation barrier produces either no visible chat or one complete active capsule.
11. Every stale pre-tombstone catalog snapshot is rejected or replayed through the
    higher independently durable witness sequence before any chat becomes active.

Required destructive property test:

```text
Generate arbitrary chats with overlapping agents, routes, projects, attachments,
bindings, branches, and in-flight turns. Select random X. Snapshot all non-X state.
Delete X while injecting late responses and crashing at every transition. Assert X is
tombstoned and absent, no X response commits, and every non-X byte and relation is
unchanged.
```

### Trust, privacy, and provenance

1. Prompt-injection corpora produce zero unauthorized side effects.
2. Every verified claim resolves to a valid Nexus receipt or deterministic source.
3. Every summary assertion resolves to authorized source events and retains dissent.
4. A peer cannot mint policy, user authority, access grant, or tool receipt.
5. New participants receive zero raw pre-join events without an explicit grant.
6. Route/provider trust-domain changes never disclose history silently.
7. Retrieval returns no event outside conversation, grant, or trust filter.
8. A derived item is visible only to the intersection of all source grants unless the user authorizes declassification.

### Crash, corruption, and migration

1. Random crash/restart at each turn, commit, migration, and deletion transition preserves invariants.
2. Corrupt registry/database/event chain never appears as an empty new chat.
3. Failed migration leaves original artifacts intact and visible.
4. Re-running migration produces no duplicate event or attachment.
5. A newer schema is never silently downgraded.
6. At every rollout phase, rollback/read behavior is specified and tested.
7. Every legacy 64-bit chat/file key maps durably to exactly one 128-bit conversation ID across repeated migration attempts.
8. Shadow, cutover, rollback, and retirement each have exactly one declared authority; no provider turn can observe dual authority.
9. Legacy prepared cursors are never imported as acknowledged; ambiguous ranges are reconciled or checkpoint-replayed without duplicate commit.
10. Legacy provider bindings are staged and verified/rotated before cutover, then become
    active solely through a matching catalog token; no cross-database atomicity is assumed.
11. Crash injection before/after each prepared binding and catalog cutover-token step
    selects legacy before activation and capsule after activation; Electron caches are
    rebuilt and never decide authority.

### Cross-store coordination

1. A field-level ownership assertion proves every durable fact has exactly one authority.
2. No code path holds capsule and control-store SQLite transactions simultaneously.
3. Crash after capsule turn/dispatch-intent commit but before coordination enqueue is
   recovered by idempotent intent scan; the inverse ordering cannot dispatch a
   nonexistent turn.
4. An orphan control-store lease/index entry with no matching capsule intent is dropped.
5. Event plus projection job commit in one capsule transaction; crash before publishing
   the view is recovered from the same-store outbox.
6. A stale project-writer process cannot commit after writer-fence takeover.
7. Deletion/revision between dispatch validation and provider response rejects the
   response even if transport succeeded.

## 4. Collaboration-quality gate

Use deterministic scripted agents first, then real-provider evaluations. Stratify all
metrics by task class, provider, model, and context length.

Critical scenarios:

1. Trivial request completes in one team cycle when peer review adds no value.
2. Hidden-fact relay: recipient correctly applies peer evidence in at least 98% of successful provider calls.
3. Original-question re-answer rate after objective evolution remains below 2%.
4. Sealed proposals remain hidden until every commitment or timeout.
5. False unanimous consensus fails a contradictory deterministic oracle.
6. Material minority dissent survives synthesis.
7. “Tests passed” without a Nexus receipt cannot satisfy verification.
8. A new user direction creates an objective revision and does not restart old work.
9. A resumed long task does not repeat accepted tasks.
10. One provider failure preserves other participants’ committed work.
11. A twenty-plus-checkpoint task survives restart and provider-thread rotation.
12. Progressive 50- and 100-cycle tasks do not false-pause.
13. Stable and A/B dead loops pause within two cycles after the required replan fails.
14. Paraphrased repetition does not count as progress.
15. Exact fixed limit schedules no team cycle beyond N across all phases.
16. Unlimited permits at least a 100-cycle materially progressive fixture.
17. Collaboration lift is positive on cooperation-beneficial tasks versus the best single-agent baseline.
18. Trivial tasks show no meaningful quality/time regression and a low unnecessary-peer-call rate.
19. Model-generated novel prose without canonical evidence/task transition cannot keep a stalled run alive.
20. Protocol selection is deterministic, traced, and stable for the same policy inputs.
21. A deterministic low-risk task can take the direct one-agent path without a ceremonial peer call.
22. A high-risk AI judge is rejected when its contribution lineage is not independent.
23. Immediate Pause fences new dispatch and offers bounded cancel/detach for a hung call.

Initial release targets:

- Zero cross-chat leak, stale commit, duplicate logical commit, wrong-chat deletion, or permission breach in the critical suite.
- Zero critical completion claims contradicted by deterministic checks.
- At least 95% verified success on the trivial scripted suite.
- At least 85% verified success on the long-horizon multi-agent suite.
- Fewer than 1% of materially progressive conversations falsely paused for stagnation.
- Provider attribution and recipient selection above 99.5%.

Thresholds must be revisited using confidence intervals and production baselines; they
may be raised, never quietly waived, before general release.

## 5. Performance and accessibility gate

- Context compilation p95 below 150 ms on a representative 10,000-event capsule.
- No provider request exceeds its declared context budget.
- A normal two-agent chat requires no clicks beyond the current type-and-send path.
- Starting a new chat, changing a simple Team policy, pausing, resuming, diagnosing an error, and deleting the exact chat are keyboard-operable.
- Status never relies on color or animation alone.
- Core surfaces pass automated WCAG 2.2 AA checks and manual keyboard/screen-reader review.
- 200% zoom/reflow introduces no horizontal composer overflow.
- Background updates do not steal focus, force scrolling, or flood live regions.
- Happy-path UI contains no IDs, hashes, cursors, epochs, leases, WAL, DAG, or ledger jargon.
- A provider uncertainty is described honestly and offers one clear recovery action.

## 6. Observability evidence required for a pass

The test harness must be able to prove:

- Objective revision and active acceptance criteria per run.
- Exact context version/digest and included event IDs per turn.
- Prepared, delivered, acknowledged, and committed cursor positions.
- Provider-binding identity, dispatch queue, retries, and reconciliation result.
- Accepted and rejected agent-proposed state transitions.
- Task, claim, decision, evidence, artifact, and blocker transitions.
- Progress fingerprint and reason for retry/replan/pause.
- Judge input, verdict, failed criteria, and tasks created from failure.
- Round, provider-call, retry, token estimate, elapsed time, pause, resume, reset, cancellation, deletion, and stale rejection.

Logs are redacted before persistence. Diagnostic export is explicit, local, and
sanitized.

## 7. Required judge questions

The independent judge must attempt concrete counterexamples:

1. What exact crash point can lose or duplicate peer context?
2. How could an answer from chat X be accepted by chat Y?
3. How could a late web/CLI response resurrect deleted state?
4. Which authority can falsely promote a claim to verified fact?
5. What happens when a provider receives a prompt but Nexus times out?
6. What happens when two agents use the same physical provider connection?
7. What prevents repeated historical goals from becoming the active prompt?
8. Can a genuinely productive 100-cycle task continue under Unlimited?
9. Can a stalled run pause and later resume without repeating finished work?
10. What does a screen-reader user hear during long autonomous work and on failure?
11. Can one-chat deletion touch a sibling same-pair chat or branch?
12. Can migration failure or corruption look like a fresh empty conversation?
13. Is every provider capability claimed by the design actually detectable or tested?
14. Does the default interface stay as simple as type and Send?
15. Does cooperation measurably beat the best single agent where it should?

A finding is resolved only by a normative rule, a testable invariant, and—when
implementation is judged—supporting evidence. Explanatory prose alone is insufficient.
