# External benchmark plans

The external benchmark helper freezes evaluator arguments and checks prediction coverage. It does not generate SWE-bench patches, install Docker, download datasets, or claim an official score.

SWE-bench Verified has an official 500-task `test` split. It has no official `dev` or `mini` split. This project therefore names three different targets:

- `swebench-verified-mini-v1`: a deterministic 50-task smoke subset of Verified `test`; not a leaderboard result;
- `swebench-dev`: the official 225-task development split of the original SWE-bench dataset; not Verified;
- `swebench-verified-full`: the official 500-task Verified `test` split used for comparative claims.

The official evaluator applies each submitted patch and runs tests in Docker. Predictions use JSONL records with `instance_id`, `model_name_or_path`, and `model_patch`.

## Freeze the 50-task smoke subset

Export all 500 Verified instance IDs from the pinned dataset revision to one ID per line. Then run:

```powershell
py -B scripts/prepare_external_benchmark.py select-mini `
  --all-instance-ids .\external-results\verified-all-instance-ids.txt `
  --output .\external-results\verified-mini-v1-instance-ids.txt
```

Selection hashes the fixed salt and each instance ID, sorts by that hash, and takes 50. The command refuses to overwrite an existing selection file.

## Freeze model, budget, tools, and revisions

Copy `docs/examples/external_benchmark_fairness_lock.example.json`. Fill every revision before inference. One lock applies to every harness in the comparison. It records one model configuration, one turn/time/cost budget, and one tool policy.

The manifest requires Our Harness, mini-SWE-agent, and Simple Strands Agent. A revision can be a Git commit or a content-addressed package digest. Record the SWE-bench evaluator revision separately in the experiment report.

```powershell
py -B scripts/prepare_external_benchmark.py plan `
  --suite swebench-verified-mini-v1 `
  --predictions .\external-results\our-harness-predictions.jsonl `
  --instance-ids .\external-results\verified-mini-v1-instance-ids.txt `
  --all-instance-ids .\external-results\verified-all-instance-ids.txt `
  --fairness-lock .\external-results\fairness-lock.json `
  --run-id our-harness-verified-mini-v1 `
  --max-workers 4 `
  --output .\external-results\our-harness-verified-mini-plan.json
```

The plan recomputes the 50-task selection from all 500 IDs and rejects a hand-picked subset. The predictions file may be absent when the plan is frozen. After patch generation, validate exact coverage:

```powershell
py -B scripts/prepare_external_benchmark.py validate-predictions `
  --predictions .\external-results\our-harness-predictions.jsonl `
  --instance-ids .\external-results\verified-mini-v1-instance-ids.txt
```

Regenerate the plan under a new run ID after predictions exist. Its `official_evaluator_argv` is an argument array. Invoke that array directly, not through a shell. For `swebench-dev` and `swebench-verified-full`, omit `--instance-ids`.

Run each harness from its frozen revision in an isolated container. Give every harness the lock's model, budgets, and tools. Do not tune on hidden test outcomes. Report infrastructure failures and empty patches separately from unresolved tasks.

## Sources

- [Official SWE-bench evaluation guide](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md)
- [Official SWE-bench dataset and split table](https://github.com/SWE-bench/SWE-bench/blob/main/docs/assets/evaluation.md)
- [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent)
- [Simple Strands Agent](https://github.com/strands-labs/benchmark-harnesses)
