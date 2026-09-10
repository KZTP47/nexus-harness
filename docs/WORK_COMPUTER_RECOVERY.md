# Work computer prompt and team recovery — 0.2.24

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
