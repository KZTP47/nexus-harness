# Resume after provider sign-in or update

A normal sign-in keeps the same saved chat binding. A repair that updates the
CLI (including Nexus's Claude repair) can change its resolved executable,
version, or file identity. Nexus then pauses the chat and durable goal to
protect the saved history. Previously this left only a fresh-session action.

Use **Reconnect saved chat** in the saved-chat list or the team attention
panel. Review the confirmation after signing in to the intended account.
Reconnection keeps the chat ID, transcript, project, goal, task evidence,
budgets and permissions. It leaves the goal paused. Use **Resume team** next;
if a provider reply was interrupted, use its displayed recovery controls.
Unknown file effects are never cleared by reconnection.

The engine accepts only a changed executable fingerprint under the same
configured route, settings and transport/adapter contracts. It checks the
project directory, project execution authority, board, participants, goal
revision and stopped workers. Routes, configuration changes, legacy unverified
bindings and incompatible contracts need their original setup restored or a
new chat. The preview does not send a provider request or inspect credentials.

`provider_reconnect.py` owns the versioned review fingerprint and chat-level
coordination. `GoalStore.reconnect_provider_setup` rechecks and journals the
goal binding change. CLI principal reconstruction proves that only the saved
executable component differs. The authenticated endpoint holds the chat turn
lock and never starts a scheduler. Goals commit while paused before the atomic
chat-registry write; if interrupted between these stores, a fresh review can
complete reconnection without losing work or enabling a send to stale bindings.

Regression coverage: `tests/test_provider_reconnect.py` exercises portable
temporary installations, the authenticated endpoint, negative boundaries,
partial persistence failure, restart and real scheduler completion.
`desktop/provider-reconnect.test.js` covers both the review handler and team
panel in a browser, including cancellation, stale errors, exact chat identity,
binding refresh and narrow/wide layout.

## Desktop disappearance during a rebuild

The desktop shortcut previously launched `desktop/build-output/win-unpacked`.
A concurrent rebuild could lock that directory and cause deployment to stop
the running executable. This happened during a reported reconnect attempt;
the other task's deployment log records the explicit process stop.

`scripts/desktop_launch.py` now publishes a verified launch copy under the
checkout's ignored `.harness/runtime/desktop-apps` directory. The shortcut's
target, working directory and icon all use that copy. Each published build is
kept intact; building and publishing another version cannot replace its files
or terminate its process. A versioned content manifest and checkout fingerprint
bind the copy. Incomplete or changed copies cannot replace the current selection.
Unchanged files can share storage with earlier published copies, but never with
the mutable build output. Published copies are retained for running sessions.

The pre/post hooks, installer rebuild, shortcut refresh and source/package
checks remain in force. `desktop/reconnect-desktop.smoke.js` exercises the real
packaged Electron confirmation, retained transcript and goal, two scripted CLI
providers and deterministic completion. Its optional hold marker keeps that
window open through a real rebuild and verifies the process and chat survive.
