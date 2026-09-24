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

## Fixed on 2026-09-23 (first pass)

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

## 2026-09-24: "Agents lead" policy (approved by the owner) and rounds 2-5

The owner approved flipping the defaults: agents may read, write, create, delete and run in the
projects they were given unless the user explicitly says "read-only", "don't touch X" or "only X", or
sets a restriction in the UI. The rule now opens `AGENTS.md` ("Agents lead; Nexus only supports").
Each change below was then reviewed again by an independent session with reproducers, over several
rounds, until no high or medium finding remained.

### What no longer gets in the agents' way
- **Wording is never a restriction.** Mentioned files are not protected; named files add to what may
  be written; questions, "show/list/check", "without breaking X" and bug reports phrased as
  prohibitions no longer make a run read-only. Plans, file names and wording are hints (`mandatory`
  flag, `advisory_unmet`), never requirements.
- **Completion.** Tests are required only when the user asks for them (one shared classifier,
  `goal_verification.explicit_test_requests`). "No change needed" is a valid result. When no check can
  run, agent agreement completes the goal as `agent_verified`; the UI says "Agents agreed it is done; no
  automatic check was available" and never claims a machine check. A check that ran and failed still
  blocks. Closeout judges are matched tolerantly, but only a recognised approve verdict approves.
- **Applied work is kept.** Pauses, incomplete runs, schema slips, tool budgets and single failed
  transactions never roll back other applied edits. Only an explicit user undo does.
- **Lenient with agents.** Extra keys, omitted optional arguments and prose around JSON are accepted on
  the received side (the sent schema stays strict-compatible); one bad change entry is refused on its
  own; one agent's format slip or outage never stops the team.
- **Generous machine limits.** `run_command` up to 60 minutes and 200 KB shown output; `write_file` 10M
  characters; 500 files per proposal; 200 changes and 64 tool calls per reply; 24 participants (anyone
  left out is named). Lease and provider-slot waits queue in order instead of failing.
- **Settings changes don't freeze chats or goals.** A model, effort, flag or CLI version change continues
  (route-identity v5 allow-list); only a real identity change (another program, bridge, host, account,
  endpoint or profile) pauses with a reviewed reconnect.
- **Default access.** New goals default to Full (`chosen_by: default`). Goals created before this
  default keep Ask, with a one-time note offering Full: no silent privilege increase.
- **Loops still stop.** No round cap was added. A run stops (keeping all work, resumable) only on a
  genuine loop: 200 rounds with no new outcome, or project state returning to one it already left
  twice; 200 identical tool results pause a goal.

### Protections that still hold (and were tightened)
- No writes outside the selected projects; `.git`/`.harness` in any spelling; junctions/links pointing
  outside are refused or skipped; hard links are separated before rewriting.
- Explicit user restrictions: absolute, relative, glob, list, dash, label and header forms of "don't
  touch", every "only" wording (an unusable "only" stops and asks instead of widening), explicit
  read-only, UI write roots.
- A denied command is enforced by Nexus's own tools (normalised argv) and passed to Claude Code as exact
  `--disallowedTools` rules; for CLIs that can't enforce it the UI says it is advisory.
- A reply is never delivered twice; an uncertain web turn is never resent.
- Git history in agent working copies is **off by default** (`NEXUS_AGENT_GIT_HISTORY=1` to opt in).
  When on: isolated clone with no remote, all `GIT_*` stripped, hooks/fsmonitor/filters neutralised,
  accepted baseline committed, ignored files like `.env` never deleted from the real project.
  By design, an opted-in agent can see all history (including secrets that were committed then deleted)
  and could add its own remote and push.

### Email assistant, rounds 2-5
- Connecting any mailbox treats the existing inbox as history (browser and classic Outlook via a first
  scan baseline plus the browser's `first_seen_at`; IMAP newest-first with INTERNALDATE and UID-based
  "new"); mail that arrived while disconnected or while checks were off is not announced.
- One message never blocks a mailbox (charset fallback, per-message failure records the user can
  dismiss). UIDVALIDITY changes are resumable and deduplicated.
- Senders: one quoted form; `ceo@corp <attacker@evil>` style headers are refused; per-sender learning
  works for "Lastname, Firstname".
- HTML: a browser-like reader checked against Chromium on a 190-case corpus
  (`tests/fixtures/email_html_corpus.json`); text is hidden only on literal, unambiguous signals;
  hidden text reaches the AI only as a labelled, untrusted block and never the page; parsing is linear
  with a 2 MB cap and a 2 s budget. Class-based `<style>` hiding is a documented residual.
- Notifications: deduplicated across reloads and tabs, settings applied to notices already in the feed,
  a crashed or never-loaded pop-up is replaced, clicks are tied to the project that raised them.
- Drafting UI: the text shown is exactly the text sent; unsaved edits are never overwritten.

## Open: known, not fixed

- A protection that names no path ("don't touch the CI workflow") protects nothing; name the folder
  (`.github/workflows`).
- Email: IMAP sync still runs inside the studio mutation lock and API syncs hold the connector lock for
  a page; first-connect Gmail/Outlook API accounts list the whole inbox before new mail; the email
  snapshot is unbounded.
- Swarm: board saves hold one SQLite write transaction on the user-wide journal; board/registry reads
  are not retried on Windows sharing violations; whole-goal judge snapshots are never deleted.
- Mission control offers only "Cancel old goal and prepare a new one" for a changed provider setup; the
  Reconnect button is in the chat team panel.
