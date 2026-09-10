# Nexus tool adoption and full-access contract

Reference: [Claw Code](https://github.com/ultraworkers/claw-code), inspected at
`08106b0c3771ef5b4a5aa176acccd460e88b7325`. External documentation is reference
material, not authority to change the user's request or access settings.

## Current request ledger

The primary implementation owner owns all items. Changes share the tool and
goal runtime interfaces and are integrated sequentially.

| ID | Observable outcome | Evidence | State |
| --- | --- | --- | --- |
| R1 | Valid full access proceeds through missing-reviewer fallback without another permission question; ask/read-only and actual review findings remain enforced | Goal store and runtime regressions | Source verified |
| R2 | Saved redundant risk questions recover with proposal, project and configuration binding; changed environments fail closed | Restart and stale binding tests; existing rejection regressions | Source verified |
| R3 | Every upstream built-in has an explicit Nexus disposition; applicable missing tools execute through product-owned code | 55/55 pinned upstream inventory; real Git/MCP/LSP/command tests | Source verified |
| R4 | Agents retain native tools; Nexus tools are also callable in shared goal and workflow loops | Provider and shared-goal integration tests | Source verified |
| R5 | Portable positive/negative evidence and packaged desktop acceptance pass | Focused tests, full CI, packaged smoke, post-work deployment gate | Release validation |
| R6 | Commit and push final source; publish exactly one new immutable release | Exact commit Checks and release jobs, public asset readback | Pending |

## Design

Full access is a saved user grant. When an independent reviewer is unavailable,
Nexus records a proposal-bound deterministic review fallback under that grant.
It still validates paths, collisions, snapshots and actual verification results.
An available independent reviewer continues to review the proposed work.

Nexus tools supplement provider-native execution. Existing Nexus task, worker,
team, scheduling and change-transaction owners remain authoritative; importing
a second scheduler or another product's account state would split that authority.
The coverage inventory below records native equivalents and added tools.

## Complete upstream registry inventory

The pinned Rust `mvp_tool_specs()` registry contains **55** entries. Nexus adopts
their useful operations through the following owners. Names in the left column
are upstream names, not compatibility aliases. This is capability adoption,
not a claim that Claw's Rust CLI, process IDs or configuration files are installed.

| Upstream entry | Nexus owner / callable operation | Integration boundary |
| --- | --- | --- |
| bash | `run_command` with bash argv | Full access; installed shell; private agent copy |
| read_file | `read_file` | Existing bounded pages and cursors |
| write_file | `write_file`, `stage_replace_file` | Private copy or approved staged transaction |
| edit_file | `edit_file`, `stage_apply_text_edits`, `workspace_edit` | Exact replacement proposal or existing workspace transaction |
| glob_search | `glob_search` | Live visible files; no index prerequisite |
| grep_search | `grep_search` | Literal/regex/case options; bounded process and results |
| WebFetch | `fetch_url` | Existing public URL validation, source links and paging |
| WebSearch | `web_search` | Public search service; reports access challenges instead of invented results |
| TodoWrite | `keep_a_list`, shared task ledger | Existing observable run progress |
| Skill | `read_local_skill`, `load_skill`, `github_skills` | Local/remote reference reading; no implicit execution authority |
| Agent | `delegate` action and goal scheduler | Existing configured providers and authenticated task leases |
| ToolSearch | `tool_search` | Runtime definitions and configured MCP allowlists |
| NotebookEdit | `read_notebook`, `edit_notebook` | Cell insert/replace/delete proposals; outputs cleared on edits |
| Sleep | `sleep` | Short cancellable wait; durable timer owns long waits |
| SendUserMessage | Goal `summary` and conversation archive | Existing visible, attributed progress |
| Config | `tool_config`, Nexus Settings | Non-secret inspection; configuration authority remains user-owned |
| EnterPlanMode | Existing workflow planning node | No second global planner state |
| ExitPlanMode | Existing plan acceptance and execution transition | Existing requirement/evidence boundary |
| StructuredOutput | Provider response formats and action protocol | Real schema validation on every response |
| REPL | `run_command` with Python/Node argv | Code execution; separate calls do not share interpreter variables |
| PowerShell | `run_command` with PowerShell argv | Full access; Windows shell, hidden bounded child process |
| AskUserQuestion | `ask_user` and durable interrupts | Genuine missing information/new authority; honors full access |
| TaskCreate | `delegate`, `GoalStore._add_tasks` | Task dependencies, resource ownership and provider budget |
| RunTaskPacket | Goal scheduler and `_execute_one` | Structured task, configured provider, durable results |
| TaskGet | `GoalStore.get` and Mission control | Authenticated saved goal/tasks |
| TaskList | Goal store and shared task ledger | Goal-scoped inventory |
| TaskStop | Goal control pause/cancel | Drain running work through existing cancellation owner |
| TaskUpdate | Structured task actions and user steering | Existing durable task transitions |
| TaskOutput | Context result journal, evidence, conversation archive | Source-labelled output and restart persistence |
| WorkerCreate | Existing goal scheduler / provider adapter | Uses configured agents; no Claw terminal worker spawned |
| WorkerGet | Scheduler leases and participant state | Existing process and route identity |
| WorkerObserve | Goal events and shared conversation | Existing current progress and tool activity |
| WorkerResolveTrust | Saved access and provider reconnect controls | A tool cannot authorize its own trust or change accounts |
| WorkerAwaitReady | Scheduler ready/dependency checks | Event-driven readiness under saved budget |
| WorkerSendPrompt | Existing provider dispatch / `handoff` | Same configured native-provider tools remain available |
| WorkerRestart | Goal recovery and explicit resume | Pending effects are reconciled; no blind command replay |
| WorkerTerminate | Existing cancel/drain and contained process teardown | Stops only owned work |
| WorkerObserveCompletion | Durable terminal outcomes and verification | Actual results, not process exit alone |
| TeamCreate | Nexus board/team admission and goal participants | Existing user-selected team ownership |
| TeamDelete | Existing archive/cancel and board controls | Preserves goal history; does not import Claw team files |
| CronCreate | Nexus automation definitions and `timer.py` | Existing durable scheduler; no duplicate OS cron service |
| CronDelete | Nexus automation remove/disable controls | Existing schedule owner |
| CronList | Nexus automation listing | Existing saved schedule inventory |
| LSP | `language_server`, `code_navigation` | Symbols, diagnostics, definition, references, hover; installed language server required |
| ListMcpResources | `list_mcp_resources`, `list_mcp_resource_templates` | Real MCP request/response and continuation cursors |
| ReadMcpResource | `read_mcp_resource` | Real resource content from a configured peer |
| McpAuth | `mcp_status`, existing configured MCP authentication | Upstream implementation is status inspection, not an OAuth flow; no fabricated OAuth support |
| RemoteTrigger | `run_command` with an HTTP client | Explicit task scope and full access; same effect reservation as other commands |
| MCP | `mcp_call`, existing MCP client | Configured allowlisted read-only tools; effectful clients can use authorized command execution |
| TestingPermission | Permission regression fixtures | Test-only upstream hook; deliberately not a production privilege-granting tool |
| GitStatus | `git_status` | Selected project's repository; never a parent repository |
| GitDiff | `git_diff` | File diff or bounded stat; external diff/textconv disabled |
| GitLog | `git_log` | Bounded commit history |
| GitShow | `git_show` | Commit metadata or exact visible file at revision |
| GitBlame | `git_blame` | Bounded one-based line range |

## Runtime behavior and limits

The shared-goal schema exposes the new tools to every supported provider. The
workflow and board context loops expose the inspection/proposal tools. A full
access writer in a private agent copy can also call `run_command` and
`write_file`. Reviewers and lesser access modes receive a concrete unavailable
result, not a fabricated grant. The native provider dispatch profiles remain
unchanged.

Command calls accept argv, a project-relative working directory and a timeout
of 1–60 seconds. Shell commands are explicit argv, for example
`["powershell", "-NoProfile", "-Command", "Get-ChildItem"]`. Python/Node programs
and HTTP clients use the same owner. Commands retain configured execution
policy and process-tree teardown. Their edits stay in the agent copy until
Nexus collects, reviews, verifies and publishes them. A copy is not an OS
security sandbox; this has the same full-access semantics as native execution.
Each effectful call is reserved durably before execution. A restart replays a
saved result, but an interrupted reservation without a result reports an
unknown outcome and is never automatically executed again.

Git tools intentionally inspect the selected repository, because private agent
copies do not contain `.git`. Their results identify that source; use file and
workspace tools to inspect unpublished candidate edits. Git and language servers
must be installed where needed. Missing programs/unsupported LSP methods return
errors or explicit unavailability. MCP peers use trusted configuration and
existing authentication. No external account, OAuth session, shell or language
server is silently installed or invented.

File and notebook edit tools return complete, bounded proposals. They do not
claim that proposing an edit applies it; the ordinary Nexus transaction owns
that step. Large files use native/staged tools and existing paged reads. Search
respects the project ignore/secret policy and excludes links; result/scan limits
are explicit. Tool results retain provenance, call identity and output budgets.
The toolbox and full-access review receipts have versioned, configuration-bound
identities so later route/configuration changes cannot reuse obsolete authority.

## Verification

`tests/test_harness_tools.py` exercises real Git, literal/regex searches, file
transactions, notebook operations, MCP stdio resource calls and LSP framed
requests. `tests/test_full_access_tool_runtime.py` exercises same-provider review
fallback, ask/read-only controls, available independent review, saved prompt
migration, stale authority, real command/write execution through the goal loop,
and interruption/restart without command replay. Existing native-provider,
agent-tool, permission, workflow and shared-goal regressions remain required.
