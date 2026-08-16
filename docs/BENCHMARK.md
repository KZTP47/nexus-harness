# Local benchmark

Version 3 contains two deliberately separate measurements:

- a provider-free deterministic conformance score;
- an optional black-box Agentic Resolution Score when a trusted provider profile is supplied.

Neither measurement is a result for another harness or for an external benchmark.

## Deterministic conformance

The deterministic cases exercise production safety, recovery, context, indexing, graph, stream, and execution contracts. Each manifest case receives its full weight or zero. Weights total 100. If a critical case fails, the displayed deterministic score is capped at 49; the uncapped score and every failure remain in the result.

The seed controls case order and generated fixture tokens. Temporary directory names and timing are not correctness evidence.

## Agentic Resolution Score

Supplying `--provider-profile` runs three small standard-library Python maintenance tasks through the public `HarnessApplication.run_task` entry point. The profile must be an explicit JSON file containing a `provider` object with at least `name` and `model`. Benchmark config starts from built-in defaults plus evaluator-owned overrides and the explicit provider object. It does not read user, project, or `HARNESS_*` config. Plugins, MCP, embeddings, and alternate workflows are disabled. The profile and final effective config are hashed, but their contents are not copied into task workspaces or results.

Each task attempt receives a new temporary repository. The agent sees the task, implementation, README, and public tests. An evaluator-owned public-test driver runs after each candidate, so public failures reach the bounded repair loop. The driver command is hidden from model prompts and retained evidence replaces its path with a control placeholder. Hidden evaluation never runs inside the workflow.

After the workflow stops, the authored tree is captured once. Final public and hidden grading then use separate randomly named evaluator roots and separate copies of that captured tree. Hidden evaluator programs remain outside the project root, model context, retained trajectory, and public evaluator root.

The shipped local-Qwen profile allows 180 seconds per provider request. Each agentic fixture allows 420 seconds for its full workflow, including one bounded semantic-contract correction. These are fixed benchmark budgets for CPU reproducibility; they do not change normal project defaults.

Evaluator Python starts with `-I -S`, a sanitized environment without `PYTHONPATH`, and a controlled empty working directory. Only the submitted task-tree copy is added to its import path. This prevents an installed wheel, zipapp, source checkout, or provider environment from exposing packaged expected solutions and hidden fixtures to candidate imports. Tree identity is checked before and after both public and hidden execution. Neither the fixture nor evaluator installs packages or accesses the network. A remote provider profile may independently contact its configured provider endpoint.

An attempt resolves only when every required check passes:

- the normal harness workflow completes;
- public tests pass;
- external hidden tests pass;
- changes stay within the allowed implementation paths;
- the submitted implementation satisfies the public and hidden behavior;
- public tests, documentation, and the generated seed file remain unchanged;
- public-test execution leaves its submitted-tree copy byte-identical;
- hidden-test execution leaves its independent submitted-tree copy byte-identical;
- no linked, reparse, or unexpected authored path appears.

The Agentic Resolution Score is `100 * resolved attempts / total attempts`. Tasks are binary: a partial patch receives zero for that attempt. Repetitions are deterministically seeded and bounded from 1 through 10. The JSON includes every task attempt plus elapsed time, provider-call counts, token counts when reported by the adapter, discovery-tool calls, and tool-output bytes.

`agentic_score` remains the literal `not_run` when there is no profile. In that case no Harness Quality Score is published. Once agentic tasks run, the result includes:

```text
HQS = 0.40 * deterministic_score + 0.60 * agentic_score
```

HQS is a local suite composite, not a cross-harness claim.

## Reproducibility and anti-leakage record

Each result records the suite and schema versions and hashes, deterministic seed, UTC start time, source hashes, provider-profile hash when used, Python and operating-system metadata, elapsed measurements, and per-case evidence. Agentic task records include expected and actual tree hashes plus the external hidden-evaluator hash.

The evaluator code and hidden tests are never placed under the agent workspace. The original submission is graded before any candidate import. Public and hidden execution cannot repair that recorded submission, and mutations during either phase fail their independent identity checks. Generated `.harness` state and interpreter caches are excluded from authored-tree comparison. Unexpected authored files fail scope grading. Expected patch and tree hashes remain diagnostics; a different correct implementation can resolve. Test and documentation hashes are checked independently so a test edit cannot earn resolution.

## Formats

JSON is the machine-readable record and follows `benchmark_result.schema.json`. Markdown is a compact rendering:

```bash
harness benchmark --format json
harness benchmark --format markdown
harness benchmark --provider-profile provider-profile.json --format json
```

Both the Python API and `--repetitions` accept values from 1 through 10.

Use [EXTERNAL_BENCHMARKS.md](EXTERNAL_BENCHMARKS.md) to freeze an equal-budget comparison. The local three-task score is a smoke test, not an external benchmark result.
