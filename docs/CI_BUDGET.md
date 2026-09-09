# CI and release execution budget

Checks and Windows desktop release target **10 minutes** and have a **15-minute
workflow execution budget**. This is one clock for a workflow attempt, including
serial dependencies. Adding a new job does not give the workflow a new budget.
Primary Python jobs have a twelve-minute local timeout to allow hosted-runner variation
inside that shared budget. The ten-minute target is not a second, earlier kill
switch: two measured runs otherwise cancelled different unfinished partitions at
their local ten-minute limits. The fourteen-minute workflow cancellation point
and fifteen-minute ceiling remain unchanged.
GitHub runner scheduling and cancellation propagation remain platform-owned;
repository code cannot guarantee when a queued hosted runner starts or when
GitHub displays a run as terminated.

## Enforcement

`scripts/ci_budget.py` uses only Python's standard library. Its independent
`Workflow deadline` job reads GitHub's attempt-specific `run_started_at` and
anchors a monotonic clock to that timestamp. It requests whole-run cancellation
at **14 minutes**, leaving one minute inside the budget for termination. After
10 seconds it checks the current run again and force-cancels if it is still
active. The 15-minute job timeout is a final local fallback, not the definition
of the shared deadline.

The watchdog polls every 10 seconds and exits as soon as all expected jobs have
completed successfully. It does not hold an otherwise completed workflow open
until the deadline. Its explicit job manifest includes every matrix expansion
and downstream release job. Missing jobs remain pending; unknown jobs, duplicate
names, failures, neutral results, unexpected skips, malformed evidence, and
persistent API errors fail the guard. A failed guard requests cancellation and never reports a
successful skip. This policy favors a clear failed run over an unverified green
one; it may cancel peer jobs before they finish after a confirmed failure.

Every HTTP call has a five-second total wall-time bound, including DNS and body
reads, enforced by a short-lived Python child process. The token travels over
stdin, not command arguments; API response bodies and subprocess error output
are never copied into diagnostics. Pagination is bounded and must supply a
complete set of jobs. Final messages report the measured workflow elapsed time,
or explicitly say that it could not be verified.

A transient timeout while polling attempt jobs receives one bounded retry on
the same workflow clock. It cannot restart the clock or postpone the cancellation point. A second
failure still cancels the workflow; malformed evidence, identity mismatches,
permission failures, and failed checks remain immediate failures. Initial
timestamp reads and cancellation identity reads remain single calls, and
cancellation requests are not automatically retried. This handles the observed
single slow GitHub read that otherwise cancelled a healthy release after less
than five minutes.

The repository, run ID, and attempt come from `GITHUB_REPOSITORY`,
`GITHUB_RUN_ID`, and `GITHUB_RUN_ATTEMPT`. API responses must match that identity.
Cancellation checks the latest attempt again before each destructive request,
so an observed rerun cannot be cancelled by the old attempt. GitHub's cancellation
endpoint is run-scoped and has no atomic attempt precondition; the small race
between the read and cancellation request belongs to that API contract.

## Workflow integration

Use an independent Ubuntu watchdog job; it must not depend on the jobs it watches
and those jobs must not depend on it. Give only this job `actions: write` plus
`contents: read`, and pass `${{ github.token }}` as `GH_TOKEN` to the helper.
The watchdog job **and its watch step** use `if: ${{ always() }}` so an ordinary
cancellation cannot kill the guard before it requests force cancellation.
Ordinary work jobs should not use job-level `always()`.

```yaml
workflow-deadline:
  name: Workflow deadline
  if: ${{ always() }}
  runs-on: ubuntu-latest
  timeout-minutes: 15
  permissions:
    contents: read
    actions: write
  steps:
    - uses: actions/checkout@v4
    - name: Enforce the workflow execution budget
      if: ${{ always() }}
      env:
        GH_TOKEN: ${{ github.token }}
      run: >-
        python scripts/ci_budget.py watch
        --required-job "Build"
        --required-job "Verify"
```

Use the exact display names from the attempt-jobs API. Normal Checks runs the
full Python 3.13 suite once across eight `Tests, part N of 8` jobs. Python 3.11
receives one short compatibility check instead of another complete eight-part
suite. The user explicitly requested this reduction after the duplicate Python
3.11 run held up otherwise completed CI. This does not claim exhaustive Python
3.11 coverage. The separate broad `Panel checks` browser job is also removed
from routine CI. Its suite remains available through `our-harness qa run` for
manual investigations. The desktop job retains its source-browser and packaged
UI checks, alongside `The project's own suite` and `What we would hand out`.
Normal Checks has thirteen jobs including its deadline guard. The Windows release manifest requires:

- `Build installer`
- `Installed app`
- `Installed long-horizon`
- `Installed team-chat`
- `Installed shortcuts`
- `Publish release`
- `Public downloads`
- `Public source ZIP`

The build uploads the verified installer, checksum, and size metadata first.
The installed-shortcut lane assembles and uploads the offline ZIP after its
acceptance checks, while the longer installed long-horizon lane runs in
parallel. This removes the measured 31 seconds of ZIP assembly/upload from the
path that delayed every installed lane. The ZIP still uses the exact downloaded
installer and checksum and the existing product identity validation. Publication
waits for every installed lane, including the completed offline ZIP upload.

Only manual `workflow_dispatch` releases may explicitly allow the last three
publication jobs to be skipped, by adding an `--allow-skipped-job` argument for
each. Tag releases require their success. An exception never permits a failed
job or bypasses the remaining required jobs. Update the manifest whenever jobs
or display names change.

The eight-way Python split keeps `test_swarm_work` in part 3. Profiling the
overloaded part 8 measured that module at 259 seconds, with every other module
under 40 seconds. In the affected hosted run, part 3 finished in 156 seconds on
Python 3.13 and 197 seconds on Python 3.11, leaving room for this module. The
placement changes no tests and creates no extra job or timeout allowance.
Other partition counts retain their ordinary assignment; exact-once coverage
tests protect both paths. These measurements guide placement, not a promise
that hosted machines always run at the same speed.

For 0.2.20, the expanded board/chat module is assigned to part 4 so it no
longer shares part 3 with `test_swarm_work`. Checks run `34361226193` passed
all 961 tests in part 3 in 698 seconds, but the job was cancelled after
exceeding its own timeout. Moving the complete board module preserves every
test exactly once, the eight-job count, and all existing time limits.

Immediately before the externally visible publication step, run the read-only
check with enough reserve for that step and the downstream public readback:

```text
python scripts/ci_budget.py check --reserve-seconds 180
```

That command requires more than 180 seconds before the 14-minute cancellation
point. The publication job needs `actions: read` to read the attempt, in addition
to its existing narrowly scoped publication permission. Bound the write step
with `timeout-minutes` as well. The reserve must cover the write plus the longest
downstream branch, not merely the time taken by the check. The release reserves
one minute for publication and two for the longest public verification job.
The previous successful public source-ZIP check took 66 seconds; its two-minute
cap retains that entire check with margin. The initial five-minute reserve
rejected a fully accepted installer after 10 minutes 20 seconds, despite enough
time for the measured public checks. The shared 14-minute cancellation point
and 15-minute execution ceiling remain unchanged. Release verification
dependencies must still all succeed before publication. A time check does not
replace product acceptance evidence.

## Platform limits and failure evidence

The watchdog needs a hosted runner and a working GitHub API. A guard delayed in
the queue still charges elapsed time from the attempt's original start and will
cancel immediately if the work deadline has already passed. It cannot execute
while GitHub has supplied no runner. Normal per-job timeouts bound individual
jobs if the watchdog runner or API is unavailable.

Fork and Dependabot pull requests normally receive read-only tokens even if a
workflow requests write permission. They can be verified within the budget, but
cannot self-cancel through the write API if they overrun. Cancellation rejection
is a visible failure, never a green budget result. Do not give untrusted pull
requests a PAT or switch to privileged execution to work around that boundary.
If API authentication, availability, or cancellation fails, the log explicitly
reports that whole-run termination could not be confirmed. The repository does
not claim a hard platform wall-clock guarantee under those conditions.

An API acceptance response is not proof that every runner process has stopped.
GitHub owns final cancellation and process teardown. The one-minute reserve
reduces that gap; real workflow elapsed times and conclusions must still be
checked after rollout before claiming the 10-minute target was achieved.

## Verification

Run `python -m unittest tests.test_ci_budget -v`. Tests use arbitrary repository
identity, fake clocks, and fake APIs; they do not need a local account, token,
runner, installation path, or network access. They cover delayed guard start,
serial jobs that have not appeared yet, expired or failed jobs, intentional and
unexpected skips, API failures, whole-run normal/force cancellation, rerun
identity, pagination, clock rollback, and publication reserve checks.

Primary GitHub references, checked September 2026:

- [Workflow runs REST API: attempt timestamps and cancellation endpoints](https://docs.github.com/en/rest/actions/workflow-runs)
- [Workflow jobs REST API: attempt-specific job listing](https://docs.github.com/en/rest/actions/workflow-jobs)
- [Workflow cancellation: `always()` and runner termination](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation)
- [Workflow syntax: token permissions and fork restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
