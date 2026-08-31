# Source Binding Audit

Audit date: 2026-08-14.

The source harness was inspected read-only. The new package was written in a separate destination. No source file was copied into the package.

## Project-specific bindings found

| Binding | Source examples | Portable replacement |
|---|---|---|
| Domain policy | Product-specific formats, runtime placement, and build rules | Explicit configuration and enabled plugin rules; absent from the core |
| Fixed memory location | Named Obsidian vault, fixed current-state and session notes | Configured SQLite database and standards-file list |
| Fixed generated indexes | `.vault-index`, `.codegraph`, `graphify-out` | One index port with SQLite FTS, dependency edges, and plugin adapters |
| Fixed build tree | Win32 release folder, named DLL and converter | Manifest detectors and configured build/test argv arrays |
| Fixed CI | Jenkins files, local wrapper names, project-specific gates | Detector and plugin checks registered by data or entry point |
| Fixed Git policy | Named remote, `ongoing-work`, `master`, standing draft pull request | Configured protected branches, optional prefix, explicit commit/push grants |
| Fixed docs gate | Named Three.js SDK guide and watched folders | Plugin doctor checks and review packets |
| Fixed live deployment | Named environment variable, Ruby backup folders, deploy batch | No deployment in the default profile; plugins must declare mutation scope |
| Fixed model settings | `MLB_OLLAMA_URL`, named local embedding/chat models | `provider.*`, `memory.embedding_*`, and `HARNESS_*` overrides |
| Windows-only wrappers | CMD and PowerShell entry points, Visual Studio lookup | Python zipapp, PowerShell installer, POSIX installer, argv process adapter |
| Repository terms in prompts | Game/editor-specific question prompt | Generic immutable policy plus local standards and task contract |
| Fixed memory budgets | Named files with fixed kilobyte ceilings | Context section budgets and database retention settings |

## Reusable capabilities extracted as contracts

- health checks and ranked audit findings;
- project orientation and standards loading;
- indexed long-term memory;
- architecture and dependency lookup;
- bounded local model access;
- subprocess checks and code review adapters;
- exact file checkpoints and rollback;
- transaction-safe refinement with baseline conflict checks;
- independent packet-bound review;
- portability scans;
- incremental index refresh;
- source, test, linter, and build detection.

## Decoupling rule

The `our_harness` core must not contain source-project names, user paths, drive letters, fixed branches, fixed remotes, named build outputs, or fixed documentation paths. `harness audit` enforces this over executable source, templates, examples, and installers. This audit note is excluded because it records the removed bindings.

Project policies belong in entry-point plugins or explicitly enabled project-relative plugin files. Loading a plugin is an execution decision; a discovered file is never loaded automatically.
