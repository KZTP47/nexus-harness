# Benchmark results - 2026-08-14

> **Superseded v2 report.** Keep this file only as historical evidence. Do not cite it as the current harness result. The v3 evaluator changes repair opportunities and treats exact patch/tree matching as diagnostic. Publish new results only after the integrated source is frozen and the v3 runbook passes.

These results describe this checkout and the packaged artifacts built from it. They do not establish a SWE-bench score or prove superiority over another harness.

## Release checks

| Check | Result |
| --- | --- |
| Source test suite | 215 passed, 1 skipped, 0 failed in 59.723 s |
| Source audit | 59 files compiled and scanned; 0 findings |
| Deterministic harness benchmark | 100/100; 12/12 cases passed |
| Package parity | 39 packaged files matched source byte-for-byte in both artifacts |
| Windows Python | CPython 3.14.4, 64-bit |

The skipped test needs Windows symlink privileges. The equivalent junction traversal tests ran.

## Agentic coding result

Provider: local Ollama, `Qwen2.5-Coder-3B-Instruct` GGUF Q4_K_M, temperature 0, one repetition.

| Metric | Result |
| --- | --- |
| Agentic Resolution Score | 0/100 |
| Harness Quality Score | 40/100 |
| Hidden tasks resolved | 0/3 |
| Workflows completed | 2/3 |
| Public test gates passed | 2/3 |
| Hidden evaluators passed | 1/3 |
| Provider calls | 10 |
| Discovery-tool calls | 1 |
| Input/output tokens | 18,979 / 1,556 |
| Agentic attempt elapsed time | 308.083 s |

This model produced plausible patches: two passed the visible checks, one passed its external hidden evaluator, and none met every v2 resolution condition. The 0/3 resolution result shows that the harness can execute and grade a coding loop; it does not show strong coding performance from this small local model.

The scripted oracle fixture resolves 3/3 and scores 100/100. That is an evaluator calibration, not a model benchmark. The deliberately incomplete fixture passes its visible checks and fails the hidden checks, confirming that visible-test success alone cannot receive resolution credit.

## Package measurements

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `harness.pyz` | 158,441 | `abb14e5503f44d6f2730301beb84909eab96786a6b6973aedda8e3261173f4ea` |
| `our_harness_cli-0.1.0-py3-none-any.whl` | 167,547 | `0986d4124f3306a50de8479705d22c798b47854ac1aac81219f6e46fda359300` |

Median cold CLI version probe for the zipapp was 449.3 ms across seven Windows runs. The source package contains 32 Python files and 12,581 physical lines. Size and line count are inventory values, not quality scores.

Both benchmark files bind the same source artifact hash: `fc6c41cf67ed017cd52c30c2cbe2e2be7584cfbe48ceec10c62dcc2251e6d5f5`.

## Interpretation

The deterministic score supports claims about the tested harness contracts: bounded execution, transaction recovery, confinement, context budgeting, repository indexing, streaming, and graph validation. The local provider score measures the complete harness-plus-model system on three small isolated tasks.

The sample is too small for a stable model ranking. It has one platform, one local model, one repetition, and no official SWE-bench Verified run. Before making comparative coding-performance claims, run a larger fixed task set with several models and repetitions, publish confidence intervals, and compare identical tasks, budgets, and tool permissions.
