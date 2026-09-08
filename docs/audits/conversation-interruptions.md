# Conversation interruption audit

Scope: the entire first-party Nexus source tree, desktop shell, provider adapters,
UI, and supporting scripts/configuration. The branch also includes the source
changes already present when the audit began. Private runtime state and external
project memory are excluded.

## Coverage method

`scripts/audit_conversation_interruptions.py` inventories every version-controlled
or new non-ignored source/configuration file across the repository, including
entry scripts, example plugins and workflow configuration.
It records source hashes, broad interruption candidate lines, and Python AST
transition sites in `conversation-interruptions-inventory.json`. Tests, vendor
dependencies, and generated build outputs are not production source. Tests are
included in regression execution instead.

The census is a coverage aid. Candidate sites were traced through their owning
state machines, caller paths, persistence and UI consumers. A keyword match is
not automatically a defect, and keyword absence is not proof of correctness.
Necessary stops were retained rather than converting failures into completion.

## Confirmed defects repaired

| ID | Trigger and previous interruption | Corrected behavior | Evidence |
| --- | --- | --- | --- |
| I1 | Read-only or a saved command denial raised another pending permission stop | Denied tool result reaches the agent; only a new command approval request pauses. Final required verification remains mandatory | Python interruption/access tests |
| I2 | Web reply still generating at 165 seconds was forcibly stopped despite the bridge's 420-second allowance | Bridge service deadline travels through UI, preload, main and queued browser work; shorter explicit budgets still apply; partial replies remain rejected | Browser deadline tests, bridge and web-chat tests |
| I3 | No baseline callable existed, yet startup asked the user to select one | Planning can discover/design the target; this does not ratify a target or bypass final acceptance checks | Planning interruption test and existing acceptance tests |
| I4 | Planning received new file contents while readiness stayed false; loop guard called it stalled | Actual delivered file/directory observations advance planning; fabricated filenames, missing files, repeats and oscillation do not | Retrieval progress regression and existing loop tests |
| I5 | Wrong conversation/decision ID or cursor aborted the whole agent turn | Typed, side-effect-free request errors are returned to the agent for correction; archive corruption remains a hard error | Runtime request/negative integrity regressions |
| I6 | Independent review said `changes_requested`, leaving author and review blocked | Unapplied candidate is discarded, findings reach the author, obsolete review is retained as superseded, replacement requires fresh review. Repeated unchanged candidates remain bounded | Review correction/restart and existing rejection/review tests |
| I7 | Agent proposed changes in read-only mode; engine impersonated a user Pause | Rejected proposal is recorded and returned to its author without writing; repeated forbidden proposals are bounded | Runtime read-only correction and loop regressions |
| I8 | Proposed file content already matched the authenticated baseline; apply raised an error | Record a verified no-change snapshot, create no transaction, and continue toward required verification | Runtime no-op regression; baseline/transaction regressions |
| I9 | Required project checks were absent during implementation, classified as unrepairable infrastructure | Return a bounded repair task to the team to author checks, retaining the unavailable verification result and requirement. Actual missing runtime/containment/permissions remain stopped | Missing-check and verification-policy regressions |
| I10 | A reviewed compatible provider reconnect changed the access fingerprint and silently reverted full/ask to read-only | Rebind previously validated access inside the reviewed transaction; preserve deny/always/consumed-once grants; unrelated drift remains read-only | Reconnect restart/access regressions |

## Retained boundaries and ownership review

| Owner group | Sites reviewed and disposition |
| --- | --- |
| `long_horizon` admission, claims, dispatch, context, apply, review, verification, controls, recovery, startup watcher | Preserve exact scheduler leases, explicit provider/tool/task budgets, real user questions, uncertain provider/file effects, changed setup, publication conflicts, repeated identical work, no runnable dependency graph, required verification and explicit Pause/Cancel. Correctable reads/reviews/access/no-op application repaired above. |
| `goal_access`, `goal_decisions`, `user_questions`, `goal_context_progress`, `goal_budget_policy`, `action_protocol` | Preserve authenticated authority, exact decision audiences, bounded format correction and actual no-progress detection. Known access denials and typed read-request errors repaired. |
| `goal_dialogue`, `goal_chat_projection`, `goal_chat_progress`, `chat`, `swarm_chats`, UI | Preserve transcript identity/order and audience, input-only attention, stale-request guards, explicit control semantics. Switching/minimizing/closing a panel cancels its reads rather than agent work. |
| `swarm_work`, `swarm`, `team`, `collaboration_outcomes`, `collaboration_ledger`, `swarm_goal_queue` | Trace direct collaboration, legacy planning/execution loops, essential structured questions, provisional transaction rollback, exact saved-run recovery and goal queue incompleteness. Repair target discovery and planning observations; retain required verification/authority boundaries. |
| `swarm_runs`, `cooperation`, `agent_mailbox`, `messaging`, `resident`, `cancellation` | Preserve per-resource capacity, physical-provider ambiguity fences, authenticated claims/checkpoints, cancellation wakeups and incomplete effect recovery. No global conversation stop added for ordinary messages. |
| `web_chats`, desktop `web-chats`, `external-browser`, main/preload/server | Repair deadline mismatch. Retain exact marked delivery, active-generation rejection, uncertain-send no-replay, explicit Stop and app shutdown. |
| Provider adapters, `provider_repair`, `provider_reconnect`, `provider_help`, `local_models`, `seats` | Preserve unsupported/missing setup, authentication and credential failures, actual transport ambiguity and incomplete response rejection. Historical known failures do not permanently poison unrelated fresh requests. |
| `workflow`, `graphs`, `plain_graph`, `workflows`, `checkpoints`, `runstate`, `memory` | Preserve authored approval nodes, bounded transition/deadline/failure loops and durable checkpoint/effect consistency. These paths cannot silently skip acceptance or declare incomplete work complete. |
| `changes`, `staged_coding`, `programmatic_workspace`, `goal_workspaces`, `gitops`, `runtime_integrity`, `safety`, `ignore_policy` | Preserve immutable baselines, path/symlink protection, ownership and rollback/commit boundaries. No blanket retry of a possibly applied mutation. |
| `goal_verification`, `verification*`, `execution`, `project_execution`, `windows_containment`, `playwright_runtime`, `timer` | Preserve required checks, user execution authority, executable/runtime confinement and explicit execution deadlines. Infrastructure unavailable remains distinguishable from a repairable project assertion failure. |
| `agent_tools`, `context`, `indexer`, `mcp`, bounded reads | Preserve call/output/time limits, tool allowlists, cached-call identity, confined reads and cancellation propagation. Ordinary tool dispatch failures already return structured results; read APIs outside that wrapper needed I5. |
| Visual automation: `pipelines`, `pipeline_runs`, `pages`, `navigate`, `recorder`, `selectors`, `qa`, `review_panel`, `watcher` | These are automation/check execution owners, not arbitrary chat-message stops. Preserve explicit waits, action failure evidence, user cancellation and finite checks. |
| CLI/setup/reporting/benchmark/scripts/configuration/templates/static pages | Review their entry points and interruption candidates as process control, bounded tooling, diagnostics or presentation. CI deadline guard and mandatory local deployment/memory gates retained. Other inventoried source modules contain no independent conversation-state transition. |

## Verification and review

Independent round 1 rejected the first revision: review IDs renewed the retry
counter, the UI Deny path still stranded a command-permission pause, and reviewed
reconnect invalidated saved access. All three received fixes and new regressions.
The repaired round passed 37 focused Python tests and four browser/UI tests.
Independent round 2 passed source correctness with no further concrete finding:
41 Python tests and four browser/UI tests passed. The parent reran those checks
successfully against the joined source. The final desktop regression suite
passed all 376 tests.

The source census covers 197 files, with 146 files containing lexical candidates
and 153 Python AST candidate transition assignments. The distribution audit
passed on a clean snapshot of all 514 tracked/new non-ignored files (311 scanned
distribution files, zero findings). The same audit in the working checkout
reported old machine-specific paths exclusively in ignored `reports/` evidence;
that evidence is neither staged nor distributed. Its failure is recorded rather
than misreported as a passing checkout run.

The broad run also detected two control-inventory gaps. Existing permission and
reconnect browser tests are now included in that inventory; new browser actions
exercise saved-work inspection and ensure all three composer access selections
reach the goal request policy. All 16 control-inventory tests and the repeated
376-test desktop suite pass after that test-only correction.

The complete Python run executed 4,602 tests across all 173 test modules, with
no omitted or duplicated modules. Nine platform/privilege-dependent tests were
skipped. Its four failures were the distribution audit described above, the two
control-inventory gaps, and an activity-panel fixture missing the newly called
attention-refresh dependency. The latter fixture now asserts the exact agent
refresh. The owning activity-panel and control-inventory groups pass on rerun;
the distribution audit passes on the clean publishable source snapshot. There
were no Python test errors or unresolved product-runtime failures in this run.

This audit covers the current source snapshot and exercised behaviors. It cannot
prove absence of every possible future provider, OS or concurrency failure.
