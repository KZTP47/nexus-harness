---
name: nexus-completion-discipline
description: Use for substantive Nexus Harness work with material omission risk, including requests with multiple independent outcomes, multi-session execution, exhaustive scope, explicit terminal conditions, or prior partial completion. Reuse Nexus requirement ledgers and parallel schedulers; skip lookups, narrow known-root-cause fixes, and small reversible edits.
---

# Nexus completion discipline

<!-- nexus-completion-discipline-contract:v1 selective=true denominator=current-request parallel=ready-wave reverify=parent integration=branch no_unlazy_state=true -->

Apply this contract only while working in the Nexus Harness repository. Keep
the repository's `AGENTS.md`, private-memory hooks, provider boundaries, and
portable engine-level rules authoritative.

## 1. Establish the completion denominator

Before implementation, reread the current request and every amendment. Record
each independently omittable outcome and acceptance-changing constraint in the
existing task plan or, when Nexus is executing the work, its structured
`requirement_ledger`.

Each item needs:

- a stable ID;
- an observable outcome rather than an activity;
- one owner;
- dependencies and resource paths when concurrency decisions need them;
- a runnable evidence method, or the exact manual observation when automation
  cannot decide it; and
- a current state that distinguishes pending, active, verified, blocked, and
  explicitly out of scope.

Do not count abandoned, deferred, blocked, or owner-decision items as complete.

## 2. Reuse Nexus-owned state and scheduling

Do not create `GATES.md`, `.unlazy/`, a fixed-depth task tree, a Stop hook, or a
second progress hierarchy. Nexus already has the relevant owners:

- `workflow.py` owns the structured requirement ledger, coder witnesses,
  checkpoints, and frozen review packets;
- `cooperation.py` owns deterministic fan-out/fan-in for workflow nodes; and
- `long_horizon.py` owns task dependencies, `parallel_safe`, `resource_paths`,
  claims, and persisted goal state.

Use those facilities when the product is running the work. For direct repository
work, use the active task plan as the single current-request ledger.

This skill does not authorize delegation. If the user or higher-priority
project policy already permits subagents and at least two independent leaves
are ready, declare disjoint ownership, dependencies, and evidence, then launch
the whole ready wave before the first wait. Promote newly ready work only after
its prerequisites verify. Keep shared resource paths, project mutation, and
unfinished interfaces sequential.

## 3. Reverify and integrate

Treat delegated results as candidate evidence. The primary agent reruns each
runnable check and inspects any manual evidence. After related leaves join, run
the relevant interface, end-to-end, persistence/restart, changed-environment,
and regression checks at their common owner. Leaf-level success does not prove
integration.

Use the narrowest practical verification first, then expand according to risk.
Never weaken schema, path, permission, provider, vault-binding, or closeout
checks merely to obtain a passing result.

## 4. Reconcile before claiming completion

Immediately before the final claim:

1. Reread the latest user request and amendments.
2. Reconcile every current-request ID against fresh evidence.
3. Confirm joined work passes branch-level integration checks.
4. State any remaining, blocked, deferred, or owner-decision item explicitly.
5. Run the mandatory Nexus post-work hook and treat a failed deployment gate as
   incomplete closeout.

A handoff is an honest partial outcome, not completion. Do not claim completion
because the plan is exhausted, an agent reports success, or a budget is nearly
spent.
