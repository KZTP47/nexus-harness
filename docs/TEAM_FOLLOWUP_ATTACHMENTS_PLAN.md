# Team follow-up attachments

Current-request ledger. Apply unlazy's four-pass implementation and independent
verification through the Nexus task-plan contract; no second gate hierarchy.
Preserve existing working-tree changes. Parent owns this plan and closeout.

| ID | Observable outcome | Owner | Evidence | Dependencies | State |
|---|---|---|---|---|---|
| R1 | Explain complete UI/API/storage/provider cause and risks | analysis agents + parent | Source trace and reproducible checks | None | verified |
| R2 | Concrete portable implementation plan includes independent critique improvements | planning agents + parent | Reviewed contracts and test matrix below | R1 | verified |
| R3 | Existing team goals accept files and pasted screenshots through both composers | UI implementer | Focused desktop behavioral tests including failed sends and chat switching | R2 | verified |
| R4 | Follow-up attachments reach intended provider context and survive persistence/restart | backend implementer | Portable API/engine/provider integration tests | R2 | verified |
| R5 | Validation, chat/project isolation, lifecycle, limits, and text-only compatibility hold | implementers + parent | Positive and negative regression tests in temporary projects | R2 | verified |
| R6 | Independent fix critique is addressed and parent verifies integrated result | reviewer + parent | Review findings, rerun focused checks and integration | R3-R5 | verified |
| R7 | Packaged desktop and installer rebuilt; shortcut/icon refreshed | parent | Mandatory post-work hook plus artifact verification | R6 | verified |

## Dispatch

Analysis wave: UI investigator owns read-only composer trace; backend investigator
owns read-only API/persistence trace; provider investigator owns read-only delivery
and test trace. All three launch before any wait. No implementation until the
planning phase and independent plan critique are complete. Implementation file
ownership will be declared after contracts settle.

## Plan and evidence

### Investigation

Three independent source traces agree: app.js disables Attach, paste follows that
state, and sendToActiveChatGoal rejects staged files. GoalStore.control reads only
text; creation alone calls keep_attachments and stores provider descriptors.
Provider adapters already support original screenshot bytes. This restriction
arrived with shared-project conversation UI in commit da3527b, not a demonstrated
upstream provider requirement. Parent reran existing team-chat UI tests: 48 pass.

### Proposed contract for independent critique

1. Team steering accepts payload `{text, attachments, request_id}`. Single ordinary
   question answers accept top-level attachments with the existing answer envelope.
   Only validated team-visible answers accept files. Reject attachment-bearing
   private messages/answers, other controls, and risk/multiple-question answers
   explicitly; preserve their existing text-only behavior.
2. Screenshot-only steering uses `Please review the attached files.`; answering a
   pending question still requires actual typed answer text. Both composers retain
   discovery, binding, lifecycle and loading checks. Picker captures originating
   chat before opening; pasted files share the same availability rules.
3. Atomic validated admission stages original bytes in a goal/authority/request
   owned directory, commits files plus steering/answer together, and cleans partial
   batches on failure. Stable request IDs bind exact input and chat/goal identity.
   Retry after restart returns the original receipt, without duplicate events.
4. A durable receipt identifies schema, accepted status, request ID, goal and binding,
   submission digest and file count. Renderer validates it before clearing only
   submitted file objects and matching text. Uncertain responses retain input and
   retry identity; successful persistence with failed refresh is still accepted.
5. Versioned follow-up batches preserve initial inputs and freeze team audience,
   public metadata, private original descriptors, extracted evidence, and nonsecret
   contract/manifest fingerprint. Private paths/base64 never enter public state.
   Both archive and legacy transcript projection associate metadata with its message.
6. Hydrate and hash-check all accepted initial/follow-up originals for provider and
   context-tool calls. Text is separately labelled attachment evidence, never a
   fabricated steering instruction or decision answer. Context/decision bindings
   fingerprint the input contract so obsolete continuations cannot replay.
7. Keep per-send limits (six files, 4MB/file, 8MB/batch). Declare cumulative goal
   limits: 64 descriptors, 8MB retained bytes and 240k combined objective/evidence
   characters. Reject excess atomically and explicitly; no truncation or eviction.
8. No broad new binary/PDF understanding is claimed. Preserve current supported
   extraction and native image behavior. Metadata display must not introduce broken
   downloads or unauthenticated file serving.

### Planned ownership after critique

| Leaf | Owns | Dependencies | Evidence |
|---|---|---|---|
| UI | app.js; team-chat-ui.test.js; chat-completion-attachments.test.js | settled request/receipt contract | both composers, picker/paste, retries, races, decisions, real browser |
| Backend | long_horizon.py; new goal input helper; server.py; new backend tests | reviewed plan | admission, persistence, limits, provider hydration, API binding, answer lifecycle |
| Projection | chat.py; goal_dialogue.py; goal_chat_projection.py; new projection tests | fixed metadata batch/event shape | safe metadata, replay, exact chat ownership |
| Parent | plan and integration/closeout | all leaves | rerun checks, independent critique, packaged source and deployment |

### Plan critique accepted before implementation

The delivery investigator separately cross-reviewed the backend and UI plans.
Parent inspected the actual answer whitelist, decision digest and transaction
owners. Required improvements:

- Bind canonical file intent before any answer receipt shortcut; preserve legacy
  text-only hashes. Forward attachment fields explicitly in server's whitelist.
- Freeze the entire uncertain request envelope, not only its ID: an accepted answer
  can disappear from pending cards before its lost response is retried.
- Keep attachment receipts with retained batches (no independent 128-entry pruning).
- Use unique attempt staging so cross-runtime retries cannot delete committed data;
  serialize durable acceptance in SQLite, cleanup only the losing/uncommitted attempt.
- Validate current authority/setup on replay without requiring a terminal accepted
  request still to own execution. An exact replay has no dispatch side effect.
- Return accepted receipt even if subsequent scheduling fails, with a separate honest
  scheduling problem; committed files survive. Test scheduler throw plus restart/retry.
- Count every original, including nonvisual files, for cumulative limits. Count
  existing objective once and follow-up evidence including labels for 240k limit.
- Cover closeout context's early return as well as facilitator/isolated contexts.
  Strip entire private batch state from public snapshots.
- Reject unsupported attachment-bearing operations at API/runtime boundaries.
- Prove submitted-object cleanup and retry races behaviorally in both composers.

Receipt schema fixed for implementation: top-level `followup_receipt` with
`schema_version:1`, `accepted:true`, `request_id`, `goal_id`, `chat_id`, `project_id`,
`participant_ids`, `submission_sha256`, `attachment_count`. Optional top-level
`scheduling_error` describes post-commit dispatch failure. Goal remains under `goal`.
Safe event/archive metadata field is `attachments`, an allowlisted metadata array.
Backend owns all long_horizon.py changes, goal_inputs.py, goal_decisions.py and
server.py; projection agent owns only chat/dialogue/projection and dedicated tests.
UI/backend/projection implementation wave activated after this critique.

Parent-owned independent integration: tests/test_team_followup_api.py. Baseline
HTTP reproduces both missing receipt and silently ignored files on unsupported
message action. Real browser baseline: 4/4 pass, no skip. An existing unrelated
fixture test currently expects context schema7 while source is already schema8;
reconcile that expectation with the final context version during integration.

### Fix critique and fresh evidence

Delivery agent cross-reviewed backend/UI implementation; UI agent cross-reviewed
projection implementation. Both report no remaining actionable attachment defects.
The following issues were found and repaired before acceptance:

- Combined input limits are preflighted before steering can roll back pending file
  transactions, including later text-only steering on an attachment-bearing goal.
- Direct store/API unsupported file actions fail rather than discard files.
- Input ownership binds stable goal/chat/project authority and participant IDs;
  provider setup remains checked separately. Explicit provider reconnect therefore
  retains originals and receipt validity without weakening execution checks.
- Explicit goal clone validates and rebinds inherited inputs, records provenance,
  and removes parent receipt authority. Child inputs obtain fresh child receipts.
- Compact picker now includes existing ZIP/DOCX formats supported by expanded chat.

Parent reran 91 UI tests (zero skips, actual Chromium), 31 projection tests, 14 new
runtime tests and 3 real HTTP tests. Runtime coverage includes both participant
routes receiving exact bytes, DOCX evidence separation, real ZIP read_archive after
restart, concurrent duplicate staging, cumulative limits, missing/tampered inputs,
authorized reconnect, fork inheritance and source receipt preservation. HTTP tests
cover authenticated binding, unsupported actions, exact retries, answer restart and
accepted receipt after scheduling failure. Parent inspected the corrected real
browser fixture capture: valid PNG chip, enabled Attach, Send to team and Resume team.
It is source-renderer evidence with mocked receipt responses, not live-provider or
packaged runtime evidence.

Integrated run: 335 Python tests, 334 passed and one stale pagination assertion
failed. Independent reviewer confirmed the existing visible-message paging
contract; parent corrected that test with stronger two-page private-content
exclusion checks. Final 70-test run (archive, runtime, HTTP, actual attachment
adapters, projection) passed, including all changed behavior; previously passing
unaffected core checks were not repeated. Another 45 desktop regression checks
passed (permissions, recovery, decision cards, identity/snapshot isolation), for
136 desktop checks total, zero skips. Python compilation, JS syntax and diff
whitespace checks passed. No real provider account/network dispatch was needed:
tests exercised actual adapters with controlled transport and original bytes.

Current-request reconciliation: all seven requirements verified, none unmet or
abandoned. Post-work deployment succeeded with session receipt
`2026-09-14T09-35-41Z-ad708fcbc4bd4f1988d35d5e89bacc63`. Parent verified all eight
changed product files match both build-output and the immutable runtime selected
by the refreshed desktop shortcut. Installer exists and shortcut icon references
that rebuilt runtime executable. No unresolved reviewer findings remain.
