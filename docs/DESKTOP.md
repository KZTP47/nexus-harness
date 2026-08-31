# The desktop app

The desktop app is a window around the same control panel that `harness ui`
serves. It starts the local harness server for the folder you pick, shows it,
and stops the server when you close the window.

It lives in `desktop/` and is separate from the Python package. You do not need
it to use the harness.

## Installing the released app

Stable Windows releases contain a versioned per-user NSIS installer, a
private Python 3.11 runtime with exact locked dependencies, and a matching
SHA-256 file. They are built and smoke-tested on a clean GitHub-hosted Windows
runner. Use the release page directly, or double-click
`Install Nexus Harness.cmd` in a clone to download and verify the stable
release automatically without system Python. The installer creates desktop
and Start menu shortcuts.

When an Authenticode publisher is configured, both CI and the bootstrap require
that exact valid signer. Until then, the published asset is visibly named
`*-UNSIGNED.exe`; CI verifies it is actually unsigned, and the bootstrap
requires its published SHA-256 and declared unsigned mode before running it.
Windows may show an unknown-publisher warning. Untagged local/development
builds remain `*-UNSIGNED-DEV.exe` and are not accepted as public releases.

While the repository is private, the bootstrap reuses an existing GitHub CLI
or non-interactive Git Credential Manager login, or a process-scoped
`GH_TOKEN`. It never prints or stores the token. A machine with no GitHub login
must download the installer and checksum through a signed-in browser. Truly
anonymous installation requires a public repository or separate public
distribution repository.

Only use `python scripts/put_it_on_your_desktop.py` when developing from a
checkout. Its source fallback is intentionally described as a development
window, not as an installed Electron release.

## What source development needs first

- Python 3.11 or newer, with the harness and test runner installed for it (`python -m pip install ".[test]"`)
- Node.js 22.12 or newer, to build or run the window

## Run it from source

```bash
cd desktop
npm install
npm start
```

The first time it opens, it shows a short welcome page with one button. Press it
and pick the folder you want to work on. It remembers that choice, so next time
it goes straight there. Use **Project, Open another folder** to change it.

## Build an installer

```bash
cd desktop
npm run build
npm run smoke:packaged
```

The supported release build currently produces an NSIS installer on Windows
under `desktop/build-output`. The second command opens the packed archive that
installer ships and confirms every file the app loads is really inside it.
The Python CLI remains cross-platform; macOS and Linux desktop packages are not
part of this release contract yet.

That second step matters. The installer ships only the files named in
`package.json`, and a file the app loads but nobody listed is simply missing at
run time: the installed app dies on start with no window and no message. That
happened once here, so `npm test` now also checks the list against what the code
actually loads, and fails before anything is built.

The installer carries the Electron window, a compatible Nexus Harness source
snapshot, and its own private supported Python. Installed mode never substitutes
`py`, `python`, `HARNESS_PYTHON`, or another checkout for that private runtime.

## How source mode finds Python

Only source development does this. It tries, in order: `py -3`, `python`, then `python3` on Windows, and `python3`
then `python` elsewhere. If your Python lives somewhere unusual, set
`HARNESS_PYTHON` to its full path before starting the app. When that variable is
set, no other command is tried, so a typo is reported instead of quietly running
a different Python.

Python below 3.11 is rejected before Nexus imports the application. If an
installed build reports a missing `resources/runtime/python.exe`, that package
is incomplete: reinstall the same checksummed release rather than installing a
system Python.

## What it does at start

It shows its window first, with a short welcome page and one button. Only then
does it look for the folder you used last time. A folder picker on top of a
blank screen tells a first-time user nothing, so the window always comes first.

Once a folder is chosen:

1. Starts `python -m our_harness --project <your folder> ui --port 0 --no-open-browser`.
2. Waits for the server to print the address it bound to.
3. Checks that address is on this machine, then loads it.

Port `0` asks the system for any free port, so a restart or an intentional
source-development window cannot collide with a stale fixed port. One installed
desktop-app instance owns one selected project and one server process; launching
Nexus again focuses the existing window. Switch projects from the Project menu.
**Help → About and
diagnostics** shows the exact Nexus version, executable, selected project root,
server address, port, and process ID, which prevents a stale checkout from
masquerading as the current app.

If the server does not start within 45 seconds, or stops on its own, the window
shows what it printed and offers to try again.

The first project screen checks the exact routes used by the workflow and its
configured agents. One unrelated healthy provider cannot make Start look
ready. A machine-local executable config is never trusted silently: the native
error page shows its exact path and contents, lists the command/model/MCP/plugin
consequences, and records trust only after the user presses the explicit trust
button. A project without tests can select bootstrap mode; Nexus then creates
maintainable test infrastructure first and must run it before claiming success.

If an installed app is older than the project's settings, the error page also
offers **Fix and start**. That action uses `src/our_harness` from the chosen
project for the retry and remembers the choice for that project. On later
starts, the bundled harness is still tried first; if it reports the same
version mismatch, the app falls back to the project copy automatically. This
means a newly installed compatible bundle takes over without any cleanup.

## What it will not do

- It never loads a page from outside this machine. A link to anywhere else opens
  in your own browser instead, where you can see the address first.
- The page has no access to Node, the file system, or a shell. It can ask the
  app only for narrow named actions such as choosing a folder, trying again,
  showing help, or accepting the project-copy repair described above.
- It answers no to every browser permission request, such as camera or location.

## Testing it

```bash
cd desktop
npm test              # the start-up logic and the installer file list, with no window
npm run smoke         # starts the real app from source and checks the window
npm run build         # makes the installer
npm run smoke:packaged  # checks what the installer actually carries
```

The smoke run needs Playwright, which the project root installs for its browser
checks. It opens the app against this repository, walks through every tab, and
fails if the browser console reports anything.
