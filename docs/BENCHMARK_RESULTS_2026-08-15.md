# Benchmark results - 2026-08-15

These results bind the current source and release artifacts. They separate harness verification from model coding quality.

## Release checks

| Check | Result |
| --- | --- |
| CPython 3.14 source suite | 372 passed, 1 permission skip, 0 failed |
| CPython 3.11 source suite | 372 passed, 1 permission skip, 0 failed |
| Source audit | 80 files compiled and scanned; 0 findings |
| Deterministic benchmark | 100/100; 12/12 cases passed; 0 critical failures |
| Package parity | 50 package files matched source in the zipapp and wheel |
| Packaged UI browser check | Add Agent, focus restore, pointer-drag connections, invalid-route feedback, live usage, shared memory, and prompt history passed; no console warnings |

`benchmark-current.json` is schema v3, seed 17. Its SHA-256 is `9458e5394eae78d319ef349a398c74fc643c42588802046f844ade25e77e623f`. Agentic evaluation is `not_run` in that file by design.

## Provider-backed run

The ChatGPT-backed Codex profile passed its local executable, login, private model-catalog, strict-schema, and usage-parser checks. The final three-task run then hit the account's Codex usage quota before any successful provider response.

| Metric | Recorded result |
| --- | --- |
| Successful provider calls | 0 |
| Agentic Resolution Score | Not a valid quality measurement |
| Harness Quality Score | Not a valid quality measurement |
| Workspace safety checks | Passed for all 3 tasks |
| Infrastructure cause | Codex usage quota exhausted until the provider-reported reset time |

The result file contains the evaluator's mechanical `0` ARS and `40` HQS because no workflow could start. Do not cite those numbers as coding quality. `benchmark-codex-subscription.json` has SHA-256 `99c78fa738790186ad77ca703edf241cd3cf46fb4539e4f15d7f37019259c790` and retains the exact failure evidence.

Historical local Qwen runs are stored under `benchmark-archive/` with `.archived` extensions. They bind older source revisions and remain diagnostic records, not current release scores. The best historical three-task local run resolved 1/3 behaviorally. Later CPU-only 7B attempts were limited by 221-300 second turns and truncated structured output.

## Release artifacts

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `harness.pyz` | 262,942 | `905af8c2aeab6c1de66be51c18af3f615af52fcb33d9ada08e79b7423ad52ad2` |
| `our_harness_cli-0.1.0-py3-none-any.whl` | 273,131 | `23c38c8c7e42ef197145cb96e9f8b2e370c3503a37446b2598579a8439e22ffc` |

## Best-in-class claim

No best-in-class coding claim is supported yet. The built-in suite is a three-task smoke test. The required external run did not start on this machine because the Docker daemon is unavailable and the system drive has about 50.9 GB free.

The release includes a provider-free SWE-bench preparation and validation path. A valid claim still requires the official SWE-bench Verified 500-task evaluator, identical model and budgets across Our Harness and comparison harnesses, repeated runs, confidence intervals, published patches and trajectories, and a second benchmark. The exact gate is in [BEST_IN_CLASS_EVALUATION.md](BEST_IN_CLASS_EVALUATION.md).
