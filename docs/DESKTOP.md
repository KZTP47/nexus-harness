# The desktop app

The desktop app is a window around the same control panel that `harness ui`
serves. It starts the local harness server for the folder you pick, shows it,
and stops the server when you close the window.

It lives in `desktop/` and is separate from the Python package. You do not need
it to use the harness.

## What you need first

- Python 3.11 or newer, with the harness installed for it (`python -m pip install .`)
- Node.js 18 or newer, to build or run the window

## Run it from source

```bash
cd desktop
npm install
npm start
```

The first time it opens, it asks which folder you want to work on. It remembers
that choice. Use **Project, Open another folder** to change it.

## Build an installer

```bash
cd desktop
npm run build
```

This produces an NSIS installer on Windows, a DMG on macOS, and an AppImage on
Linux. The installer carries the window only. Python and the harness package
stay a separate install, because the harness is meant to run against the Python
you already use for your project.

## How it finds Python

It tries, in order: `py -3`, `python`, then `python3` on Windows, and `python3`
then `python` elsewhere. If your Python lives somewhere unusual, set
`HARNESS_PYTHON` to its full path before starting the app. When that variable is
set, no other command is tried, so a typo is reported instead of quietly running
a different Python.

## What it does at start

1. Starts `python -m our_harness --project <your folder> ui --port 0 --no-open-browser`.
2. Waits for the server to print the address it bound to.
3. Checks that address is on this machine, then loads it.

Port `0` asks the system for any free port, so two projects can be open at once
without clashing.

If the server does not start within 45 seconds, or stops on its own, the window
shows what it printed and offers to try again.

## What it will not do

- It never loads a page from outside this machine. A link to anywhere else opens
  in your own browser instead, where you can see the address first.
- The page has no access to Node, the file system, or a shell. The only two
  actions it can ask the app for are "choose a folder" and "try again".
- It answers no to every browser permission request, such as camera or location.

## Testing it

```bash
cd desktop
npm test          # the start-up logic, with no window
npm run smoke     # starts the real app and checks the window
```

The smoke run needs Playwright, which the project root installs for its browser
checks. It opens the app against this repository, walks through every tab, and
fails if the browser console reports anything.
