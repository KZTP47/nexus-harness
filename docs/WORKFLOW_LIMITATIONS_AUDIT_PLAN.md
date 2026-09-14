# Workflow limitations audit and repair

Single current-request ledger under Nexus's project-specific unlazy contract.
No GATES.md or .unlazy state. Preserve existing uncommitted work, including the
completed team-attachment changes. Parent owns hooks, ledger and deployment.

| ID | Observable outcome | Owner | Evidence | Dependencies | State |
|---|---|---|---|---|---|
| L1 | Chat/composer restrictions assessed against supported engine behavior | UI investigator | Source map, portable reproductions, legitimate restrictions distinguished | None | verified |
| L2 | Team lifecycle/decision/recovery restrictions assessed end to end | engine investigator | Source map and portable positive/negative reproductions | None | verified |
| L3 | Provider/tool/file-delivery restrictions assessed for silent loss and unreachable capabilities | delivery investigator | Source map and portable reproductions | None | verified |
| L4 | Confirmed defects have concrete plan, ownership and acceptance checks with critique addressed | planners + cross-reviewer + parent | Separate planning phase and critique before implementation | L1-L3 | verified |
| L5 | Confirmed in-scope defects repaired in portable product source | assigned implementers | Positive, negative, persistence/changed-environment checks proportional to risk | L4 | verified |
| L6 | Independent fix critique resolved and parent verifies integrated behavior | reviewers + parent | Rerun leaf checks and branch regressions, manual/browser evidence | L5 | verified |
| L7 | App/installer rebuilt and shortcut/icon refreshed | parent | Mandatory post hook plus packaged source verification | L6 | verified |

## Scope and classification

Audit Nexus-owned restrictions that prevent already-supported user workflows:
disabled/rejected inputs, UI/API mismatches, silent omission, irreversible-looking
dead ends where safe continuation exists, and inconsistent provider/tool routing.
Examine chat UI, shared goals, decisions, recovery, provider delivery and file/tool
access. This is a broad functional audit, not a claim to enumerate every defect
in external services or to add all unsupported product features. Evidence-backed
constraints protecting ownership, approvals, integrity or real provider limits
are retained. No real user request is resent to a live provider during analysis.

## Dispatch

Analysis wave: three read-only leaves, launched before any wait. UI owns composer
trace; engine owns lifecycle/decision/recovery trace; delivery owns provider/tool
trace. Parent independently examines cross-surface contracts and reproductions.
No implementation before a separate subagent planning phase and plan critique.
Later implementation paths are assigned only after interfaces settle.

## Findings, plan and verification

## Confirmed findings and planned outcomes

| ID | Defect / observable repair | Owner | Evidence | State |
|---|---|---|---|---|
| F1 | Singleton Work and existing-goal followups reach the already-supported one-agent durable engine | UI | Both composer handlers plus one-participant runtime; exact participant isolation | verified |
| F2 | Ordinary screenshot/file-only sends use neutral review prompt; failures preserve original empty draft | UI | Both views, native input bytes, explicit-text and empty-negative checks | verified |
| F3 | Settled complete/cancelled goals can fork; source stays immutable | engine + UI | Actual clean temporary Git fork, negative dirty/divergent/live/effect/decision/config cases | verified |
| F4 | Accepted directed messages reach their exact recipient before completion, including running/completed targets | engine + UI | In-flight provider reproduction, durable pending generation, restart/retry/privacy/effect tests | verified |
| F5 | Mission Control failures/drift cannot erase unsent steering/message drafts | UI | Actual callback reproduction; acceptance/refresh/selection/new-draft races | verified |
| F6 | Earlier ordinary-chat text/DOCX/images remain usable within disclosed request budget | delivery + UI | Two-turn original-byte/extraction tests, restart, ownership, bounded omission, user notice | verified |
| F7 | Packaged desktop Python launches do not write bytecode into product source | engine + independent delivery review | Actual isolated interpreter import plus startup/trust argv and environment tests | verified |

Reproductions: solo Work issued zero requests in both views although a real
one-participant mocked-provider goal completed. Terminal Fork is disabled and
isolated terminal engine fork requests an impossible pause. A targeted message
injected during dispatch was accepted but never appeared in recipient context;
completed/invalid targets likewise had no runnable delivery. Both rejected and
provider-drift Mission Control sends cleared the draft. TXT/DOCX/PNG originals
disappeared on ordinary followup while ZIP positive control remained accessible.

## Concrete plan submitted for critique

### UI

Permit singleton Work and goal lookup, but retain actual multi-agent collaboration
requirements. Ordinary file-only chat gets the neutral attachment-review prompt;
preserve raw typed draft separately from generated outgoing text. Give Fork its
own settled-state eligibility. Mission Control returns an explicit acceptance
result, separates refresh failure, and clears only matching captured goal/text.
Directed messages freeze exact request envelope and stable request ID until a
verified receipt. Display safe attachment-context omission notice on both live and
restored replies. Do not consume unrelated composer files from decision cards.

### Directed messages and forks

Introduce versioned goal_messages helper with stable goal/project/task/recipient
ownership, exact submission receipt and bounded immutable records. Validate target
and pairing before acceptance. Freeze accepted/claimed/delivered sequence per task;
mark covered messages delivered only after provider acknowledgment. Incoming input
must not discard an in-flight native effect/transaction/review. Reopen settled
completed targets with unread messages; completion cannot pass while unread work
remains. Scope continuation invalidation to the recipient task. Pending private
messages cannot be silently delivered through reassignment/failover. Exact retry
does not duplicate events or dispatch; scheduling failures preserve acceptance.

HTTP directed_message_receipt: schema_version=1, accepted=true, request_id, goal_id,
task_id, agent_id, submission_sha256; optional separate scheduling_error.
For isolated forks permit settled paused/failed/complete/cancelled source while
preserving authority/setup, clean Git, copy equality, live/effect and decision
checks. Child remains paused and independently bound; keep attachment inheritance.

### Historical attachment context

Retain canonical originals, scope resolution to exact chat IDs, validate hashes,
and reuse interpretation without creating duplicate copies. Current explicit
inputs take priority. Select newest complete historical turn/input bundles within
the existing bounded chat-history budget and a bounded hydrated-input budget;
omit whole historical bundles that cannot fit, with explicit provider AND user
disclosure. Missing/tampered historical files are unavailable (never supplied as
original evidence) and disclosed without wedging unrelated chat. Newly invalid
input still rejects. Reattachment makes an older file current/highest priority.
No cumulative lifetime upload cap and no silent truncation. Persist safe notice
under assistant correlation.attachment_context for reload visibility.

## Ownership after critique

Independent plan critiques completed before implementation. Engine review of F6
requires canonical chat rather than speaker-route ownership; actual rendered
history (including questions and disclosure) must fit the historical budget;
current valid inputs keep their own admission limits. Resolve and confine actual
regular files, reject symlink/substitution/hashless originals, and select bounded
candidates before hydration. Preserve duplicate turn/name meaning and persist
only the exact successful request's safe notice. Test shared-chat changed speaker,
foreign IDs, question-heavy budget, current priority, unavailable originals and ZIP.

Delivery review of F1-F5 requires the actual dispatched message high-water mark,
transactional unread barriers including facilitator completion, settlement of
existing effects before reopening, and cancellation/fork lifecycle (no inherited
parent receipts or live dispatch claims). Stable private recipient ownership must
survive reviewed transport changes but block silent reassignment/failover. Freeze
the whole UI submission envelope; receipt and scheduling/refresh success differ.
Retain all negative fork guards. Test actual handlers in both composers.

Implementation wave: all three independent leaves ready after interface review;
launch UI, engine and delivery before waiting. Parent owns an independent real
HTTP integration test file, tests/test_workflow_limitations_api.py.

UI: app.js and dedicated/focused desktop tests. Engine: long_horizon.py,
goal_messages.py, server.py and dedicated engine tests. Delivery: chat.py and new
history-input helper/tests. Parent: this ledger and independent integration.
Shared interface changes must be settled before the implementation wave.

## Retained constraints / checked non-findings

Ownership, provider drift, explicit risk decisions, pending snapshots, cancellation
draining, divergent fork work and real native-image limits remain enforced.
Decision cards do not auto-consume a separate composer draft. Cooperative graph
macro exclusions have no proven equivalent implementation and are not removed.
Browser attach uses a fixed preparation delay, but downstream disabled-send checks
exist; no actual supported-provider loss was reproduced. That is an unconfirmed
hardening candidate, not a repair claim; generic chip gating could add false blocks.
Broad PDF understanding and undeclared CLI screenshot capability are not added.

## Implementation review and independent evidence

Three separate planning leaves completed, followed by independent cross-critiques,
then all three implementation leaves launched. All leaves performed four passes.
Cross-review of completed code caught and resolved: ordinary text-only history
projection regression; omission-disclosure budget overhead; definitive stale
recipient rejection trapping the frozen UI envelope. Parent review also corrected
forked message capacity, credential-redacted evidence, exact public receipt digest,
and edited/restored uncertain request identity. No review finding is waived.

Directed receipts hash canonical exact public goal/task/recipient/text separately
from private ownership and redacted stored evidence. Explicit rejection proofs are
issued only after a known validation error and authenticated readback without an
accepted matching request ID. Unknown outcomes and accepted-ID conflicts remain
frozen. Historical originals use versioned bounded request projection, not a
cumulative chat lifetime cap.

Parent evidence so far: 135 actual UI/Chromium checks pass; neighboring desktop
chat/navigation/permissions/layout suites pass; 36 joined engine/HTTP/team-input
checks pass. The independent screenshot test verifies identical original bytes in
both actual OpenAI payload formats after reopening, plus foreign-chat isolation.
154 joined chat/history/fidelity/loading checks pass. Final ordinary-history budget
change is being reverified with its dedicated and full conversation tests.

Broad engine run: 353 tests, 350 passing test methods; three existing test methods
need contract-aligned fixture/assertion updates. The handoff privacy fixture must
record the actual dispatch/acknowledgment before transferring a message-bearing
task; recovery tests must compare durable peer state separately from public
presentation metadata. Engine privacy/completion guards are not weakened.

Final parent verification: current UI 135/135 (zero skips), ordinary chat plus
attachment projection/native payload 122/122, integrated decisions/recovery/new
messages/HTTP/previous team attachments and repaired lifecycle regression 81/81.
Independent reviews explicitly closed both history findings and the rejected
recipient retry finding. Broad rerun covered 369 methods: 368 passed and one
assertion reflected a concurrently changed format-repair policy. The authorized
test now asserts exactly four calls, retained peer completion and no verification;
its rerun and the current repair-policy suite passed 9/9. This is not recorded as
an all-green monolithic 369 run. Earlier 154 chat checks and neighboring desktop
suites also passed. Source snapshot/hash checks preserve concurrent product edits;
last observed repair-binding change received the focused 9-test recheck.

Deployment discovered F7: an existing preview running directly from build-output
created Python 3.11 caches during packaging; the mandatory source-privacy check
correctly rejected the first post hook. Independent engine analysis and delivery
plan critique confirm python311._pth ignores environment variables. Approved fix:
explicit -B for both startup and trust probes, plus copied child environment
PYTHONDONTWRITEBYTECODE=1 before early return. Verify actual isolated imports.
Do not weaken the privacy gate or stop existing user processes without authority.
The first failed gate does not close the session; deployment remains pending.

F7 implementation and independent fix critique complete. Parent reran desktop
server suite: 58/58, zero skips. Both captured startup/trust flags run real -I
temporary imports with conflicting environment settings, verify bytecode disabled
and leave no cache. Privacy check remains intact. Retrying mandatory deployment.

## Final closeout

Second mandatory post hook succeeded with receipt
`Sessions/2026-09-14T10-31-36Z-0e07677a7e044f5f94ef966b05775725.md`.
App and NSIS installer rebuilt; desktop shortcut and executable icon refreshed.
Parent verified all six changed Python/UI source hashes match tested source in
both build-output and the actual shortcut runtime. Desktop server.js extracted
from both Electron archives matches the tested bytecode-prevention launch code.
Shortcut icon points to its new target executable. No packaging privacy bypass.

Final denominator: L1-L7 and F1-F7 = 14 met, 0 unmet, 0 abandoned.
Seven portable fixes delivered. Existing concurrent work preserved. Live provider
accounts were not exercised; provider payloads and lifecycle boundaries used
portable temporary projects and mocked network/provider replies.
