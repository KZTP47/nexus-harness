# Tooling and relay audit and repair

Single current-request ledger. Applies unlazy acceptance, four-pass review and parent verification through Nexus's project-required plan; no duplicate GATES.md/.unlazy hierarchy.

## Acceptance gates

| ID | Observable outcome | Owner | Check / expected evidence | State |
| --- | --- | --- | --- | --- |
| A1 | Tool execution/catalog/research/MCP surfaces catalogued, including concrete defects and examined non-findings | tool auditor + primary | source paths, isolated reproductions, confidence and impact | verified |
| A2 | Relay routing, continuation, cancellation, persistence and provider handoff surfaces catalogued | relay auditor + primary | no duplicate delivery, route/context identity and recovery evidence | verified |
| A3 | Desktop/browser bridge, connection and visible relay status surfaces catalogued | desktop auditor + primary | portable fixtures, stale-tab/route and status evidence | verified |
| A4 | Cross-surface catalogue reconciled into prioritized repair plan; independent plan critique addressed before fixes | primary + independent reviewer | reviewed catalogue with explicit scope and evidence for each finding | verified |
| A5 | Every confirmed in-scope defect fixed portably in product-owned source | assigned implementer then primary | positive, negative, restart and changed-configuration checks proportionate to risk | verified |
| A6 | External dependency decision documented; additions only if needed and safely distributable | primary | demonstrated benefit, provenance/license/version and packaged verification if added | verified |
| A7 | Independent fix critique resolved; primary reruns leaf evidence and integrated regressions | independent reviewer + primary | runnable checks pass; no unresolved material findings | verified |
| A8 | Final request denominator reconciled and packaged deployment completed | primary | app/installer rebuild, shortcut refresh, packaged source/runtime verification and post receipt | verified |

## Ready wave 1 — read-only discovery

| Leaf | Owns | Needs | Tier | State |
| --- | --- | --- | --- | --- |
| tool-audit | harness_tools.py, agent_tools.py, research_tools.py, public_web.py, mcp.py and focused tests | A1 audit contract | judgment | complete |
| relay-audit | chat.py, relay modules, provider adapters, continuation/reconciliation and focused tests | A2 audit contract | judgment | complete |
| desktop-audit | desktop web-chat/bridge/provider plumbing and associated UI status/tests | A3 audit contract | judgment | complete |
| primary | shared long_horizon/server integration, existing requirements and audit consolidation | current source and session hook | judgment | complete |

No discovery agent edits product source. Reproductions must use arbitrary temporary roots and synthetic providers; never resend a user's real request. Parent owns hooks and deployment. Existing dirty changes must be preserved. Findings need a failing behavior and clear impact; suspicions and environment limitations are catalogued separately. Later implementation ownership will be assigned only after plan critique. External tools/repos are authorized but optional, not an objective by themselves.

## Scope

Audit Nexus-owned tool and relay contracts and their main integration boundaries, not a claim to enumerate every possible defect in external providers or third-party websites. The prior research/wait fixes are regression context. Exercise supported paths available in this checkout and disclose unavailable live-provider boundaries. This is a functional/reliability audit; any encountered security issue must be handled with evidence and containment rather than expanding silently into a repository-wide security scan.

## Confirmed catalogue and reviewed repair plan

Independent discovery completed four passes (dataflow, boundary cases, isolated reproductions, existing regressions). Desktop auditor independently critiqued the consolidated plan before implementation and approved with the conditions below. Primary reproduced duplicate browser evaluation, missing relay context, partial receipt commit and incorrect command outcome directly.

| ID | Priority | Defect / portable repair | Owner | Acceptance evidence |
| --- | --- | --- | --- | --- |
| T1 | P2 | Local skill paging schema cannot accept returned cursor; share bounded file paging contract | tools | complete long UTF-8 skill; bad/stale cursor; non-skill rejected |
| T2 | P2 | Git history/diff rejects deleted paths; allow absent paths while preserving confinement | tools | deleted file/directory; existing file; ignored/traversal paths |
| T3 | P2 | MCP isError treated as successful execution; normalize typed failure and invalidate obsolete cached results | tools | both aliases; success/error; restart/config identity |
| T4 | P1 | Research/public-web/navigation/MCP stages replenish or bypass deadlines; propagate remaining budget and bounded cancellation | tools | short-budget fake transports; stalled DNS/I/O; fresh sessions |
| T5 | P1 | Language server stdin/cleanup can block before timeout begins | tools | no-read peer; cancellation; normal peer; bounded cleanup |
| R1 | P1 | Browser continuation drops original goal/history; serialize role-labelled full supplied history | relay | multi-round research, fresh/reconnected route, literal content |
| R2 | P1 | HTTP non-streaming calls outlast deadlines and cancellation | relay | stalled headers, drip success/error, cancel, normal response, late cleanup |
| R3 | P2 | Malformed receipt timing partially commits completion | relay | malformed/nonfinite timing, duplicate/late receipt, atomic completion |
| D1 | P1 | Post-preflight browser operations can hang past deadline/Stop | desktop | hung submit/poll/stop; no late activation; uncertain delivery preserved |
| D2 | P1 | External evaluate automatically replays mutating actions | desktop | context-loss sends once; normal evaluation succeeds |
| D3 | P1 | Channel Stop/reset/remove leave queued work live | desktop | obsolete queue rejected; fresh work allowed; channel isolation; failed durable removal preserved |
| D4 | P1 | Active external relay adopts an unrelated tab or navigates before Send; pin page and pre-activation conversation identity | relay; desktop reviewer | zero foreign sends/reads, generic-page drift, idle recovery |
| I1 | P2 | Engine run_command nested exit failure reported finished | primary | recognized command envelope fails; read-file JSON remains untrusted data |

Plan critique conditions: fence every late activation step (not just outer Promise); preserve uncertain delivery; do not retry writes; bound cleanup and worker growth; propagate one absolute deadline; preserve safety/redaction/size limits; only interpret owned command envelopes; invalidate affected cached tool contracts. Desktop cancellation scope verified against route/conversation-key IPC ownership. The initially unconfirmed replacement-tab risk was later reproduced as D4 and received a separate plan critique before repair. Existing environment-dependent Codex executable discovery test is tracked separately.

Ready implementation wave: tools owns agent_tools/harness_tools/research_tools/public_web/navigate/mcp and new focused tests; relay owns providers/base + web_chats and new tests; desktop owns desktop/web-chats + external-browser and new tests. Primary owns goal_chat_progress, shared integration, ledger and deployment. Implementers must not touch each other's paths or run lifecycle hooks.

External dependency decision (A6): no addition required. Existing standard-library transport and browser integration can enforce these contracts without another bundled runtime; adding repositories would not repair the demonstrated ownership defects.

## Implementation and review history

- D1-D3 implemented; primary reran all 108 desktop lifecycle/relay checks successfully.
- R1-R3 implemented; primary reran 67 relay/projection integration checks successfully and independently exercised a real localhost drip response (0.133s elapsed for 0.12s budget).
- I1 implemented: recognized command receipt exit failure/timeout now projects failure, with file-content negative controls and saved-chat restart; 41 focused progress/discovery tests passed. Existing discovery fixture now isolates both managed-install discovery sources, eliminating reliance on this machine's Codex installation.
- Independent fix review found R2 blocker despite passing timing tests: a stalled DNS lookup can resume inside urllib and SEND after the caller has already timed out. R2 was reopened; the owner subsequently fenced actual connect/send after DNS/TLS and demonstrated zero late HTTP requests. Bounded caller return alone is insufficient. Review continues across disjoint author/reviewer ownership.
- Agent creation reached the environment's thread limit. Existing audit agents are performing independent fix reviews of code written by the other agents; none reviews their own implementation for acceptance.

## Examined boundaries and limits

- Tool audits checked catalog validation, file ignore/confinement, exact-edit proposals, live search, notebook metadata, MCP allowlists/read-only annotations and pagination, public-address validation, immutable GitHub reads and archive ownership. No additional confirmed defect in those checks.
- Relay audits checked physical-session fences, cancelled/late receipt handling, deduplication, route/principal/config fingerprints, CLI monotonic deadlines and continuation identity checks. No additional confirmed defect in those checks.
- Desktop audits checked exact reconnect keys, persisted URL validation, marker matching, partial-answer suppression, immutable receipt retries, durable rollback and preflight recovery. No additional confirmed defect in those checks.
- Tests use temporary projects, synthetic browser peers and loopback HTTP services. External provider DOM/account behavior and every native continuation adapter were not exhaustively exercised. Replacement-tab adoption was subsequently reproduced and added as D4; live provider DOM/account changes remain outside the synthetic coverage.
- OS DNS may remain blocked in a bounded worker after cancellation. Admission limits prevent unbounded growth; post-resolution send fences prevent a late model/tool request. This is bounded containment, not a claim that Python can forcibly terminate OS DNS.

## Operator recovery guidance

Timeout means the local request exhausted its allowed wait, not necessarily a broken login. Use the actual provider error/route diagnosis; a free availability check does not prove a model response will succeed. For a stuck browser turn, Stop cancels the channel's outstanding/queued work. Check the provider conversation if submission is uncertain before choosing a fresh request; Nexus must not silently resend it. Reset/reconnect the intended conversation only when its binding is wrong. Tool errors now retain failure status: correct the reported arguments/path, use read_file for ordinary files and read_local_skill only for SKILL.md, and follow returned cursors for the remaining content. These engine bugs require the rebuilt Nexus app; editing local caches is not the repair.

## Additional confirmed finding D4

The final bounded replacement-tab fixture converted the deferred risk into a confirmed P1: original tab closes after preflight, the external adapter adopts an unrelated focused provider tab, and trusted submission clicks Send there. D4 is now in scope (13 confirmed catalogue items total).

Independent plan critique approved pinning exact page identity after readiness but before final preflight, without fallback adoption during pin acquisition. All active operations must honor the pin; token-scoped release prevents old cleanup clearing a newer turn. Preserve idle OAuth adoption and same-page expected navigation. Owner: relay agent (desktop files now free); reviewer: desktop agent. Required tests: original closure during baseline/selection, zero foreign send/read, post-submission closure, same-page behavior, idle reconnect. Primary reruns integrated desktop evidence afterward.

Review also caught a proposed MCP cache upgrade breaking legacy remote-receipt replay. T3 was held open until migration corrected failure status without redispatching completed calls or invalidating unrelated paused sessions; the final review approved that correction. The original global identity version is retained; migration evidence must include older checkpoints and pending/unknown receipts.

## Review reconciliation

R2 late DNS POST blocker is repaired through shared guarded HTTP handlers used by both provider and MCP transports. Reviewer reproduced the original case and verified zero POST after timeout; tests also cover Stop. T3 global-identity incompatibility, legacy receipt redispatch and changed-route memory-cache findings are repaired: global identities stay unchanged, typed failure presentation migrates on read, existing receipts never cause automatic redispatch, route fingerprints distinguish new observations. Legacy receipts with unknown configuration are labelled explicitly. Reviewer reran 38 tooling/identity/staging tests successfully and approved T1-T5. R1-R3/I1 independently approved; D1-D3 independently approved by their non-author.

Primary integrated Python verification: 233 tests ran successfully (one POSIX-only containment test skipped on Windows). This joins actual tools, journal/restart identity, research, navigation, staged/full access, provider HTTP/MCP, browser broker and chat projection. An additional 101 research/navigation/full-access/recovery tests passed. Primary full desktop run before D4 passed452; final D4 integration is required before deployment.

D4 fix review additionally reproduced same-page generic /new -> unrelated conversation drift before Send. The repair now fences generic and specific conversation identity until explicit activation while permitting legitimate conversation creation afterward. A deterministic poll-deadline boundary test also fixes a timing race that previously mislabeled normal accepted/unknown timeouts as generic browser failures. Both changes use the existing canonical timeout/Stop path; assertions were not merely weakened. Final independent D4 recheck and the complete 460-test desktop rerun subsequently passed.

## Final source acceptance

All 13 confirmed defects (T1-T5, R1-R3, D1-D4, I1) are implemented and independently reviewed. D4 reviewer reproduced generic drift and observed zero insertions/clicks; no remaining material review findings. Final parent full desktop run: 460 passed, zero failures/skips, 88.50 seconds. Final integrated Python run: 233 tests successful, one POSIX-only test skipped on Windows. git diff --check passes. No external dependency added, no real user/provider task replayed, and unrelated working-tree changes preserved. A1-A7 verified; A8 requires the deployment hook and packaged fingerprint/import checks below.

## Deployment closeout — verified

Mandatory post-work hook succeeded: receipt `Sessions/2026-09-14T08-36-53Z-cfa05291869745d7b2c6658f6d7a6f61.md`. Rebuilt app and NSIS installer `desktop/build-output/Nexus-Harness-Setup-0.2.27-UNSIGNED-DEV.exe`; recreated the visible desktop shortcut and refreshed its icon from the new executable. The shortcut targets the new versioned runtime copy under `.harness/runtime/desktop-apps/`.

Primary verified all 10 repaired Python source fingerprints and imports with the bundled Python runtime, plus exact `web-chats.js` and `external-browser.js` contents inside app.asar. Repeated those checks against the actual versioned app copy named by the desktop shortcut, not just the build directory; all matched reviewed source. All A1-A8 gates are verified. Close and reopen Nexus using the refreshed desktop shortcut to run the repaired build. External provider website/account behavior remains limited to the synthetic and loopback coverage described above.

## Follow-up F1 — streaming transport parity

Current request: identify additional fixable tooling/relay issues. Prior authorization for portable fixes and independent reviews remains applicable. Narrow follow-up denominator:

| ID | Outcome | Owner | Evidence | State |
| --- | --- | --- | --- | --- |
| F1a | Streaming cannot send after timeout/cancelled DNS; worker growth/cleanup bounded | relay; independent desktop reviewer | real loopback delayed DNS, cancellation, cap/close tests | verified |
| F1b | Streaming error-body reads and decoded yields honor deadline/cancellation | relay; independent desktop reviewer | delayed errors, incremental decoder and cancellation tests | verified |
| F2a | Resource results remain complete JSON or fail explicitly under fresh/replay output budgets | tools; independent desktop reviewer | listing/templates/read UTF-8, exhausted budget, cache/restart | verified |
| F2b | Resource pagination works with session-bound server cursors and rejects stale local cursors | tools; independent desktop reviewer | single connection, bounded snapshots, config/restart/cyclic cursor tests | verified |
| F3 | Independent plan/fix critiques and parent regressions | primary + reviewers | source review and runnable tests | verified |
| F4 | Deployment post-hook and actual shortcut-runtime parity | primary | rebuilt installer/app, module/ASAR checks | verified |

Primary reproduced streaming late send (request sent after returned timeout) and .351s HTTP error-body read with .05s budget. These are in providers/base.py _stream_lines, distinct from previously repaired _post. Proposed repair: guarded dispatch and retained worker admission, worker-owned error reads/close, nonblocking cancellation shutdown, shared deadline, preserving incremental decoding/redaction/limits. Await independent plan critique before implementation. No external tools or accounts needed.

Follow-up F2 discovery confirmed two MCP resource defects: low remaining output allowance clips JSON while reporting success; listing reconnects between server cursor pages, breaking session-bound cursors. F2 repair proposal: bounded server-page collection within one connection, bounded per-tool-session DATA snapshots with local method/server/config-bound cursors (no retained live processes), explicit stale-cursor errors, and complete JSON envelopes or output-budget error for listing/template/read results. No global identity change. Independent plan review requested before edits.

Both follow-up plans independently approved before edits. Streaming must share admitted-worker bounds and fence decoded yields; resource envelopes and cursors must be validated on replay as well as fresh dispatch, without global identity changes.

Follow-up streaming implementation independently approved. Primary reran streaming, earlier relay repairs, cognition/MCP and credential-redaction regressions: 89 tests successful, one POSIX-only skip. Covered real delayed DNS timeout/cancel no-send, slow error body and cleanup, shared capacity, start failure, queue/generator closure and buffered-frame cancellation. No product desktop code changed in this follow-up.

Follow-up source acceptance: all four additional confirmed defects repaired. Independent streaming and resource plan/fix reviews approved; terminal-page cursor replay and tiny-error-budget review findings resolved. Parent combined regression: 324 tests successful with one POSIX-only skip (71.36s); after the last added resource test, parent reran all42 resource/harness/agent/workflow identity checks successfully. Parent also reran89 streaming/relay/cognition/redaction checks with one platform skip. No new dependencies or desktop source changes. Resource snapshots retain at most8 snapshots/2MB; no live clients persist between model tool calls. Collection-limit exhaustion is an explicit incomplete-enumeration error, never a successful partial catalogue. F4 remains gated on deployment and actual shortcut-target verification.

Follow-up deployment closeout: mandatory post-work hook succeeded with receipt `Sessions/2026-09-14T08-55-59Z-74acfa24ecc34389a6bf8d29f3d33eb7.md`. App and NSIS installer rebuilt; desktop shortcut and executable icon refreshed. Parent verified all 10 repaired Python module imports/source fingerprints and both desktop ASAR source fingerprints against the actual shortcut target, versioned runtime `build-130c028b39d771a0127bbc7e4983d65e16017fbfb292ee4691ec8a9737652440-5d379c2b609c`. All F1a-F4 outcomes are verified. Close and reopen Nexus using the refreshed desktop shortcut to load this build. Verification used synthetic peers and loopback services; live provider accounts were not exercised.
