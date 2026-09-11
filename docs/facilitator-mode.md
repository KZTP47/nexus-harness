# Nexus facilitator mode

New project conversations work directly in the selected folder. Native CLI tools,
Nexus file tools, and structured changes all use that folder. An agent's saved
files are available immediately; there is no later test-gated delivery step.

Nexus coordinates the conversation and records what happened. The user chooses
participants, including reviewers on the same provider. Reviewer feedback and
test results are separate from an agent saying its work is finished. Failed,
unavailable, and unconfigured checks retain their actual status; Nexus does not
start an automatic repair loop or hide files because a check failed.

Read only, Ask before commands, and Full project access remain user choices.
Explicit fixed writer/reviewer roles still apply. New commands in Ask mode use
the existing Deny / Run once / Always allow controls, bound to the project,
working directory, arguments, and timeout. Full mode allows native CLI work;
saved command denials switch native work to inspection so command requests go
through the permission controls. Ordinary explicit shell commands and nested
working directories are supported. Exit failures and timeouts are recorded.

Since participants share real files, Nexus serializes their turns and queues
other conversations for the same project. This avoids concurrent writers
overwriting each other. It does not certify that an agent's output is correct.

## Existing private work

Historical replies have **Open working files** and a separate **Open destination**.
Their working-files button finds the private folder from saved goal metadata,
including after a goal has migrated. An old reply remains an old working-copy
report, not proof that its files were delivered.

Explicitly resuming a settled old private goal recovers its combined changes to
the selected project using the existing journalled publication transaction.
Tests do not gate this recovery. Conflicting destination changes and unsettled
operations must be reconciled first; neither version is discarded. Private
folders are retained, and subsequent work uses the selected project. Existing
user access and fixed role choices survive migration. Completed historical goals
are not silently rewritten.

## Development build

Remaining regression checks and the final build launch check were not completed,
at the user's request.

Use the built development executable after closing the previous Nexus window.
The installed release remains available. Loading the new build does not itself
move a paused goal's private files; use Resume for that goal when ready.
