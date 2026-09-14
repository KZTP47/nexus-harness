# Background assistance without interrupting collaboration

Current request: implement the assessed improvements while keeping agent-to-agent conversation and collaborative file creation/editing intact and uninterrupted by these additions.

Owner: primary agent for every requirement. This is the single implementation ledger; no delegated work or second progress hierarchy.

| ID | Outcome | Owners and dependencies | Evidence | State |
| --- | --- | --- | --- | --- |
| T1 | Opt-in timers skip unchanged inputs and stale occurrences; settings changes invalidate observations; existing timers retain behavior | timer.py, timer observation helper, timer UI | Positive/negative/restart/config-change tests, API/UI checks | Verified |
| M1 | Optional local hybrid memory uses versioned, source- and model-bound vectors; unavailable/slow embedding returns lexical results without waiting; mandatory notes and project binding remain | persistent_memory_index.py, optional embedding helper, config and hook integration | Synthetic labeled queries; edit/delete/model/root changes; offline fallback; bounded context regression | Verified |
| H1 | Delivery stage and overdue/failed handoff visibility are observational; no new replay, stop, budget or permission policy | agent_mailbox.py, swarm status and UI | Status purity, restart/failed-delivery tests, stage rendering | Verified |
| C1 | Agents continue exchanging messages and creating/editing shared project files with these additions; pause/resume and uncertain outcome recovery keep their existing meanings | Joined owners, existing collaboration engine unchanged | Focused integration plus packaged team-chat and concurrent-goal acceptance | Verified |
| D1 | Documentation/configuration are usable and portable; required app/installer/shortcut gate passes | All prior items | Diff review, focused suites, packaged acceptance, mandatory post-work deployment | Verified |

Implementation principles: optional background work never acquires a goal's scheduler or project publication lease; it does not restart agents, pause conversations, inject turns into active chats, extend budgets or introduce approval steps. Preserve the provider-neutral mailbox and goal engines. Embeddings run in bounded background work with lexical fallback, not in the agent turn's critical path. Existing timer behavior remains the default.

## Evidence recorded on 2026-09-14

All commands ran from the owning repository (desktop commands from `desktop/`) and exited zero unless explicitly noted below. Python checks used `PYTHONPATH=src;tests`.

- T1: `python -m unittest tests.test_memory_context_refinement tests.test_running_on_a_timer` — 134 passed at that checkpoint. Additional panel roundtrip and local HTTP tests subsequently passed in `python -m unittest tests.background_assistance_checks tests.test_timer_server` — 32 passed. Final background helper run — 14 passed. Upgrade regression seeds a pre-change accepted request and proves it is deferred rather than dispatched twice.
- M1: final helper tests cover cold in-process restart over persisted vectors, lexical-miss synonym recall, real loopback HTTP adapter, stalled/offline model fallback, same-mtime edit, deletion, different vault, changed model, and binding/model changes during embedding. Config suite — 49 passed after correcting a test fixture to use a portable absolute path. No installed-model quality claim; optional semantic retrieval remains disabled in this user's configuration.
- H1: mailbox byte equality before/after observation, active delivery without age interruption, failure/ack projection, and goal projection purity passed. Existing mailbox, dialogue, collaboration recovery, swarm-run, and swarm-chat regressions passed their assertions. A combined run initially exited nonzero due to a nonexistent test module name; a later combined run had the invalid relative-path fixture described above. Corrected owning suites passed; neither failed selection is counted as a clean suite result.
- C1: fixture agents exchanged A → B → A replies, created and edited the same file, and completed while the embedder stayed stalled and timer ticks ran. Packaged `node team-chat.smoke.js` emitted `TEAM_CHAT_PACKAGED_ACCEPTANCE_PASS`: substantive dialogue, file publication, restart/resume, generated unit/API/browser checks, and negative broken-scoring oracles passed. Packaged `node long-horizon.smoke.js` passed same-project concurrent goals, checked publication while another goal ran, no overwrites, exact participant dispatch after admission recovery, and reopened chats. Providers were deterministic scripted fixtures; the actual packaged runtime, scheduler, files, and UI executed.
- D1: full desktop `npm test -- --test-reporter=dot` passed. After adding the timer form test, `node --test --test-reporter=dot team-chat-ui.test.js` passed 48 tests. `npm run build` produced the app and NSIS installer. `git diff --check` passed. The packaged conversation screenshot was visually inspected. Usage and limitations are in `docs/BACKGROUND_ASSISTANCE.md`. Final mandatory post-work rebuild/shortcut gate exited zero; app, NSIS installer, desktop shortcut and icon refresh verified by the hook.

The full initial timer/mailbox/persistent-memory selection passed 137 tests; the config/swarm run/chat selection passed 137 tests with 3 existing skips. No runtime scheduler, publication, approval, retry, or provider-call-budget policy was changed. Tests use temporary project roots and private fixture stores.

## Closeout

Required T1, M1, H1, C1 and D1 outcomes are verified. The mandatory post-work hook completed successfully at 2026-09-13T23:59:26Z and recorded the session in the project-bound private vault. The final app and installer were rebuilt and the desktop shortcut/icon refreshed. No required work remains. No commit, push, release, model installation, or local hybrid-memory activation was requested or performed.
