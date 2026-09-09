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

## Follow-up: stale verification blockers and local browser scenarios

The reported saved team repeated a historical verification exception after its
runner had been repaired. A Codex transport instruction also prohibited reading
files or running commands without distinguishing native CLI tools from the
Nexus JSON tool protocol. Fresh execution exposed a separate local Playwright
compiler defect: a regex crossed string boundaries, merged an assertion into
the next click selector, and retained only one assertion after all actions.

The current-request acceptance ledger is maintained here for this follow-up.
All work is owned by the primary agent; no delegation was used.

| ID | Observable outcome and owner paths | Evidence method | State |
| --- | --- | --- | --- |
| R1 | Verifier errors remain JSON-safe, retain the cause, and allow later checks; `swarm_work.py`, broker runtime | Broker failure/reload tests; a real check after injected OSError | Verified |
| R2 | Authorized reads, checks and team messages remain available; obsolete verification observations cannot permanently strand settled tasks; `providers/codex_cli.py`, `long_horizon.py` | Transport prompt contract; real scheduler with provider fixtures, explicit Resume, restart, obsolete contract, uncertain-effect and repeated-recovery cases | Verified |
| R4 | Local browser checks retain literal strings, all assertions and source order; unsupported syntax is repairable test feedback; `playwright_scenarios.py`, `swarm_work.py` | Real contained browser positive and initial/final negative scenarios; escaped/template strings, count assertions, dynamic/unsupported-operation rejection; project failure projection | Verified |
| R3 | Joined changes pass regression and the packaged deployment gate; depends on R1, R2, R4 | 156 final focused Python tests plus 4 Playwright integration tests passed; post-work Electron/NSIS/shortcut gate passed; built-app smoke passed, including access and Resume controls; all four packaged source owners match the tested files byte-for-byte | Verified |

Fresh execution of the reported scenario passed all seven browser assertions.
The original team then resumed through product-owned APIs, exchanged fresh
verification evidence, and reached durable `complete` with final verification
and publication. The delivered game required no local source repair.

Context binding schema 5 invalidates pre-fix observations automatically.
Explicit Resume expires completed verification observations while preserving
budgets, artifacts and pending-effect reconciliation. Automatic scheduler
recovery only reopens settled reported blockers when an obsolete verification
observation exists; superseding that observation prevents repeated recovery.
Current failures, user pauses, review decisions and uncertain effects are not
converted to completion. The local compiler remains a literal straight-line
subset; unsupported suites fail with actionable compatibility feedback rather
than being partially accepted or labelled a broken runtime.

## Follow-up: packaged AppContainer compatibility

Current-request ledger (unlazy discipline using the repository's single ledger).
The primary agent owns all implementation and integration. Two independent
read-only agents audit browser compatibility and recovery; their findings require
primary-agent inspection and runnable verification before acceptance.

| ID | Observable outcome / owned paths | CHECK / EXPECT | State |
| --- | --- | --- | --- |
| AC1 | Long packaged executable paths launch with reliable native handle/error handling; `windows_containment.py` | 45 native/runtime tests pass, including >300-character paths, Unicode, shell metacharacters, exit status, external-write denial and mutex cleanup; actual 269-character packaged Chromium path passes under bundled Python 3.11 | Verified |
| AC2 | Supported browser verification uses paired runtime, preserves suite selection and assertion semantics, serves ordinary local assets; `playwright_runtime.py`, `swarm_work.py` | Six browser/scenario tests pass, including real delayed DOM/module/directory navigation, ordinary loops/keyboard/selectors, negative assertion, line selectors and custom config; runtime discovery and remote-suite regressions pass | Verified |
| AC3 | Engine upgrades invalidate obsolete verification failures without fabricating user decisions; `long_horizon.py` | Schema 6 native contract restart/one-time recovery and pause/uncertain-effect regressions pass | Verified |
| AC4 | All AppContainer entry points audited; supported paths integrated and remaining capability boundaries explicitly accounted for | Inventory below; 170 owning regression tests, four Playwright integration tests, native and local browser checks pass | Verified |
| AC5 | Reported saved goal gets fresh executable evidence through the repaired packaged runtime | Goal 8e75d780 reached durable complete with two real browser tests and publication; preserved both teams' tests during publication conflict resolution | Verified |
| AC6 | Joined source passes checks and ships in desktop app, NSIS installer and shortcut | Post-work gate passed; NSIS rebuilt; refreshed shortcut/icon targets immutable app; five packaged UI smoke checks pass; all six changed packaged runtime owners match source; freshly packaged 269-character Chromium path passes with external-write denial | Verified |

Dependencies: AC2/AC3 use the settled AC1 launch contract; AC5 follows native and
browser fixes; AC6 joins every preceding item. The screenshots are diagnostic
evidence, not instructions to accept missing test evidence or answer a question.

The native audit covers the shared `run_appcontainer` owner and every caller:
Python/Node verification and boundary canaries in `swarm_work.py`, plus Chromium,
Node suite runner, browser closer, exact-origin proxy and proxy closer in
`playwright_runtime.py`. Native API declarations now preserve 64-bit handles,
failed waits/resumes/exit-code reads remain errors, and a timed-out drive lease
closes its handle. ACL diagnostic decoding tolerates the Windows console codepage.
The launch contract is stable and versioned, distinct from the process-specific
AppContainer SID used for ACL reuse.

Windows limits the executable portion when `CreateProcessW` receives a NULL
application name ([Microsoft API documentation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)).
Supplying an explicit extended path fixes the first launch. Real immutable-app
testing exposed Chromium's own long-path child-process failures as well. A
temporary, identity-checked junction to the immutable executable directory fixes
those without a runtime copy, registry change, 8.3-name dependency or global
drive lease. Native reparse-point creation preserves literal `%`, `&`, spaces
and Unicode in paths; cleanup removes only the junction after the contained Job
closes. Both length checks count UTF-16 units.

Ordinary local Playwright suites now use the existing contained in-process
WorkerMain when a literal probe cannot preserve their semantics, or when config,
filters or line selectors apply. Fixtures, loops, keyboard actions, canvas
evaluation and multiple tests no longer require rewriting into the small probe
grammar. The bundled static server serves directory indexes, JavaScript modules
and common web media with appropriate MIME types. Assertions use Playwright's
polling and normalization. Test selection excludes dependency/private folders,
preserves CLI filters/config, and serializes WorkerMain's shared process state.
Remote HTTPS suites retain their exact-origin proxy and TLS evidence checks.

Supported protected execution remains Python, Node and bundled Chromium. This
audit does not claim support for arbitrary native toolchains or other browser
engines, remove actual authorization decisions, bypass filesystem/network
boundaries, or mark missing evidence successful. Dynamic application servers
still need an executable setup compatible with those supported runtimes; the
new automatic local server serves static project assets. These explicit
capability boundaries are not evidence of a successful arbitrary user prompt.

The reported goal's earlier user decision remained resolved. Fresh checks ran
the tic-tac-toe and platformer suites successfully. Its publication initially
detected the other team's newer tic-tac-toe test. The recovery retained that
file unchanged, moved the complete platformer test into `megaman.spec.cjs` in
the private workspace, reran verification, and published the platformer through
the normal transactional goal publisher. No success criterion was waived.

Final current-request reconciliation: six outcomes verified, zero unmet, zero
abandoned. Independent browser/native review findings were corrected and
reverified. The deployment gate recorded session
`2026-09-09T06-46-44Z-ab22ec92fed242a494c648e705c10284`; subsequent packaged
startup and real contained-browser checks both passed against its refreshed
immutable desktop shortcut target. The installer contains the tested sources.

## Delivery claims and actual destination files (2026-09-09)

Current-request ledger (owner: primary agent; implemented sequentially because
publication, completion, and chat projection share one engine contract):

| ID | Required observable outcome | Owner paths / evidence | State |
| --- | --- | --- | --- |
| R1 | Explain when the claimed game became available | Authenticated goal/publication history and destination file timestamps: the 07:57 agent report preceded the 08:39 publication | Verified |
| R2 | Missing file evidence cannot complete; provider speech cannot serve as a delivery receipt | `goal_delivery.py`, `long_horizon.py`, `goal_workspaces.py`, chat projection and both chat renderers; portable missing/changed/source-copy/publication/restart tests | Verified |
| R3 | User can locate and open the reported game | Destination file opened directly with Chromium; drawing, movement, shooting, restart and no page errors checked; authenticated publisher hashes matched; existing chat upgraded through engine projection | Verified |
| R4 | Integration and desktop deployment pass | 329 Python tests and 61 desktop tests passed, followed by 28 focused checks on the final sources; deployment evidence is recorded by the mandatory post-work hook | Tests verified; deployment gated |

The incident was a premature provider delivery claim. Verification had kept the
result in a private goal copy, but an ordinary team-discussion bubble told the
user to open a destination file that had not yet been published. The previous
runtime repair subsequently verified and published the game. This session did
not create substitute game files or waive verification.

Completion now checks explicit file evidence against the current readable-file
manifest, including after a passing test result. Missing references schedule a
repair instead of completing. Before completion, `selected-project-delivery/v1`
reads named deliverables back from the selected project and compares their hashes
with the verified execution manifest. The receipt carries a schema, goal/epoch/
destination binding fingerprint, destination path, and file hashes. Publication
files are included independently of provider-written evidence. Existing isolated
completions recover that evidence from their authenticated publication receipt;
an unavailable or changed destination produces a delivery problem instead of an
invented receipt.

Agent reports remain available to peers unchanged. Engine-owned metadata marks
isolated-workspace replies as provisional delivery claims in both chat views.
Old projected replies gain the metadata without duplicate speech or loss of
history. The completion record exposes the destination and exact paths; a
versioned legacy readback can enrich the existing status at the same goal
revision without replaying the goal. Provider prompts explicitly distinguish
preparing a working copy from delivering files to the user.

## Delivery navigation and chat management (2026-09-09)

Current-request ledger, owned by the primary agent. Changes are sequential
because both delivery surfaces share metadata and all chat actions share the
conversation registry.

| ID | Required observable outcome | Evidence | State |
| --- | --- | --- | --- |
| Q1 | OPEN beside agent delivery destinations opens that dynamic folder | `desktop/chat-qol.test.js`: actual button dispatch and desktop IPC handler with arbitrary paths | Verified |
| Q2 | OPEN beside receipt paths opens the folder or reveals the file | Versioned engine receipt locations, actual completion renderer and IPC tests | Verified |
| Q3 | Right-click Archive uses the current archive action | Actual Chromium menu dispatch; existing chat archive regression suite | Verified |
| Q4 | Delete/Purge removes the local chat after stopping work, without deleting published files or resurrecting history | Portable transcript, attachment, private-goal, sibling, request-tombstone, backup recovery, draining and HTTP lease tests | Verified |
| Q5 | Rename persists a custom name without changing engine identity | Registry restart/binding assertions, real dialog and HTTP test during a turn | Verified |
| Q6 | Pin/Unpin persists and sorts at the top within the pair | Pair-local sidebar sort, reversed-pair persistence, actual Pin/Unpin dispatch | Verified |
| Q7 | Cancel dismisses the menu without a mutation | Chromium Cancel/Escape assertions; keyboard navigation and narrow viewport inspection | Verified |
| Q8 | Changes ship in the rebuilt desktop application | Mandatory post-work deployment gate and packaged smoke checks | Deployment gated |

Files are revealed in their containing folder, never executed by OPEN. Directory
paths come from the engine's selected-project metadata; file paths come from its
versioned delivery receipt. Electron validates the calling window and resolves
the current filesystem object before handing it to the operating system.

Chat names and pins are display fields on the existing canonical chat ID. Purge
waits for active turns/goals to release their ownership, removes conversation
content and private goal copies, and keeps minimal replay fences. Published
project files and other conversations are outside its deletion scope. Registry
recovery snapshots are scrubbed so an older backup cannot resurrect a purged chat.
Terminal communication-journal prompts, results and events are also erased;
versioned, sealed records retain only the identities needed to reject replay.

Verification: 60 existing Python tests (three skipped), six new engine/HTTP
tests, 65 existing desktop checks, and two new desktop/browser checks passed.
The final journal integration run passed 62 engine/HTTP/run-store tests.
The deployment hook records installer and shortcut evidence separately.
