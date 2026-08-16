# Architecture

## Package map

```text
src/our_harness/
  cli.py                 command parsing and exit codes
  config.py              config layering, validation, provenance
  detect.py              stack and command detection
  safety.py              path confinement, link checks, redaction
  changes.py             baseline-bound file transactions and rollback
  execution.py           bounded process and optional Docker runner
  gitops.py              branch, exact-stage, commit, and push guards
  memory.py              SQLite event, episode, index, prompt, review storage
  indexer.py             incremental text and dependency indexing
  context.py             stable prefix and bounded context compiler
  providers/             OpenAI, Anthropic, Ollama, compatible, local process
  refinement.py          reviewed supplemental state and candidates
  mcp.py                 stdio and HTTP JSON-RPC client
  graphs.py              graph validation, conditions, cycles, simulation
  agent_tools.py         bounded read-only planner/coder discovery loop
  benchmark.py           deterministic conformance and optional agentic repair evaluator
  workflow.py            planner, coder, reviewer, repair application service
  review_panel.py        isolated concurrent reviewer aggregation boundary
  server.py              loopback HTTP API
  ui/                    accessible graph editor
  templates/             versioned built-in workflows
```

## Dependency direction

CLI and UI call `HarnessApplication`. The application owns ports for provider, memory, execution, changes, and indexing. Provider and project details do not enter the graph reducer.

```text
CLI / local UI
      |
HarnessApplication
      |
state graph + context compiler
      |
provider | memory | files | process | Git | MCP | plugins
```

## Durable state

SQLite uses WAL mode and schema migrations. Run events carry a sequence, run ID, state node, event kind, causation field, canonical payload hash, and timestamp. Current state is a projection of ordered events.

Large project files are not copied into the database without limits. The index stores text files up to `project.max_file_bytes`. Runtime backups live under `.harness/backups/<transaction-id>`.

## Memory blueprint

```text
.harness/
  config.json
  config.local.json       optional; authority requires an out-of-project user trust record
  memory/harness.db       events, chunk FTS, symbols, vectors, dependency edges
  backups/<id>/           exact pre-change files and manifest
  runs/                   reserved for exported run bundles
  cache/                  rebuildable provider/index data
```

Retrieval combines full-corpus FTS ranking, direct term matching, trust, exact symbol matches, and optional cosine scoring. Files are split deterministically into non-overlapping chunks of at most 6,000 characters; repeated content within one file is stored once, and identical evidence across files is returned once. Python indexing uses the standard-library AST to record imports, classes, functions, methods, and qualified names. Other supported text languages retain regex import and symbol extraction. Discovery applies nested `.gitignore` and `.ignore` rules, then a non-overridable secret-file filter. Text is credential-redacted before chunk storage or embedding dispatch. Workspace indexing stores per-chunk vectors from `memory.embedding_provider` and `memory.embedding_model`; query vectors use the same provider when it differs from the completion provider. Vector scoring streams every embedded chunk through a bounded top-candidate set, so path order cannot hide older evidence. During a workflow, both indexing batches and semantic query calls receive the fractional time remaining on the single workflow deadline, and indexing checks that deadline between files, batches, and database writes. Top symbol/document matches expand through bidirectional dependency edges, then symbol locations and relation evidence are ranked together before formatting. Results are deduplicated by content hash and constrained by section budgets. Current disk evidence always wins.

With `memory.enabled=false`, `MemoryStore` uses an in-process database only for the active workflow's ordered state. It skips source indexing and all episode/document retrieval and does not retain runs, review packets, prompt versions, or candidates after process exit. No configured memory database is created.

## Prompt cache layout

```text
[STATIC PREFIX]
base policy
provider execution boundary (responses are data; no model-callable tools)
output grammar
[END STATIC PREFIX]
[DYNAMIC SUFFIX]
task contract
detected stack
project standards
reviewed supplemental state
recalled episodes
workspace evidence
recent run state
```

The static prefix uses canonical JSON and stable ordering. Its SHA-256 is recorded in the context manifest. Compaction only changes the suffix. `fit_request_context` then bounds the complete prefix, suffix, and task prompt against the configured request ceiling and reserved output space.

## File transaction

1. Acquire the re-entrant cross-process project transaction lock, then normalize and confine every project-relative path. Portable component validation rejects trailing spaces or dots, colons and alternate data streams, DOS device basenames, and normalized `.git` or `.harness` components at any depth.
2. Reject links, junctions, duplicates, non-files, and scope limits.
3. Reread every target.
4. Compare the exact baseline SHA-256.
5. Revalidate file identity, metadata, and SHA-256 immediately before recording prior bytes in a transaction backup.
6. Bind each backup's SHA-256 and byte count into the prepared manifest and verify the stored backup.
7. Revalidate every target immediately before replacement, write a temporary file in the owning directory, flush it, replace atomically, and preserve POSIX mode bits.
8. Preallocate the transaction ID and checkpoint its candidate before backup creation. Checkpoint the verified prepared manifest before the first replacement.
9. Record before and after hashes plus a canonical patch and its SHA-256.
10. Mark the manifest applied only after every mutation succeeds. Resume binds a fully applied interrupted intent or applies a prepared, not-yet-mutated intent without another coder call.
11. Before internal or later rollback writes, validate the complete restore set: every file must match its exact before or after hash and mode, and all required backups must match their manifest-bound hashes and sizes.
12. Persist a hash-bound `rolling_back` intent before the first restore. After each atomic restore, verify the before boundary and persist progress. Retry skips exact before-state records, restores exact after-state records, and rejects any third state.
13. Verify the cumulative frozen change set around success persistence and immediately before returning completion.

## Self-healing graph

Planner and coder nodes may iterate through a read-only discovery loop before returning their strict final object. The loop offers root-confined tree and file reads, indexed workspace and memory search, dependency lookup, and an MCP bridge only for explicitly allowlisted server tools. MCP discovery calls require `annotations.readOnlyHint` to be exactly `true` and reject `destructiveHint: true`; `idempotentHint` alone grants no authority. Unclassified and mutating calls are refused. It has no command or write operation. Calls and bounded results have typed envelopes, span IDs, hashes, provenance, untrusted-data labels, a shared workflow deadline, and call/per-result/run byte limits. A SQLite journal binds each completed result to run, node, call ID, tool, and argument hash so resume reuses it instead of repeating the operation. OpenAI Responses calls continue with typed function-call outputs and retained response state; the text action envelope is the common fallback.

### Persistent programmatic workspace V1

`PersistentProgrammaticWorkspace` is a library boundary for coding loops that need to survive a worker restart. A caller supplies planner-approved mutable files, separate read-only support files, and trusted `VerificationAction` definitions. The controller accepts only typed inspect, replace, patch, delete, verify, and finalize actions. It never accepts a shell string or arbitrary Python program.

Each accepted action first writes an atomic intent checkpoint under `.harness/checkpoints/programmatic/<session>.json`, then records completion in its hash-chained journal. The checkpoint uses an HMAC key stored outside the project in the user's configuration directory. It binds the canonical project path and filesystem identity, complete effective harness configuration, approved paths, support paths, verification argv, generated-output policy, action IDs, and changed UTF-8 file snapshots. Copying an authenticated checkpoint to another project cannot restore its candidate. Source baselines include missing/regular type, content hash, byte count, mode, filesystem identity, and modification time. Restore creates a new temporary stage and rechecks every bound field. An intent without a completion record is `uncertain` and is never replayed.

Persistent verification requires Docker mode. The container receives only the temporary stage mount, not the source project. A bounded content-and-metadata manifest of the full source project is also compared under the project transaction lock before a verification result is accepted. Verification results do not survive restart; every required check must pass again against the reconstructed revision. A verification action that changes an approved or support file, creates an undeclared output, creates a link, or coincides with a source-project change taints the checkpoint and blocks restore.

Finalization returns a `StagedCandidate`. It does not write the project. The existing `FileTransaction` remains the only commit boundary. A deterministic HMAC-derived file in the private operating-system temp registry holds a bounded signed record for the current project, session, random nonce, creation time, and exact stage path. Construction reserves an empty private stage root and acquires this authenticated OS lease before `StagedCodingWorkspace` can copy any approved or support file. The next attempt can therefore recover a crash in either initial creation or restore, including the pre-copy and pre-checkpoint windows. Recovery validates the record, confines the stage to the direct temp root, acquires its released OS lock, and removes it. A live lock blocks cleanup. Malformed records, links, and other project/session registry entries are not touched. The replacement stage and lease are persisted before restore returns. `close()` removes the temporary stage and registry record while keeping the checkpoint; `discard()` removes both stage state and checkpoint. This is a restartable typed staging controller, not a persistent REPL or an unrestricted agent kernel.

```text
discover -> plan -> code -> apply -> verify -> review -> complete
                         ^        |         |
                         +--heal--+---------+
```

The graph stops on the first matching limit: loop attempts, loop timeout, the single workflow deadline, repeated failure signature, changed-file count, changed bytes, process timeout, or output bytes. The same remaining deadline is checked around local operations and passed into provider and command timeouts. Exhaustion restores transactions by default. `workflow.name` selects a validated built-in graph for CLI runs. The editor has two explicit paths: simulation is state-only, while Start run submits the current graph to the production interpreter. The interpreter starts at the explicit start entry, considers outgoing edges in JSON order, evaluates the restricted typed condition grammar, copies declared variables into edge input, and executes each visited role. A state with no matching edge fails instead of silently falling back. The canonical graph hash is frozen in run state.

Planner output carries a requirement ledger inside `acceptance_criteria`. Each row has a stable ID, an exact task quote, a category, and a distinct counterexample. The coder puts one code-path witness and counterexample result per ID in its self-review findings. Local validation rejects missing, duplicate, reordered, empty, or out-of-scope witnesses before a file transaction starts. The frozen review packet includes both collections, and the reviewer must test each counterexample independently. This adds no provider round. Old persisted string-only acceptance criteria are migrated to an internal version-one ledger during resume; new structured responses use the version-two contract.

The interpreter saves compare-and-swap checkpoints before and after graph nodes. A checkpoint binds the frozen graph and current node to typed workflow state, loop counters, step and tool budgets, retained transaction evidence, the remaining deadline, and any approval decision. A coder transaction also gets two write-ahead boundaries: ID plus candidate, then prepared backup manifest. Resume claims the checkpoint version while holding the project transaction lock, validates the current configuration and applied-file hashes, reconciles the pending transaction, and advances past an already completed node instead of executing it again. An `approval_required` node persists a pause before later work; approve and reject commands bind an explicit JSON-object decision. Terminal state is recorded before checkpoint deletion, and later resume calls return that recorded result without repeating effects.

Prepared transaction manifests are durable recovery points. `recovery list` classifies current hashes as not applied, fully applied after interruption, rollback in progress, ambiguous, or invalid. Rollback and finalize record terminal manifest states; finalize is allowed only when every after-hash matches. An interrupted rollback remains retryable when every record still matches one of its two bound states. A record matching neither boundary requires manual inspection.

Reviewer calls use a dedicated immutable review policy and an empty dynamic-context field. The exact frozen packet is the only per-run reviewer input; author memory, repository context, recent events, and coder reasoning remain outside that request. Packet IDs, panel hashes, stored packet text, and checkpoint hashes use the same stable Unicode-preserving canonical JSON serializer.

`ReviewPanel` controls production evaluation when `workflow.reviewers` is greater than one. It canonicalizes and hashes the packet before starting work, creates a distinct provider instance per reviewer, supplies only immutable lens policy plus that exact packet, and applies one caller-provided absolute deadline across the panel. Parallel work is bounded by `workflow.review_parallelism`. Each reviewer runs in a dedicated child process so explicit cancellation or deadline expiry can terminate and reap a non-cooperative provider and its process tree before the panel returns; unfinished members become blocking cancellation results. The worker starts with Python safe-path, no-user-site, and no-site flags. Its `PYTHONPATH` contains only the source, installed-package, or zipapp container that owns the already-loaded `our_harness` package; the project directory, empty path, user site, `sitecustomize.py`, and a project shadow package cannot participate in worker imports. Python environment controls are cleared and replaced with safe-path/no-user-site settings. On POSIX, local-provider commands share the reviewer's session/process group, and the parent kills that group after collecting the atomic result; on Windows, tree termination retains the job/task-tree behavior. Temporary packet/result files are removed after every panel run. Provider failures and malformed strict verdicts remain isolated to their reviewer. Aggregation is reviewer-order independent: blocker evidence sorts before advisory evidence, duplicate findings and residual risks are unioned deterministically, and PASS requires every configured reviewer to return a valid PASS. One configured reviewer uses the existing isolated exact-packet evaluator. The optional programmatic `provider_factory` is a trusted testing/embedding hook: its returned object must deserialize using the owning package root alone. Project-defined factory classes fail closed rather than widening worker imports.

## Self-improvement

Failures and successes become episodic records. A repeated failure signature asks the provider for one narrow supplemental prompt candidate. The candidate is staged, not activated. It can be reviewed with named verification records that include concrete evidence, explicitly rejected, or promoted only after a passing review with a reason. The review hash binds the candidate body, baseline, evidence, verdict, and verification records; promotion rejects altered review state. Activated-version metadata also stores the canonical verification hash. Rollback requires external verification records and a passing verdict; it never invents a successful check. The immutable base policy is not part of editable state. Versions use compare-and-swap baselines and support conflict-safe rollback.

## Extension points

Installed packages may register the `our_harness.plugins` entry-point group. A project may also list project-relative Python plugin files and names in config. No plugin runs unless its name is enabled. The application consumes plugin detectors and workflow-policy factories, and doctor consumes plugin checks. A workflow factory returns bounded execution-policy values; it is not an arbitrary executable canvas node.
