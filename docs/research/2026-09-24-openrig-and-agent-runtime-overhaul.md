# OpenRig evaluation and the case for an agent-runtime overhaul (2026-09-24)

Question from the owner: would adopting OpenRig (https://github.com/mvschwarz/openrig) fix the AI
Swarm's problems, and should it be how Nexus agents talk to each other? Approached as "would an
overhaul solve most of our issues", not as a minimal fix.

Five independent research passes fed this document: an OpenRig code dossier (clone at c8fca9d,
v0.5.14), a hands-on install and run attempt on this Windows 10 machine, a 2026 survey of how other
systems run and connect CLI coding agents, an inventory of Nexus's own swarm code and bug history,
and a map of Nexus's live-activity pipeline. Measurements from the same day are in
`docs/audits/agentic-and-mail-tabs-2026-09-23.md`.

## Verdict

- **Do not adopt OpenRig as the engine, and do not run it as a backend.** It cannot run on native
  Windows (tested: its daemon refuses to start without tmux, and its commands are POSIX shell
  strings that cmd.exe rejects). Its agents talk by pasting text into each other's terminal and
  guessing readiness with regular expressions over the screen; its own open issues include "sent but
  still a draft". It supports only Claude Code and Codex, writes global `~/.claude*` and
  `~/.codex/config.toml`, boots its own agent team on daemon start, has no sandbox or file-change
  transaction boundary, and is effectively one maintainer with no CI. License (Apache-2.0) is fine.
- **Borrow five of its ideas** (below).
- **Yes, overhaul the agent runtime.** Our recurring problems come from the transport, not from the
  orchestration library: every agent turn is a brand-new CLI process, every Nexus tool request is a
  whole extra turn, every reply is forced through a 28 KB JSON schema, and Work Together runs one
  agent at a time. The 2026 state of the art, which all four of our CLIs now support, is one
  long-lived streaming session per agent. That change addresses most of the issue classes below at
  their root.

## What OpenRig is (short)

TypeScript (Node 20/22/24), about 213k lines: a local HTTP daemon on SQLite, a `rig` CLI, a TUI, a
web UI in maintenance mode and an MCP server. A team ("rig") is YAML: pods of seats with stable
addresses (`dev-owner@first-project`). Each seat is the vendor's interactive TUI inside tmux
(`claude --session-id … --name …`, `codex -s workspace-write`, resume and fork supported).
Messages: `rig send` → `tmux load-buffer` + bracketed paste + 200 ms + Enter, in an email-style
envelope. Durable coordination: a SQLite queue/inbox/outbox with a "hot-potato" rule (a task can only
close as done with a reason: handed off to, blocked on, denied, cancelled, no follow-on, escalation).
Observability is the raw terminal (tmux panes, xterm over WebSocket) plus vendor hooks for idle/busy;
no structured tool-call or thinking stream. No shared-file conflict control; seats share one folder.

## Why Nexus is slow, stuck and opaque (evidence)

| Issue class | Root cause | Evidence |
|---|---|---|
| Minutes per answer | New process per turn, full prompt rebuilt; every Nexus tool request is another full turn | Goal 1f6c158c: 253 s, then a 600 s stall; same call replayed 10.7-14.8 s |
| Team waits | Work Together claims one task per pass (`long_horizon.py` `claim_ready`) | Claude2 idle ~14 min |
| Nothing visible | Activity only for durable goals; chats had no sink; rows collapsed | Pipeline map, fixed partially today |
| Schema slips | 43-variant `anyOf`, 28 KB, forced on every reply | Malformed `extract_archive` NUL path |
| Freezes on identity | Chats bind to route fingerprints because the session is rebuilt each turn | Route identity v1→v5, reconnect flows |
| Fragile durability | Many partial authorities: 5 message stores, admission journals across renderer, Electron and Python | 51 schema versions, ~67 contracts; `NEXUS_CONVERSATION_RUNTIME_V2.md` names this |

About 50k lines of swarm code and ~20 overlapping concepts (pair chats, relay, collaborate, legacy
work together, long-horizon goals in two collaboration contracts and two execution modes, board
goals, mailbox, ledger, dialogue archive, peer delivery, closeout judge, web chats, the older team
graph, mission control).

## Recommended architecture: Nexus Agent Runtime v3

```
Electron UI  <-- one event stream -->  Python core: session manager + broker
                                         ClaudeSession  : claude -p --input-format stream-json
                                                          --output-format stream-json --verbose
                                                          --include-partial-messages (+ --resume)
                                         CodexSession   : codex app-server (JSON-RPC over stdio)
                                         GeminiSession  : gemini --acp
                                         CopilotSession : copilot --acp / Copilot SDK
                                         WebRelaySession: existing relays, same events
                                         Nexus MCP server: team messages, tasks, leases,
                                                          decisions, verification, report_result
```

1. **One persistent session per agent.** Start once, send each turn into the running session
   (Claude stream-json input; Codex `thread/start` then `turn/start`, `turn/steer` to add input mid-
   turn, `turn/interrupt` to stop). Resume by session/thread ID after a restart. Removes process
   start, config reload and prompt rebuild per turn, and uses provider prompt caching properly.
2. **No forced output schema.** Agents stream free text and use their native tools. The structure
   Nexus needs comes through Nexus-hosted MCP tools (`report_result`, `send_message`, `claim_task`,
   `request_review`, `ask_user`) whose arguments are validated there. A schema only for an optional
   final summary turn.
3. **One internal event model shaped like ACP `session/update`**: message chunks, thought chunks,
   tool calls with updates, diffs, plans, usage, permission requests. Codex app-server and Claude
   stream-json map onto it directly; Gemini and Copilot speak ACP natively. The UI renders one
   schema, live, for every agent; new ACP agents (Cursor, Goose, OpenCode, Qwen...) come for free.
4. **Agent-to-agent messages through a Nexus-hosted MCP mailbox with push delivery.** Durable in
   SQLite (one store replacing the five). When a message arrives: if the recipient is mid-turn,
   Codex gets `turn/steer`, Claude gets a queued stream-json user message; if idle, start its turn.
   Peer messages are labelled as from agent X, never grant permissions, and are rate-limited and
   deduplicated.
5. **Parallel work, isolated per agent.** A git worktree (or Nexus agent copy) per agent plus
   advisory path leases from the mailbox; merge through review. Ordering only where the task graph
   has a real dependency.
6. **Keep what is genuinely good**: file-change transactions and hash-bound rollback, path
   confinement, Windows AppContainer verification, command grants and user denials enforced as CLI
   rules, access modes, "never auto-resend an unknown outcome", authenticated goal journal,
   delivery receipts, explicit-only test requirements, independent closeout judge, subscription CLI
   integration and the session-health monitor, credential redaction, and the email assistant (it
   only shares provider routes and session health).

### Ideas taken from OpenRig

1. Closure reasons on tasks plus an append-only transition log (hot-potato contract).
2. Checking that a resume really resumed (resumed / fresh / failed), shown to the user.
3. Stable seat identity: the address stays while the conversation occupying it changes.
4. Stuck-work sweeps that report rather than act.
5. A read-only team-structure view.

Not taken: paste-into-terminal delivery and screen-scraped readiness.

### Windows notes

Spawn agents with plain pipes, never a PTY (Gemini ACP hangs under a TTY; ConPTY has EPIPE bugs).
Codex's native Windows sandbox is experimental and Windows 10 is best-effort: detect and surface it.
Generate Codex app-server schemas from the installed version and feature-detect Claude via its
`system/init` capabilities; show "unsupported CLI version" instead of failing silently.

### Risks

- `codex app-server` is labelled experimental by OpenAI even though every Codex surface uses it;
  pin and feature-detect.
- Anthropic's subscription policy for programmatic use changed twice in 2026; drive only the user's
  own installed and signed-in `claude`, never ship or broker credentials, never use `--bare`.
- Migration of durable goal state: run v3 beside the existing engines for new chats first; old
  goals finish on the old engine.

## Phased plan

| Phase | Deliverable | Removes |
|---|---|---|
| 0 (done 2026-09-24) | Stall watchdog, native-tools prompt, live chat activity feed, Claude thinking and block-collision fix, Codex reasoning summaries while watched | Worst waits, blind waiting |
| 1 (done 2026-09-24) | Unified event model, persistent Codex app-server and Claude stream-json sessions, in a separate **Live team** tab rather than behind a setting | Process per turn, no deltas |
| 2 (done 2026-09-24) | Nexus MCP server (messages, tasks, questions, reserved paths, report_result, approvals); no forced output schema | Tool round-trips, schema slips |
| 3 (done 2026-09-24) | Push mailbox, parallel agents, per-agent git worktrees, path leases, closure reasons with a transition log, resume verification, stuck-task sweep | Serial turns |
| 4 (partly done 2026-09-24) | ACP client for Gemini and Copilot is built. Retiring relay, collaborate and legacy paths and migrating goals is the owner's decision, once Live team has proven itself | Concept sprawl, not yet |

What was built and how to use it: [docs/LIVE_TEAM.md](../LIVE_TEAM.md). Tested by clicking
through the tab against real Codex and Claude sessions: a two-agent game build, messaging one
agent, close and reopen with both conversations remembered, Allow and Deny cards, worktree
branches with a commit from each agent, and Interrupt. Gemini stopped at its Google Cloud
project requirement, with a clear message. Copilot was only exercised against a stand-in.

## Sources

OpenRig: repository README, `packages/daemon/src/domain/session-transport.ts`,
`adapters/claude-code-adapter.ts`, `adapters/codex-runtime-adapter.ts`, `lib/pane-envelope.ts`,
`domain/hot-potato-enforcer.ts`, `docs/reference/edge-types.md`, issues #9, #12, #14, #17, #25, #28.
Claude Code: headless and streaming-input docs, agent teams, cross-session messaging, Agent SDK.
Codex: app-server README and docs, non-interactive mode, MCP server removal, Windows sandbox.
Gemini CLI headless and ACP mode; Copilot CLI ACP server and Copilot SDK (GA 2026-06-02); Agent
Client Protocol overview, agents list and updates (SDK 1.0 2026-06-25); A2A v1.0; mcp_agent_mail;
claude-squad, uzi, Emdash, Nimbalyst.
