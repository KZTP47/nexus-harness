# The icon on your desktop

Double-click **`Install Nexus Harness.cmd`** at the top of this folder. Afterwards
there is a **Nexus Harness** on your desktop, and that is what you use from then
on. You never need a terminal to start it again.

## What it does

1. Looks for Python, under either of the two names a machine can have it under.
   If it is not there, it says so and where to get it, and changes nothing. That
   is the one part nobody can do for you.
2. Works out what the icon should open, best first.
3. Puts the icon on your desktop, and says which of the three you got.

Nothing outside your own account is touched, so it never asks to be an
administrator. Run it again any time - it writes over what it made before, which
is also how you point it at a folder you have moved, or move up to a better
option once you have one.

## The three things it can open, best first

**The desktop app, already installed.** The whole thing in a window of its own,
with the harness carried inside it. This is what you get if somebody has run the
installer in `desktop`, and it is the best of the three.

**The desktop app built here but not installed.** What you have after `npm
install` and `npm run build` in the `desktop` folder. Same window, started out of
this folder.

**The panel, started by Python out of this folder.** Needs nothing but Python, so
it always works, and it is what most people get. It opens the panel in your
browser rather than a window of its own, and the installer says so rather than
letting you wonder why it looks different.

All three end at the same panel. If you get the third and want a window of its
own, build the app once and run the installer again.

## The icon

Drawn in code, by `scripts/draw_the_icon.py`, and kept at
`desktop/nexus-harness.ico`: a dark rounded square, a cyan ring, a yellow dot.
Six sizes in one file, from 256 down to 16, so it stays itself on the desktop, in
the taskbar and in a file list.

The desktop app is built with it, so the app in your taskbar and the icon on your
desktop are the same picture. When the icon opens the app, the shortcut takes the
picture from the app rather than from this folder - so it keeps its icon even if
this folder moves.

Drawn rather than kept as a picture somebody once made, because a picture in a
repository is a thing nobody can open to change and nobody remembers how to
re-cut. This one is a few dozen lines and it says what it is.

## When it will not start

**"this settings file has not been trusted."** A settings file can name commands
to run, so nothing reads one until the person at the keyboard says the file is
theirs. Read it, then:

```bash
python scripts/harness.py trust
```

**The folder moved, and the icon opens the Python one.** That one has this
folder's path inside it. Run `Install Nexus Harness.cmd` again from wherever the
folder lives now.

## Not Windows

It works on macOS and Linux too, from a terminal rather than a double-click:

```bash
python scripts/put_it_on_your_desktop.py
```

On Linux that writes a desktop entry, on macOS a small file you can open. Both
name the same icon.
