# Nexus facilitator mode

New project conversations default to **Facilitator mode · direct project work**.
In an expanded chat, open **collaboration settings → Facilitator mode**, then **Project work mode**, to
choose it or **Private copies · verified delivery**. The same choice remains in
the permissions menu and goal composer. **CHAT** opens by default and gives the
conversation its own pane; team settings, questions, command approvals and
recovery live in **collaboration settings**, with an **Input needed** indicator
when attention is required. The Facilitator mode card always shows whether it is on or off. A paused private-copy goal offers **Switch to facilitator mode**, which explicitly recovers its files and resumes in the selected project. Chat setup and saved files are always expanded and share the settings page scroll; they have no inner scrolling pane. Existing API clients that omit
`policy.execution_mode` retain isolated execution; `facilitator` selects direct
work and `isolated` selects private work. In direct mode, native CLI tools,
Nexus file tools, and structured changes all use that folder. An agent's saved
files are available immediately; there is no later test-gated delivery step.

Nexus coordinates the conversation and records what happened. The user chooses
participants, including reviewers on the same provider. Reviewer feedback and
test results are separate from an agent saying its work is finished. Failed,
unavailable, and unconfigured checks retain their actual status; Nexus does not
start an automatic repair loop or hide files because a check failed.
Success criteria and missing referenced files remain visible as observations;
finishing agent turns does not certify those criteria. A final check needing a
new command grant pauses for the existing permission controls. Denying that check
allows completion with its unavailable result recorded.

Read only, Ask before commands, and Full project access remain user choices.
Explicit fixed writer/reviewer roles still apply. New commands in Ask mode use
the existing Deny / Run once / Always allow controls, bound to the project,
working directory, arguments, and timeout. Full mode allows native CLI work;
saved command denials switch native work to inspection so command requests go
through the permission controls. Ordinary explicit shell commands and nested
working directories are supported. Exit failures and timeouts are recorded.
Nexus-run commands retain configured executable/argument denials, built-in
prohibitions, and the chosen process or Docker backend. Changing this execution
policy invalidates native-command grants. Docker mode uses native inspection
and routes commands through Nexus. Native CLI work in Full/process mode uses the
provider's own permissions and is not an operating-system security sandbox.

Since participants share real files, Nexus serializes their turns and queues
other conversations for the same project. This avoids concurrent writers
overwriting each other. It does not certify that an agent's output is correct.

## Existing private work

Historical replies have **Open working files** and a separate **Open destination**.
Their working-files button finds the private folder from saved goal metadata,
including after a goal has migrated. An old reply remains an old working-copy
report, not proof that its files were delivered.

Normal Resume keeps an existing private goal's execution mode. The separate
**Recover files and resume in selected project** action on a paused goal recovers its combined changes to
the selected project using the existing journalled publication transaction.
Tests do not gate this recovery. Conflicting destination changes and unsettled
operations must be reconciled first; neither version is discarded. Private
folders are retained, and subsequent work uses the selected project. Existing
user access and fixed role choices survive migration. Completed historical goals
are not silently rewritten.

## Integration

Adapted from [Naphal3000's facilitator implementation](https://github.com/Naphal3000/nexus-harness-fork/commit/b8d8ca2e1b907ade50c6119a4d155dc4caded9de).
Nexus retains its source tests, CI workflows, legacy isolated workflows and
versioned project/access bindings. Mode-specific regression coverage lives in
the existing goal-access, delivery and native-input test modules.
