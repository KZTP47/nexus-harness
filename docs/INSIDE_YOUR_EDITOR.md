# Inside your editor

The panel is a good place to watch a run. It is a bad place to be when you are in
the middle of writing code: you are in your editor, and the answer you want —
where is this used, what do we already know about this, run my checks — is one
window away.

Editors have agreed on one way to talk to a tool like this. The harness already
speaks the asking half of it, so it could call your tools. Now it speaks the
other half, so your editor can call the harness.

---

## Setting it up

```bash
harness editor setup
```

That prints exactly what to paste, and where to paste it. It does not edit your
editor's settings — those are yours, and a tool that changes them behind your
back is a tool you cannot trust with anything else.

| Editor | Where it goes |
| --- | --- |
| VS Code | `.vscode/mcp.json` in this project, or the mcp part of your settings |
| Cursor | `.cursor/mcp.json` in this project |
| Claude Desktop | the `claude_desktop_config.json` its settings point at |
| Anything else | give it the command and the arguments the setup printed |

Then restart the editor. It starts the harness itself and talks to it down the
pipe between them — nothing listens on a port, nothing is reachable from anywhere
else, and it stops when you close the editor.

---

## What your editor can then ask

**Where is it, what uses it, what is it.** The same three questions the *Look it
up* tab asks. A real language server answers where this machine has one for that
kind of file, and the files are read where it does not — and the answer always
says which, because that is what decides whether you trust it.

**What this project already knows.** The notes and the runs the harness has kept.
Worth asking before working something out that somebody here has worked out
before.

**Which automations are saved here.** By name.

---

## And, if you say so

```bash
harness editor setup --let-it-run-things
```

Two more: **run one of those automations**, and **run the checks**. Both run real
commands on your machine.

Be clear about what those two are. An automation does whatever the person who
drew it put in it. Most checks only read — they open a page, or look through the
files — but a check can also be a plain command somebody wrote down, and a suite
is only as safe as the least careful check in it. So both can write files into
your project, delete them, and ask an assistant something over the network.
"Runs commands" is the floor, not the ceiling, and if you did not write them
yourself, read them before you turn this on.

They are off unless you ask for them, and off means not offered at all rather
than offered and then refused. An editor is a place where a tool gets called
without anybody deciding to call it — a model reads your file, decides the checks
would help, and runs them. That is fine when you meant it and a surprise when you
did not, so it is your sentence to write, not ours.

If your editor asks for one anyway, it is told plainly that it is turned off and
who turns it on.

---

## What it will not do

- **Write to your project, or reach out to anything, on its own.** The three
  reading tools only read, and they are all you get unless you turn the other
  two on. What those two do afterwards is whatever your automations and your
  checks do — and both of them say they may destroy something, in the words this
  protocol uses for it, so an editor that asks before a destructive thing knows
  to ask. They say it about the whole tool, because one careless check among
  fifty careful ones is still one careless check.
- **Edit your editor's settings.** It prints them. You paste them.
- **Send the conversation anywhere.** It is the pipe between two programs on
  your machine, and nothing else.
- **Hand back a whole file.** One answer is cut short past sixty thousand
  letters, and says it was. An editor puts this in front of a model, and a whole
  file of it is a whole file of somebody's budget.

---

## When something goes wrong

Nothing is printed on that pipe that is not an answer. One stray line and the
editor stops understanding anything, so if you are adding to this, keep it that
way.

If the editor shows the harness as failed to start, run the command from the
setup by hand in a terminal. It will sit there waiting for a message, which is
right — press Ctrl-C. If it says something else instead, that is your answer.
