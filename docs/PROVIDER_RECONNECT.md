# Resume after provider sign-in or update

A normal sign-in keeps the same saved chat binding.

## What pauses a saved chat, and what does not

A saved chat records each agent's provider in two parts (`swarm_chats.py`,
`chat.route_identity`).

- **Identity** (`nexus/route-identity/v5`, stored in the binding's optional
  `route_identities` map beside `agent_routes`). It is an allow-list: the route
  name, the provider kind, the endpoint authority, the credential slot and
  account scope, the whole command line and `arguments`, the environment
  settings, and for the local provider its execution mode and container
  (`execution.mode`, `docker_image`, `docker_network`). Positional words and
  unknown flags are identity. So `python bridge_a.py` → `bridge_b.py`,
  `npx -y <package>`, `ssh user@host`, `wsl -d <distro>`, `codex --oss` and
  `--profile other` all change it. In a command, tunable flags are recognised
  only after the kind's own program (`claude`, `codex`, `gemini`,
  `copilot`). The program counts only at `command[0]` or as the target of a
  known launcher: `npx`/`bunx`/`pnpm dlx`/`yarn dlx`, `node`, `cmd /c`, or
  `wsl ... --`/`-e`. It may be spelt as a package (`@openai/codex@latest`),
  an npm-vendored binary (`codex-x86_64-pc-windows-msvc.exe`) or
  `node .../@anthropic-ai/claude-code/cli.js`. An ssh host, docker container,
  wsl distro or Python module that is merely named `codex` is not the
  program, and a command without the program is identity as a whole. So are
  its `arguments`, because they go to something else. A wrapper's `-m` or
  `--model` (`python -m bridge_a`) is never taken for the model flag. A
  tunable flag never takes a following flag as its value. The endpoint's
  username and non-credential query parameters (`?tenant=`) are identity too.
  Passwords and credential-looking parameters (`key`, `token`, `sig`, ...)
  are not.
- **Tunables**: settings such as the model, reasoning effort, timeouts, output
  caps and concurrency (`chat.ROUTE_TUNABLE_FIELDS`), and the flags that
  `chat.TUNABLE_FLAGS_BY_KIND` lists for each kind. Those are model, reasoning
  effort, output format, verbosity and timeouts, plus Codex `-c key=value` for
  tuning keys such as `model` or `model_reasoning_effort`. For `assistant-cli`
  and `local` no flag is known to be tunable, so the whole command line is
  identity. Tunables also include the program's resolved path, file identity
  and reported version, and the transport or engine contract revision.

If only tunables changed, the chat continues. `_refresh_route_bindings`
writes the current route and effective-dispatch fingerprints over the old ones
under the registry transaction, so the next turn and any goal admitted from
this chat see the current setup. A CLI auto-update, a new `--version` output
or a timed-out version probe is recorded this way and never pauses the chat.
A timed-out or failed probe is also remembered for five minutes, so the digest
stays stable instead of flipping between observations.

If the identity changed, the chat pauses (`route_identity_changed`) and offers
**Reconnect saved chat**. A change of route name is `route_changed`: the
transcript stays with the old route, and the recovery is a fresh chat.

**Migration from identity v1-v4:** those versions ignored inputs v5 covers
(v1 every flag; v2 positional words and unlisted flags; v3 wrapper flags that
look like tunables, endpoint username and query; v4 a host, container or
distro named like the program). An old record is
upgraded to v5 silently only when its own digest still matches *and* the
saved route fingerprint (the whole profile) is unchanged. A CLI update alone
keeps that fingerprint. Any other change, for example removing
`--profile work`, pauses the chat for reviewed reconnection.

**Migration from no identity record:** chats saved before identity v1 have no
identity record. When all
of a chat's route fingerprints still match the current setup, the identity is
recorded automatically. That match proves the setup is the one saved. A chat
that had already drifted before the upgrade cannot prove that only a tunable
changed, so it offers the reviewed reconnect instead of only a fresh start.
`agent_routes` entries keep exactly their previous fields, so goal admission's
field-for-field comparison and older readers are unaffected. Older readers
drop the unknown map.

## Reconnecting

Use **Reconnect saved chat** in the saved-chat list or the team attention
panel. Review the confirmation after signing in to the intended account.
Reconnection keeps the chat ID, transcript, project, goal, task evidence,
budgets and permissions. It leaves the goal paused. Use **Resume team** next;
if a provider reply was interrupted, use its displayed recovery controls.
Unknown file effects are never cleared by reconnection.

For the chat itself, the engine accepts any change on the same named route
(`provider_reconnect.chat_route_reviewable`), including a changed identity.
The change is applied only after you confirm the exact reviewed fingerprint.
For durable goals, the engine still accepts only a changed executable
fingerprint under the same configured route, settings and transport/adapter
contracts (`provider_reconnect.compatible`). It checks the project directory,
project execution authority, board, participants, goal revision and stopped
workers. A goal whose route settings changed needs its original setup
restored, or a new goal. The preview does not send a provider request or
inspect credentials.

Known gap: goals keep their own admission binding. `GoalStore.provider_setup_status`
compares the full fingerprints, so a model edit or CLI update still marks a
running goal as `provider_setup_changed`, even though its chat continues.

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
