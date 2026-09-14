# Marveen mechanisms: investigation for Nexus

Assessment date: 2026-09-14. This is an investigation, not an integration or a claim that Marveen completes autonomous goals better.

Source snapshots inspected:

- Marveen `b73b08124537098638e6760fff52532270d96568` (package version 1.37.0).
- Nexus `94378443ca608a569f0103cee1616fb7a8e6bb54`.

The useful outcome is selective adaptation: retain Nexus's execution and verification ownership; investigate freshness-aware background checks and optional semantic retrieval. The queue protocol provides useful reference tests, but replacing Nexus's mailbox would not add a missing foundation.

## Acceptance ledger

Unlazy's evidence, four-pass review and final reconciliation are applied through this single Nexus task plan. Per project policy, no GATES.md, .unlazy hierarchy, or Stop hook was added. Owner for every item: primary investigator. H1, H2 and M1 are independent source investigations; R1 depends on all three. The mandatory deployment closeout follows this investigation and its outcome is recorded separately by the post-work hook.

| ID | Observable outcome and evidence method | State |
| --- | --- | --- |
| H1 | Identify both delivery paths, acknowledgment/retry semantics, restart risks and Nexus overlap; inspect source and exercise real queue protocol | Verified |
| H2 | Identify due-work, catch-up, priority, cost and authority mechanisms; inspect source, exercise pure decisions and existing Nexus timer checks; distinguish integration limitations | Verified with native-Windows integration limitation below |
| M1 | Identify retrieval callers, ranking, scope and invalidation; inspect source and exercise database probes plus Nexus FTS/binding checks | Verified with embedding-quality limitation below |
| R1 | Rank concrete adoption candidates, risks, owners and acceptance criteria; reconcile with H1/H2/M1 | Verified |

## H1: handoffs and delivery

[Router source](https://github.com/Szotasz/marveen/blob/b73b08124537098638e6760fff52532270d96568/src/web/message-router.ts) supports two materially different paths:

1. The traditional path inspects a tmux pane and injects a prompt. The message becomes delivered after injection, not after successful task execution. Busy/absent recipients are retried; local pending messages have a one-hour abandonment window, injection failures have a three-attempt threshold, and terminal failures notify the orchestrator. Distinct busy versus stuck thresholds reduce false restart alerts.
2. The opt-in worksource path writes an item into a per-agent filesystem queue. The reader sends a real channel notification, receives an explicit work_complete call and records the result. Its pending/active/done directories survive process restart. The writer derives item identity from the database message ID and refuses re-enqueue when the ID already exists in any of those directories. The router's delivered state means durably queued, not processed.

[Writer](https://github.com/Szotasz/marveen/blob/b73b08124537098638e6760fff52532270d96568/src/web/worksource-queue.ts) and [reader](https://github.com/Szotasz/marveen/blob/b73b08124537098638e6760fff52532270d96568/plugins/worksource/server.mjs) are useful small reference implementations. The real stdio protocol verification passed 21 checks, including queued delivery, explicit acknowledgment, malformed acknowledgment IDs, metadata framing and restart redelivery.

However, the reader reoffers unacknowledged work after 120 seconds by default, or immediately after reader restart because handedAt is in memory. A modified protocol probe, using a shorter test-only timeout, confirmed redelivery without proving the original attempt stopped. This demonstrates at-least-once delivery, not exactly-once execution. It does not demonstrate a duplicate external action: no external action was performed. A worker could finish an effect and lose its acknowledgment; replaying that effect requires idempotency or an outcome-reconciliation step.

There is also a multi-file completion transition: write done/<id>.json, then rename the active item. Crash atomicity across these two operations is not established by the passing protocol test. The writer's existence-check then write is not a multi-writer transaction. These are adoption constraints, not a full security audit.

Nexus already has durable message IDs, exact payload preservation and digest checks, allowed-sender and shared-goal filtering, and retention of undelivered messages in `src/our_harness/agent_mailbox.py`. In `swarm.py`, acknowledgment follows a durable receiving answer and required shared-notebook write. The long-horizon engine separately owns task claims, provider/configuration fingerprints and unknown outcomes; do not conflate those two coordination paths. Fresh mailbox checks passed.

Recommendation: borrow explicit delivery-stage visibility, failure escalation and protocol fault cases. Do not import terminal injection, timeout-only replay of file-changing work, or a second mailbox store. A successful reply is also not independently verified task completion; keep Nexus's final project checks.

## H2: heartbeats and scheduling

[Schedule runner](https://github.com/Szotasz/marveen/blob/b73b08124537098638e6760fff52532270d96568/src/web/schedule-runner.ts) combines several useful mechanisms:

- Persisted last-tick and last-run records scan downtime after restart. Per-task freshness decides whether a late occurrence should run or be recorded as missed. Defaults are 30 minutes for heartbeats, three hours for ordinary tasks and one day for command tasks; overrides exist.
- Persistent retries keep unavailable/busy targets visible. User-facing work is ordered ahead of routine heartbeats. A quota gate can suppress background work using a fresh quota snapshot.
- A deterministic pre-check can return SKIP before spending an LLM call, or provide a compact context prefix. Missing/failing pre-checks fall through to the LLM; this is a cost optimization, not an authorization boundary.
- The watchdog distinguishes no observed start from a finished turn: idle without sawTurn becomes lost after the grace period; idle with sawTurn becomes done. This still establishes turn activity rather than correctness of the requested result. In-flight watchdog state is an in-memory map; restart reconciliation records orphaned runs as interrupted rather than restoring full monitoring state.

The two selected upstream scheduler suites could not load on native Windows because an imported module resolves tmux at import time. This is an environment limitation, not a failing behavior assertion. Exact pure function declarations were extracted and transpiled unchanged for focused tests of hold/lost/done/abandoned and on-time/catch-up/stale decisions. These checks do not certify the live scheduler, clock/timezone behavior or tmux delivery.

Autonomy is not proven by the settings page alone. `routes/autonomy.ts` enforces locked/max-level settings changes; scaffolded agent instructions tell agents to consult the configuration. There are separate execution hooks, including [the email gate](https://github.com/Szotasz/marveen/blob/b73b08124537098638e6760fff52532270d96568/scripts/hooks/email-approval-gate.py), whose current code permits level-three main-agent email sending. Therefore the earlier documentation claim that outward actions can never become autonomous is too broad for this snapshot. This investigation does not establish comprehensive enforcement across every tool.

Nexus's `timer.py` already delegates wakeup to the OS scheduler, coalesces missed runs, prevents overlap, and runs frozen automation through PipelineRunStore. Its occurrence identity includes the timer configuration digest; an already accepted occurrence is not blindly duplicated. Tests of these existing behaviors passed. These automation timers are distinct from long-horizon goal scheduling and from browser-connection liveness heartbeats.

Recommendation: add freshness-aware, event-sensitive checks to the existing timer/goal owners if a real usage case warrants them. A useful check observes changed state, then requests an authorized action or reports an actionable blocker. It must not automatically resume a paused goal, clear an unknown outcome, extend a budget or infer permission from elapsed time.

## M1: memory retrieval

[Database implementation](https://github.com/Szotasz/marveen/blob/b73b08124537098638e6760fff52532270d96568/src/db.ts) combines lexical FTS5 results with optional Ollama embeddings using reciprocal-rank fusion (k=60). FTS candidates also receive recency re-ranking. The vector path scans eligible stored embeddings in JavaScript, so work grows with eligible memory count and vector size.

Important caller distinction: `routes/memories.ts` defaults to FTS; hybrid search requires mode=hybrid. `memory.ts`'s buildMemoryContext uses lexical matches plus recent memories. Hybrid support does not mean every ordinary agent turn automatically receives semantic retrieval.

Confirmed through source and deterministic database probes:

- updateMemory changes content and invalidates listing caches but retains the old embedding. backfillEmbeddings only fills NULL vectors, so it does not repair that stale vector.
- Vector-only results can fill the answer when no keyword matches. Marveen exposes ftsHits, vectorHits, ftsRelaxed and vectorOnly. This visibility is worth adopting; rank fusion is not a confidence or relevance threshold.
- Agent-private rows are scoped to the requesting agent, while category=shared rows cross agent boundaries. That is a fleet-sharing policy, not Nexus project-vault isolation.

Further static constraints: embedding input is sliced to 2,000 characters; stored vectors lack a per-row model/dimension/content contract fingerprint in this path; cosine comparison assumes compatible arrays. A model change or edited text therefore needs explicit invalidation before this design would satisfy Nexus's durable-state rules.

Nexus's `persistent_memory_index.py` uses source-linked Markdown chunks and FTS5/BM25 in a generated project-bound index. Its bounded context reserves mandatory policy notes. Keep Markdown canonical, preserve project binding, and add optional vectors only inside the generated index. Never copy this project's actual vault into a benchmark or Marveen installation.

Recommendation: prototype hybrid retrieval only on synthetic or explicitly approved project-local fixtures. Require content hashes, model identity/digest, dimensions, chunking version and schema version; invalidate on edits, deletions, rebindings or model changes. Filter by project before ranking. Preserve policy-note reservations, FTS fallback and retrieval provenance. Do not decay authoritative constraints merely because they are old.

## R1: adoption order and measurable acceptance

| Priority | Candidate and Nexus owner | Required evidence before integration |
| --- | --- | --- |
| 1 | Event-sensitive heartbeat with per-task freshness, using timer.py and existing goal state | No-change check spends zero model calls; changed state produces one eligible action; repeated wake/restart does not duplicate an occurrence; stale work is reported/coalesced; pause, unknown outcome and configuration changes remain respected |
| 2 | Optional hybrid retrieval in persistent_memory_index.py and bounded context assembly | Fixed labeled queries compare recall/ranking with FTS under the same context budget; no cross-project hits; edited/deleted/model-changed embeddings invalidate; unavailable embedder falls back; report latency and indexing cost |
| 3 | Handoff status and escalation over existing mailbox/goal owners | Distinguish queued, dispatched, response recorded and verified; inject crash/lost-ack cases; no replay of uncertain side effects; show actionable failures without confusing a busy agent with a stuck one |

This ranking favors direct progress on unattended goals. Semantic retrieval may be the easiest isolated experiment, but benefit remains unmeasured. No live Claude or Ollama quality benchmark was run; no comparative completion-rate or cost claim is justified.

## Verification and limits

- Nexus: 111 focused tests passed (mailbox class, timer module, FTS/KV retrieval, vault binding, bounded mandatory context).
- Marveen: 27 existing tests passed across worksource wiring and memory recency; some are source-contract checks, not runtime integration tests.
- Four additional probes passed: stale vector on edit, vector-only/shared scope, isolated watchdog decisions and isolated freshness decisions. The embedding fixtures were mocked; this measures contracts, not semantic quality.
- Real stdio MCP verification: 21/21 upstream checks passed. An augmented run passed 22/22, including timeout redelivery; these are overlapping runs, not 43 independent cases.
- Two upstream scheduler suites remained unexecuted because native Windows lacks tmux. No broad platform support claim follows from the isolated policy checks.
- Initial Nexus launches needed PYTHONPATH and access to installed LangGraph dependencies; the corrected complete selection passed. The first dependency download stalled in the restricted environment; the explicit network-enabled retry succeeded with lifecycle scripts disabled.

Reproduction materials remain in the local, Git-ignored `.codex_tmp/marveen-investigation` checkout. Added investigative files are `src/__tests__/nexus-investigation.test.ts` and `plugins/worksource/nexus-timeout-verify.mjs`. Original upstream files were not edited. Run the three selected Vitest files with `node node_modules/vitest/vitest.mjs run`; use `node plugins/worksource/verify.mjs` for the original protocol check. The Nexus selection is documented by the test names above and uses PYTHONPATH=src.

Four-pass review: traced implementations and callers; compared current Nexus owners; challenged claims with negative/restart/database probes; reconciled the final report to distinguish modeled decisions, real protocol behavior and unmeasured live-agent performance. No product source changes, external messages, installation of running services or real vault embedding were part of this investigation.
