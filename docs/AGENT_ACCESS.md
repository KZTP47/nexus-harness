# Agent access and command permissions

Team chats display **Agent access**. Pause the team before changing it; the
setting applies to every participant in that goal. The new-goal dialog also
offers the setting before work starts.

| Mode | Behavior |
| --- | --- |
| Read only | Inspect project files; no project edits or test command execution. |
| Ask before commands | Permit edits. Ask before newly discovered commands. Explicitly configured commands and existing exact approvals remain authorized. |
| Full project access | Permit project edits and supported project test commands without command prompts. |

Every mode retains Nexus's confined file operations, disposable verification
copies, protected runtime, independent review rules, and publication checks.
Full project access does not grant arbitrary access to the computer or change
permissions in an external provider application.

When a verification tool requires permission, the engine saves an actionable
command request and pauses at the tool boundary. The chat shows the command,
project, and the resolved package script where available. The choices are:

- **Deny**: retain the work and return the denial to the team. A pause caused
  solely by this command request resumes; an explicit user Pause, another
  question, or an unsettled effect remains stopped. The command stays denied
  until the user changes the decision or access mode.
- **Run once**: allow one verification attempt for the displayed command set.
  Consumption is atomic and durable, including across a restart or failed run.
- **Always allow this command in this chat**: remember the exact command
  definition for this goal. Other chats receive no grant.

Allowing resumes the team unless another user question or interrupted provider
call still needs a decision. Saved permission is retained in either case.
Older paused chats can open **Review command permissions** to recover the
missing decision flow. Conversational answers such as “yes” are not execution
grants; use the command card.

Access state is versioned and bound to the goal, project authority, engine
execution contract, and agent bindings. Command grants additionally bind the
canonical project path, argument lists and discovery manifests. Changed
commands need a new grant; changed access bindings fail closed to Read only.
An explicitly reviewed compatible provider reconnect rebinds validated access
and retains exact grants, including consumed one-use grants.
The context fingerprint changes for user decisions, but not when a one-use
grant is consumed, so successful execution evidence remains available.

Simple npm/pnpm/yarn scripts using Node, Python, or Playwright resolve into
the existing protected runner profiles. Nexus retains the approved package
command in execution evidence. It rejects compound shell scripts and lifecycle
hooks with an explanation instead of omitting them. Use an explicit supported
runner command for those projects.

The conversation-level selector and decision-card pattern is inspired by
[T3 Code's permission modes](https://github.com/pingdotgg/t3code/blob/main/docs/user/permission-modes.md).
The implementation uses Nexus's own durable store and execution boundaries.

Verification: `python -m unittest tests.test_goal_access` exercises actual
contained Node and Playwright checks, HTTP decisions, restart, single-use
consumption, denial, changed commands and bindings, and file-write rejection.
`node --test desktop/goal-access.test.js desktop/team-goal-feedback.test.js`
checks the interactive controls and responsive chat layout in Chromium.

## Interrupted turns and Resume

Command approval and restart recovery are separate engine states. A lost
provider reply must not become an endless command-approval loop. The chat now
shows the engine's versioned `resume_recovery` projection beside the composer.

On explicit Resume, a provider-only interrupted call made under the exact
`codex-cli/isolated-exec/v1` + `codex-cli/effective-dispatch/v2` contract can be
superseded with a fresh read-only inference. That contract uses an ephemeral
directory, ignores user configuration and rules, rejects native tool contracts,
and enforces a read-only sandbox. This bounded compatibility path also covers
old authenticated calls admitted under that same contract. Startup alone never
resends them. The project identity, workspace and current provider binding must
still match, and the old worker must have stopped.

Other interrupted provider calls require the visible **Retry interrupted agent
call** choice, which explains possible duplicate remote actions/usage. Its
versioned fingerprint and goal revision bind the exact effects shown. Stale,
cross-chat, changed-configuration, and duplicate submissions fail closed.
Recovery commits together with Resume; a later failed Resume check rolls back
the recovery. Prior effect IDs remain in the audit trail, and old leases cannot
deliver replies into a new attempt. Completed work, access grants and spent
budgets are preserved. Pending file transactions/actions are never discarded by
this provider-only recovery path.

Regression checks: `python -m unittest tests.test_goal_recovery` and
`node --test desktop/goal-recovery.test.js`. They cover authenticated HTTP
controls through scheduler completion, real file publication, restart,
permission changes, stale choices, changed providers, file-effect rejection,
late replies, and responsive recovery controls. The packaged smoke check also
renders the recovery card using the installed renderer.
