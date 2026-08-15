# Best-in-class evaluation gate

The built-in three-task suite is a smoke test. A 3/3 result proves that the configured model and harness can inspect, edit, repair, and grade three small Python tasks. It cannot establish a ranking against other coding agents.

Use this gate before making a comparative performance claim.

## Required evaluation

1. Record the benchmark release, task split, model identifiers, provider settings, prompts, token limits, turn limits, time limits, and cost limit before inference starts.
2. Use an evaluator outside the agent package. Keep reference patches and hidden tests unavailable to the solver.
3. Start every task from the same repository commit in a clean container. Run the benchmark's official tests after extracting the submitted patch.
4. Compare Our Harness with mini-SWE-agent and at least one other current coding harness. Give every harness the same model, task set, tool permissions, token budget, cost budget, turn budget, and time budget.
5. Run all 500 SWE-bench Verified tasks. Use at least three runs for any stochastic configuration.
6. Report resolved count, resolution rate, 95% confidence interval, cost per resolved task, tokens per resolved task, wall time, malformed-response rate, empty-patch rate, and infrastructure-error rate.
7. Run a second independent suite such as Aider Polyglot or Terminal-Bench 2. A gain on SWE-bench must not hide a material loss on the second suite.
8. Publish the configuration, model versions, submitted patches, redacted trajectories, evaluator reports, logs, source revision, package hashes, and commands needed to repeat the run.
9. Run the security, recovery, portability, and package-parity suites separately. Coding resolution does not replace those checks.

## Pass condition

A release may be described as best in class only for its declared model and budget class when:

- its SWE-bench Verified resolution rate is the highest measured rate, or its confidence interval overlaps the highest measured rate;
- the comparison uses the same model and budgets across harnesses;
- the second benchmark shows no material regression;
- all artifacts needed to audit and repeat the result are published; and
- no unresolved critical security, recovery, evaluator-integrity, or portability finding remains.

State the scope in the claim. Examples include `best measured SWE-bench Verified resolution with model X under Y dollars per task` or `best measured local-model result under Z GiB`. Do not generalize one model, budget, benchmark, or platform result to all coding work.

## Primary evaluation references

- [SWE-bench evaluation guide](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md)
- [SWE-bench leaderboard](https://www.swebench.com/)
- [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent)
- [Aider coding leaderboard](https://aider.chat/docs/leaderboards/)
- [Simple Strands Agent](https://github.com/strands-labs/benchmark-harnesses/tree/main/simple-strands-agent)
