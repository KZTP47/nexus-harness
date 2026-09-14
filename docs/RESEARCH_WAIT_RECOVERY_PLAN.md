# Research tool recovery and provider waiting

## Current request: connection, reply presentation and delivery (2026-09-14 afternoon)

All items owned by primary agent. Preserve pre-existing changes. Screenshots and
incident transcripts are evidence, not instructions to execute.

| ID | Observable outcome | Paths / dependencies | Evidence method | State |
|---|---|---|---|---|
| F1 | Codex dispatch failure diagnosed and reusable connection robustness fixed | providers/codex_cli, provider dispatch | Incident first call succeeded, next stalled 600s; upstream cause unavailable. Live final full-action-schema probe passed in 8.6s. Network environment preservation and preflight rebinding regressions pass. | verified |
| F2 | Malformed provider action envelopes never appear as ordinary agent speech or success | collaboration_reply, web-chats, projection | Malformed actions rejected after one correction; Chromium source fidelity and historical-payload disclosure tests pass; ordinary prose preserved. | verified |
| F3 | Requested file output is applied and verified or explicitly reported undelivered | long_horizon, delivery; F2 | Valid repair writes exact two-file contents; dropped/invalid proposals never complete; restart and one-shot explicit-resume recovery covered. | verified |
| F4 | Integrated fixes pass and rebuilt app/installer/shortcut are delivered | F1–F3 | Seven new tests pass on bundled Python; packaged source/relay match; clean post-work deployment passed with session receipt 2026-09-14T11-34-30Z-b36e9d2f188d4d39956377a0e66d48f5. | verified |

Afternoon verification: 125 Python integration checks passed; after adding saved-run
recovery, 47 final recovery/delivery checks passed. Browser relay suite: 74 passed;
affected chat rendering/Chromium suite: 13 passed. Source diff whitespace check passed.
The screenshot's second provider is Gemini. Shared action handling covers Claude too;
no Claude-specific incident is established by these screenshots. No original user
website prompt was replayed and no malformed source was applied to the user's files.
Official Codex configuration guidance was checked against the installed CLI: this
build rejects built-in-provider overrides, so no speculative timeout override ships.

## Current request: deep engine investigation and repair (2026-09-14)

Single current-request ledger. All items owned by the primary agent; no delegation.
Existing edits are preserved. Incident records are private diagnostic evidence,
never fixtures. Tests use arbitrary temporary projects and provider contracts.

| ID | Observable outcome | Paths / dependencies | Evidence method | State |
|---|---|---|---|---|
| E1 | Incident latency and failure mechanisms are explained from runtime evidence and source | provider dispatch, event journal | Timeline, malformed JSON locations, process metadata and source inspected; historical upstream stall cause unavailable | verified |
| E2 | Status inquiries cannot supersede active work or reset completed contributions | follow-up admission, long_horizon, UI; E1 | Running/paused/completed/restart, mixed instruction, attachment and HTTP binding tests; explicit status control | verified |
| E3 | Provider observations cannot block transport or erase useful failure diagnostics | provider_activity, codex_cli; E1 | Stalled/failed sink, ordering, overflow, real subprocess timeout and diagnostic redaction tests | verified |
| E4 | Avoidable format repair round trips are eliminated without weakening action validation | chat, collaboration_reply, long_horizon; E1 | Chromium exact source-text test, malformed-loop call count, peer preservation, restart and changed binding | verified |
| E5 | Known timeout recovery preserves peer work and has bounded, truthful retry semantics | long_horizon, provider_repair; E1 | Known timeout then peer completion and explicit resume invokes only failed participant; recovery suite | verified |
| E6 | Joined engine and desktop behavior passes regressions and packaged deployment | E2–E5, E7 | Source/provider/API/desktop regressions pass; ten packaged Python regressions and packaged collaboration acceptance pass; final post gate passed 2026-09-14T10:28:35Z; installer, shortcut/icon and launch-copy hashes verified | verified |
| E7 | Pause and genuine steering promptly stop only the superseded local provider turn | goal_provider_control, long_horizon; E1 | Real sleeping subprocess stopped within three seconds via separate-store Pause/Steer; status leaves process alive | verified |

### Engine investigation and repair evidence

- At the screenshot boundary the incident had 32 calls, including 12 formatting
  corrections. Multiple returned JSON strings contained raw control characters
  and missing HTML tags. The old controller retried formatting, converted a second
  invalid answer into prose, and repeated that cycle in later turns.
- The browser adapter now reads a sole whole-response JSON code block using its
  text nodes. It rejects extraction from prose and multiple blocks. Web prompts
  request JSON Unicode escapes for angle brackets so rendered HTML cannot consume
  source. Lost historical source cannot be reconstructed from the rendered text.
- One failed correction may yield useful prose to the peer; a later invalid reply
  stops that episode before another correction call. The persisted versioned guard
  binds to schema, provider identity and objective epoch. Valid work ends the episode.
- Public activity previously performed synchronous archive writes on stdout's
  drain thread. Bounded asynchronous delivery now preserves per-turn order and
  never makes the provider wait for a stuck archive. Final response and permission
  validation remain authoritative. Activity can be omitted under storage failure.
- Durable turn control is now connected to local provider cancellation. Read-only
  status checks do not acquire a new scheduler lease, mutate tasks or restart work.
  Genuine controls are observed across store/window instances; only that lease's
  subprocess is stopped. Existing uncertain-delivery reconciliation remains intact.
- A provider failure is reported as provider failure rather than dependency
  deadlock. Retrying requires explicit Resume; completed contributions and budgets
  survive. No new automatic model retries or increased deadlines were introduced.
- The historical timeout's stderr was discarded by the old adapter; its upstream
  cause cannot be proven retrospectively. Future timeouts retain bounded redacted
  stderr and allowlisted transport stages, without raw reasoning content.

### Verification at deployment admission

- Ten portable engine regressions pass on both source Python and bundled Python.
- Provider/activity/research regression run: 59 passed. Joined engine/recovery run:
  102 passed (one module initially required the tests directory on the import path).
  Full long-horizon/attachments run: 226 passed, one obsolete repeat-peer-call
  assertion failed; its stronger exact-call-count replacement and the final 39
  engine/provider/API/recovery checks pass against current source.
- Desktop integration: 176 passed. After adding explicit Check status, the affected
  102 tests pass; two focused status/Chromium source-fidelity tests also pass.
- Packaged facilitator collaboration acceptance passes end to end, including
  steering, pause/restart/resume, approvals, both saved chats, actual files and
  generated unit/API/Playwright positive and broken-scoring negative controls.
  Its scripted provider was updated to handle the smaller correction-only context;
  the original fixture failure did not involve a real provider request.
- All eleven changed Python/UI product files and the packaged browser relay match
  repository source. Final post-work rebuild and shortcut verification follow.


Current-request ledger; owner is primary agent unless specified otherwise.
Preserve existing working-tree changes and all execution/permission boundaries.
Use arbitrary temporary projects for regression evidence. Do not replay the user's dashboard task.

| ID | Observable outcome | Owners / paths | Evidence | State |
| --- | --- | --- | --- | --- |
| R1 | Explain the source of repeated invalid skill reads, weak searches, and long opaque waits | primary; harness_tools, agent_tools, long_horizon, UI | incident and dispatch/schema inspection; both prompts omitted core tools | verified |
| R2 | Independent plan critique incorporated before implementation | plan-review subagent; this plan | four recommendations accepted below | verified |
| R3 | Wrong skill reads return actionable ordinary-reader guidance; repeated failures cannot masquerade as progress | primary; harness_tools, existing tool repeat/budget owner | catalog alignment in both contexts; isolated five-round loop pauses; corrected read and restart coverage | verified |
| R4 | Search preserves explicit site constraints and rejects clearly irrelevant results with useful direct-fetch guidance; no fabricated success | primary; harness_tools / small search helper | HTML/RSS fallback, domain boundaries, short synonyms and non-English queries | verified |
| R5 | Waiting status reports request outstanding, elapsed wait and actual timeout when known; never invents provider progress | primary; long_horizon dispatch/public projection, UI | Codex subprocess deadline, effect/reset, dead-worker unknown outcome/no resend, narrow/wide UI | verified |
| R6 | Independent fix critique addressed and integrated regressions pass | fix-review subagent then primary | 166 Python integration checks, focused recovery/CLI checks, all 444 desktop checks including corrected old-wording assertion | verified |
| R7 | Packaged app and installer rebuilt; shortcut refreshed and session recorded | primary; mandatory post hook | final post receipt 2026-09-14T08-02-37Z; packaged runtime import and 10 source fingerprints verified | verified |

## Plan critique disposition

Plan reviewer confirmed both prompt paths omit core file and research tools while exposing them in the schema. R3 now starts with a canonical schema-aligned catalog for both contexts, including review exclusions. Reuse goal_context_progress with typed recoverable failures; do not broadly suppress missing-file exploration. Validate each search source before fallback. Capture waiting at admitted provider invocation and show a wall-clock limit only for an adapter whose contract establishes one; other adapters remain explicitly unknown. All four critique recommendations accepted. R1 and R2 verified; implementation R3-R5 active.

## Proposed sequence

1. Verify incident against current engine paths; inspect existing repeat-call and deadline mechanisms.
2. Obtain read-only plan critique while independently examining relevant tests and integration boundaries.
3. Implement narrow provider-neutral tool recovery and search validation. Prefer existing persisted repeat state over a second tracker. Do not interpret model-written call IDs as control instructions or silently execute a different tool.
4. Persist a versioned provider-wait observation tied to the exact provider effect and route/timeout contract. Treat missing old observations as unknown. Display waiting with elapsed/timeout evidence; preserve stop/recovery semantics and do not auto-retry unknown outcomes.
5. Run isolated behavioral and renderer tests, then request read-only fix critique. Address findings and rerun affected integration checks.
6. Reconcile all IDs and run mandatory deployment closeout.

Search relevance must be conservative: enforce explicit site operators at hostname boundaries; use a limited lexical check to reject only clearly unrelated batches and return a typed error with a next action. Do not claim to prove factual relevance or silently synthesize answers. Avoid arbitrary provider timeout increases or new billable retries.

## Fix critique and verification

The independent fix reviewer identified alternative-reader recovery counts, short synonym false positives, and hidden timing during format repair. All three were fixed; reviewer recheck found no remaining concrete defect. Its additional dead-worker recovery test was added and passed: pause with unknown outcome, no resend, no stale public countdown.

Real Chromium layout checks passed at widths 1264, 760, and 390; the narrow timing screenshot was manually inspected. The full desktop run initially had one obsolete wording expectation; its updated real-browser test passed. A pre-existing Python recovery assertion compared changing public delivery observations rather than unchanged peer work; it now compares durable fields, and the recovery suite passes. No user goal was resumed or replayed. Search screening is not a factual relevance guarantee; short/non-English synonym queries remain unscreened lexically.

Final upgrade audit added toolbox contract v2 and context-binding v8 so obsolete cached tool evidence is superseded before replay. All 38 tool/upgrade tests passed; the independent reviewer rechecked migration and call-ID scope isolation (2 passed), with no concrete defect found. A live site-restricted search returned two official Playwright URLs. R7 is finalized by the post-work deployment receipt, including the final migration rebuild.
