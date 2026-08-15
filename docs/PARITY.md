# Capability Parity

Baseline review:

- source harness: read-only local inspection on 2026-08-14;
- Prime Agent: local clone at commit `9bf49d897c22563f3e4483d28149c1aac452a6f9`;
- new package: this repository.

No Prime Agent source was copied. Its behavior informed independent interfaces and tests. Legal review requested for any later code reuse or distribution decision.

| Capability | Source harness | Prime Agent baseline | This package |
|---|---|---|---|
| Health checks | doctor and ranked audit | installer/runtime checks | `doctor`, `audit`, setting provenance |
| Session orientation | session brief and memory read order | session resume and context view | `brief`, standards order, recent runs |
| Long-term memory | vault, lexical/semantic index | continual prompt/memory/skill/subagent state | SQLite events, episodes, FTS, vectors, reviewed supplemental state |
| Workspace index | CodeGraph and Graphify | session/tool context | incremental documents and dependency edges; parser plugins optional |
| Cited Q&A | local retrieval then model answer | persistent RLM inspection | `ask` with indexed source labels |
| Safety snapshot | Git checkpoint tags | versions and rollback | portable ZIP checkpoints plus exact file-transaction backups |
| Local checks | Cppcheck, Semgrep, project tests | model tools and quality gates | detected/configured test, lint, build commands plus plugin checks |
| File apply | project-specific tools | plan/apply, disk reread, atomic save | planner-approved paths, baseline hash, control-root/link checks, atomic replace, POSIX mode preservation, batch rollback |
| Concurrent change guard | staged-scope review packet | baseline state conflicts | compare-and-swap file and prompt baselines |
| Review | local wrapper and independent semantic gate | automatic review and refinement review | canonical cumulative patch and hash, isolated reviewer prompt, current-file recheck after verdict |
| Self-improvement | audited evidence transaction | typed `/refine`, history, rollback | repeated-failure candidates, review-gated versions, rollback |
| Context bounds | memory budgets and targeted retrieval | compaction and bounded overview | fixed section budgets, typed recent-state compaction, evidence manifest |
| Provider cache support | local model reuse | compact harness prompt | byte-stable prefix hash and measured cacheable ratio |
| MCP | project bridge and tools | MCP manager | stdio and HTTP JSON-RPC client, tool allowlist, size/time limits |
| Agent routing | independent reviewers and optional branches | recursive subagents | planner, coder, reviewer, repair state flow; configurable plugin workflow policies |
| Visual graph | generated architecture diagrams | TUI agent views | local editor with state-only simulation plus explicit production submission; submitted tool roles and loop bounds drive real checks and repair limits |
| Cross-platform install | Windows project bundle | shell release installer | Python package, zipapp, PowerShell, POSIX shell |
| Fixed project policy | mandatory | Prime product defaults | absent from core; explicit profile or plugin |

## Intentional differences

- This package does not run an always-on daemon. Versioned run checkpoints persist in SQLite. The CLI can list, inspect, resume, cancel, approve, or reject a retained run after graph, config, deadline, transaction, and applied-file checks.
- It does not claim that the process runner is a security sandbox.
- It does not activate model-written prompt changes automatically. Candidates need fresh verification and review.
- It does not publish, merge, deploy, or create pull requests by default.
- It does not bundle large parsers or embedding runtimes. Plugins can add them without changing the base install.

## Release blockers

The test suite must cover configuration precedence, relocation, path/link escape, transaction rollback, stale baselines, process timeout, event storage, FTS retrieval, optional vectors, stable prompt hash, context budgets, refinement conflict, fragmented streams, graph cycle limits, Gauntlet repair, CLI install, and UI keyboard/ARIA checks.
