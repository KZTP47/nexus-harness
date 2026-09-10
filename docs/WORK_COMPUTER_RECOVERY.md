# Work computer prompt and team recovery

## 0.2.25: handoff continuation and chat tools

CLI replies now receive the same output-format repair as browser replies. The
repair includes the delivered reply and current task context and uses inspect
access for native workspaces. If a reply remains prose, Nexus keeps it as a
work message, collects existing native edits through the normal publication
path, and lets the teammate respond. Prose cannot grant permission, execute
JSON fragments, or certify completion. File validation errors are not mistaken
for formatting errors. Normal agreement and verification still finish goals.

For existing paused format failures, **Force agents to proceed** continues the
same saved goal, preserves drafts and budgets, refreshes stalled progress, and
can reopen an exhausted protocol-correction episode with cumulative usage
retained. The command binds to the current chat, revision and provider setup.
It does not discard pending transactions or replace unresolved user decisions.

The attention panel has a bounded default height and a draggable **Resize team
panel** handle. Arrow keys resize it; Home, double-click, or Reset sizes restore
the default. Each chat remembers its own size while the transcript and composer
remain available. Input controls remain scrollable within the panel.

**Prompt library** beside the composer supports named prompts, search, creating,
editing, deleting, and insertion at the composer selection. Insertion does not
send a message. Exact prompt text persists in the installation's local SQLite
library across restart; concurrent edits require a matching saved revision.

Regression owners: `test_collaboration_reply`, `test_prompt_library`,
`test_goal_recovery`, `prompt-library.test.js`, `team-goal-disclosure.test.js`,
and `team-chat.smoke.js`. Packaged acceptance exercises a malformed CLI reply,
force continuation after restart, and saving/editing/reusing a persistent prompt.

## 0.2.24: Windows paths and provider reconnection

A nested project could fail before either provider received its first prompt:
the filename used for an atomic copy crossed Windows' 260-character limit.
Nexus now uses the native Unicode filesystem spelling at its I/O boundaries,
including copy validation, agent inspection and collection, and transactional
publication/rollback. This does not require an administrator or registry edit.
Saved paths and project ownership identities retain their existing format.

The previous release also changed subscription CLI dispatch from v1 to
v2-native-workspace. A saved chat can now explicitly reconnect across this
known upgrade while preserving its goal, transcript, permissions, budget,
collaboration roles and unfinished agent drafts. Unrecognized contract changes,
different routes/accounts/projects and stale confirmation remain rejected.
Draft migration retains the old copy and can recover from interrupted staging.

The expanded team attention panel now includes its applicable reconnect or
fresh-chat action. Other team problems link directly to their goal controls.
Setup issues take precedence over questions that cannot yet be answered; the
panel expands when a new issue appears and keeps typing drafts during polling.
Answer cards also carry a versioned fingerprint of their decision context.
Scheduler bookkeeping can no longer invalidate an unchanged visible question;
changes to the question, objective, team, permissions, or task evidence still
reject stale answers. Response-loss retries retain one durable answer receipt.

Agents are explicitly instructed to inspect accessible real-project files,
prepare documents/archives in a permitted workspace, and investigate technical
blockers together before asking the user. Shared workspace tools can read files
added after goal admission and prepare them in the agent's draft.

Regression evidence lives in `test_windows_atomic_paths`, `test_provider_reconnect`,
`test_workspace_collaboration`, and the desktop team disclosure tests. Windows
tests exercise the failing ordinary temporary-name operation as a negative
control, then real file I/O under simulated legacy API limits through restart,
agent collection, publication and rollback. Packaged collaboration acceptance
uses scripted providers that must consume each other's actual messages; it
does not assert that every live model or work-computer configuration is infallible.
