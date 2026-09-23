# Audit: Agent Swarm and Email assistant tabs (2026-09-23)

Scope: the AI Agent Swarm orchestrator (board, pair chats, goal queue, collaboration engine) and the
Email assistant (inbox sync, automatic drafting, review UI). Every item below was checked against the
code; most were reproduced with a small script or a failing test before being fixed.

Guiding rule from the owner: *Nexus must never get in the way of the tool calls and the work the AI
agents want to do. Nexus is not smarter than the agents.*

## New feature: corner notification for new mail

When the inbox check finds new mail and the assistant starts drafting a reply, a Steam-style card
slides up in the bottom-right corner of the screen: the sender, "New email · Nexus AI is drafting a
reply", and the subject. Clicking it opens Nexus on that email. The card never takes keyboard focus.

- Server: `EmailService._announce_drafting` records a bounded in-memory feed when `_scan_account`
  queues an automatic draft; `GET /api/email/notifications?after=<seq>` reads it (never opens or
  creates a mail store). A restart gets a new `boot` mark so pages reset their cursor.
- Settings (saved per project): **Corner notification when new mail arrives** and **Show sender and
  subject** (turn off when screen sharing). Manually imported mail is not announced.
- Desktop: `desktop/mail-notifier.js` owns one frameless, always-on-top, non-focusable window
  (`pages/mail-toast.*`, own preload `mail-toast-preload.js`). Main-process sanitising, sender checks
  and a strict navigation guard. Browser mode shows the same card inside the page.
- Every panel page watches the feed from boot (`nexusEmail.watch()`), whatever tab is open. A burst
  of more than three becomes one summary card ("5 new emails").

## Fixed in this pass

### Email assistant
| Severity | Problem | Fix |
|---|---|---|
| High | One malformed message (empty body, attachment-only, bad header) made every later sync fail on it, stopping all new mail and drafts for that mailbox. | `_ingest_or_record`: each message is stored or recorded as a failed import; the sync continues. |
| High | A decoded sender such as `Müller, Hans <h@x.de>` was rejected on Python 3.13 ("Enter a valid email address"), again stopping the sync. | `_address` falls back to the exact angle address. |
| High | New IMAP connections drafted replies for the whole existing inbox (oldest first), delaying real new mail by hours. | New or reconfigured IMAP accounts set `auto_draft_since`; the Date header becomes `received_at`; older or undated mail is history. |
| Medium-high | HTML-only mail was drafted from a placeholder the assistant never saw. | HTML is converted to plain text (never rendered); mail with no readable text is refused. |
| Medium | A multi-address Reply-To was accepted on import, so the approved reply could never be sent. | Refused on arrival with the connectors' single-recipient rule. |
| Medium-high (UI) | A draft that finished after the email was opened never appeared in the open email. | `render()` adopts the newest draft for the open message. |
| Medium (UI) | Keyboard focus dropped to the page on every click and 6-second refresh of the inbox list. | Focus is restored to the same message. |
| Low (UI) | A failed refresh retried every second; an action said "Saved." even when its refresh failed. | Back-off after failure; the real outcome is shown. |

### Agent Swarm: engine (`swarm_work.py`, `agent_tools.py`, `goal_verification.py`)
| Severity | Problem | Fix |
|---|---|---|
| High | Reaching the tool-call limit was treated as a provider failure and rolled back every file edit the run had applied. | The limit is returned to the agent as a tool result; nothing is rolled back. |
| High | A rollback conflict was overwritten as "committed", so the next run started on a half-rolled-back tree. | `complete()` keeps `compensated` / `rollback_conflict`. |
| High | A command that exits 0 but prints "No module named …" (e.g. an optional accelerator warning) was marked failed. | Only non-zero exits are classified that way (both engines). |
| Medium | Goal text such as `localhost:3000`, `std::vector`, `10:30` or `// comments` aborted the run before any agent was asked. | Only real path tokens are validated. |
| Medium | `glob:**/*` failed completely in any Git repository. | Control paths are skipped, not fatal. |
| Medium | Paths written as `./src/a.py` were rejected; `./secret.txt` also slipped past the protected-path check. | Paths are normalised before every check. |
| Medium | One oversized newest turn removed all recent turns from the prompt. | Newest turn is clipped with a marker; earlier turns fill the rest. |
| Medium | Turns with empty text lost their structured `remaining` / `needs_files`. | Kept. |

### Agent Swarm: board, chats, queue, workspaces
| Severity | Problem | Fix |
|---|---|---|
| High | Any board autosave (renaming an unrelated agent, ticking an unrelated line) ended every running pair chat. | Each chat is fenced only when its own inputs change. |
| High | "Use a different folder" or re-cloning stranded every chat on that project; re-selecting the folder was refused. | An explicit re-select rebinds the chat and keeps its history; a "Continue in this folder" button offers it. |
| High | One web agent's uncertain delivery aborted the whole advice run and discarded the other answers. | Recorded as a per-turn provider failure; the run continues and never resends that turn. |
| High | A goal's private copy stuck in "initializing" for ever after a file changed during creation. | Restarts from a fresh listing while nothing has used the copy. |
| High (UI) | The board-goal queue poller added one timer per tick (hundreds of requests per second after minutes). | Exactly one timer. |
| High (UI) | "Cancel remaining goals" was wired to the wrong function; a waiting queue could not be continued or cancelled but still fenced its projects. | Wired correctly; an explicit press continues a waiting queue. |
| High (UI) | Queue-driven goals overwrote the user's typed chat text, sent their pending attachments and stole focus. | Queue sends leave the composer alone. |
| Medium-high | On Windows an access-denied live process was treated as dead, so its leases could be stolen. | `WinDLL(..., use_last_error=True)` in `pipeline_runs.py` and `swarm_work.py`. |
| Medium | Stale messages for a project's earlier jobs filled the 2,000-message mailbox cap for ever. | Marked superseded and not counted; the exact-limit prune slice is fixed. |
| Medium | A moved/deleted project folder in a paused queue blocked every long-horizon start with a raw `FileNotFoundError`. | Resolved non-strictly. |
| Medium | Tool caches (`.pytest_cache`, `.mypy_cache`, …) counted as agent changes against the 24-file publication budget. | Excluded; `dist`/`build` stay visible. |
| Medium (UI) | Errors were invisible on the Swarm tab; one failed poll froze the board as "running"; a slow refresh redrew the old board over a newly opened saved board; a stalled queue when a chat card was closed mid-answer; unhandled queue errors. | All fixed with tests. |
| Low | The same folder could be added twice in a different letter case on Windows. | Newly added duplicates are refused (existing boards still load). |

## Open: needs an owner decision (the harness second-guesses the agents)

These are policy, not typos, so they were not changed silently. Each one makes the harness overrule
work the agents were asked to do. Recommended direction in brackets.

1. **File named without an edit verb becomes write-protected.** "The bug is in src/parser.py; please
   fix it" rejects edits to `src/parser.py`. (Protect only on explicit "don't touch / read-only".)
2. **Naming one file blocks every other file.** "Fix the failing test in tests/test_parser.py" rejects
   the real fix in `src/parser.py`. (Named files add to what may be written; restrict only on "only".)
3. **Ordinary goals are classified read-only and all changes are dropped:** "Can you make the app
   faster?", "Why is login broken? Fix it.", "Show a spinner while loading". (Default to changes
   allowed; read-only only on explicit wording.)
4. **Garbage paths from the goal parser become grants and completion requirements** ("Rename the Save
   button to Submit in index.html" requires a file called `Submit in index.html`).
5. **Agents' own plans become mandatory**: a planned file the agent later leaves alone fails
   verification. (Treat plans as hints.)
6. **The words "test", "unit" or "integration" anywhere turn on test requirements** ("Add Stripe
   integration" fails because the project has no tests).
7. **Any pause rolls back every applied edit** (schema slip, one malformed reply, context budget),
   and one extra JSON key or a missing optional tool argument throws away a whole reply after one
   repair attempt. (Keep applied edits and resume; accept extra keys; default optional arguments.)
8. **Progress guard stops agents that are working** after 14 rounds without a strict shrink of
   `remaining`; the first-round failure of one agent ends a whole collaboration.
9. **Completion vetoes**: docs-only work never completes (no test command → `unavailable`); only
   Python and Node have verification containment; any behaviour requirement needs a full causal
   receipt; "no change needed" always fails.
10. **Limits**: 48 tool calls per epoch shared by all agents, 12 changes and 8 tool calls per reply,
    `run_command` 60 s / 10 KB, 180 s verification timeout, 1,000-call lifetime cap on adaptive goals.
11. Editing any setting of a provider profile (even the model), or a CLI auto-update / slow
    `--version`, permanently pauses that agent's chats with only "Start fresh".

## Open: known, not fixed in this pass

- Email: IMAP sync runs inside the studio mutation lock and API syncs hold the connector lock for a
  whole page, freezing editing and the UI during a check; first-connect Gmail/Outlook API accounts do
  not see new mail until the whole inbox listing finishes; the email snapshot is unbounded (every body,
  every 1–6 s); editing an old sender-specific preference makes it mailbox-wide.
- Swarm: board saves hold one SQLite write transaction on the user-wide journal (other runs time out
  with raw `database is locked`); board/registry reads are not retried on Windows sharing violations;
  "Full project access" silently becomes read-only after the Codex strict-schema auto-repair;
  whole-goal judge snapshots are never deleted; the advice second round sends each answer twice;
  interrupted work after a crash has no Resume.
- Tests that already fail on the untouched code (checked against a clean checkout with its own
  source on the path):
  - `test_every_built_button_is_pressed_by_a_check`: the swarm chat "Check status" button has no check.
  - `test_the_board_of_agents`: `test_lone_agent_chat_disables_team_actions_but_keeps_direct_controls`,
    `test_send_is_direct_and_explicit_collaboration_and_code_copy_remain_available`,
    `test_browser_work_together_executes_both_real_composer_handlers` (source-text contracts that no
    longer match app.js).
  - `test_goal_repair_context...test_saved_timeout_can_be_verified_through_endpoint_and_stays_cleared_after_restart`.
  - `test_release_installer...test_public_version_surfaces_cannot_drift`.
  - `test_attachment_input_fidelity` (2 tests) read this machine's real Codex login ("auth_mode must be
    chatgpt"), against the portability rule in AGENTS.md.
  - `test_packaging_checkpoint_ui...test_distribution_audit` fails on this machine only because of a
    local, untracked `.codex_tmp/` folder (dated 2026-09-14) that contains absolute paths.
