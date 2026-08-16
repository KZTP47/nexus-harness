# Two subscriptions on one job

A worked example: Claude Max plans and reviews, GitHub Copilot writes the code,
and the two send notes to each other while they work. No API key anywhere —
both use seats your organisation already pays for.

Everything below was run start to finish before it was written down. Where a
message is quoted, that is the message the harness really prints.

---

## Before you start

You need both command line tools installed and signed in. They are separate
products from the subscriptions themselves, and each one signs in on its own.

| Seat | Tool to install | Check it works |
| --- | --- | --- |
| Claude Max | Claude Code | `claude --version` |
| GitHub Copilot | Copilot CLI | `copilot --version` |

Each command should print a version:

```text
2.1.101 (Claude Code)
```

If one says `command not found`, install that tool and open a new terminal.
Nothing below will work until both print a version, because the harness runs
these tools exactly as you would.

You do not need both. One seat works on its own; you just get one assistant
instead of two.

---

## Step 1 — Start the project

```bash
cd your-project
harness init --yes
```

This writes `.harness/config.json`, which is the shareable part. Your seats do
not go in that file.

## Step 2 — Name one route per seat

Routes go in `.harness/config.local.json`. That file is yours, is never shared,
and is not trusted until you say so.

```json
{
  "providers": {
    "claude":  {"kind": "claude-cli",  "model": "claude-sonnet-4-5", "endpoint": ""},
    "copilot": {"kind": "copilot-cli", "model": "gpt-5",             "endpoint": ""}
  },
  "provider": {
    "name": "claude-cli",
    "model": "claude-sonnet-4-5",
    "endpoint": "",
    "api_key_env": ""
  }
}
```

`providers` names the two seats. `provider` is the one used when nothing else
is said. `endpoint` stays empty: a signed-in tool has no address to call, and
the harness refuses a route that has both.

## Step 3 — Say the file is yours

Try `harness doctor` first, and it will stop you:

```text
error: providers.claude.kind requires trusted local, user, environment,
explicit, or command-line config
```

That is deliberate. A settings file can start a program, so one that arrives
from somewhere else has no power until a person looks at it and agrees:

```bash
harness trust
```

It prints the file, asks once, and remembers. Edit the file later and it goes
back to untrusted on purpose.

## Step 4 — Check it took

```bash
harness doctor
```

```text
OK   config: Config schema 1; 2 file layer(s)
OK   provider: Provider configuration is present: claude-cli
OK   capability_trust: Effective executable capabilities passed final
     provenance checks
```

## Step 5 — Give each agent a seat

```bash
harness ui
```

Open the **Workflow** tab. Each agent box has a **Provider route** field. Set:

| Agent | Route | Why |
| --- | --- | --- |
| Planner | `claude` | Reads the project and decides what to change |
| Coder | `copilot` | Writes the change |
| Final Reviewer | `claude` | Judges the result |

A reviewer on a different assistant from the coder is the point of doing this.
Two models that share no training tend not to share the same blind spot, so the
reviewer catches what the coder could not see.

Press **Save** and give the workflow a name, so you can come back to it.

## Step 6 — Let them talk to each other

The arrows already hand work along: planner to coder to reviewer. Notes cover
the other half — one agent telling another something it found.

In each agent box, tick **team.message** under capabilities. Then an agent can
write:

```json
{"to": "coder",
 "subject": "The parser caches by file name",
 "body": "Two files with the same name share a cache slot. Key on the full path."}
```

Rules worth knowing: an agent may only write to another agent in the same run,
never to itself, and never reads its own notes back. Credentials are stripped as
a note is written. Every note shows in the run log, so you can read the whole
conversation afterwards.

## Step 7 — Run it

Press **Start run** in the Workflow tab. That runs the workflow you have on
screen, which is the one with your two seats in it.

Tick **Plan without file changes** first if you want to see the plan before
anything is edited.

---

## What the finished setup looks like

```text
planner  -> route claude   = claude-cli claude-sonnet-4-5
coder    -> route copilot  = copilot-cli gpt-5
review   -> route claude   = claude-cli claude-sonnet-4-5

planner  may send and read notes
coder    may send and read notes
review   may send and read notes
```

---

## What to expect

**No cost figures.** Subscription work is recorded as `subscription-unpriced`.
There is no price per request to report, so the harness does not invent one.

**Your plan's limits still apply.** Rate limits and quotas belong to the
subscription. If a seat says no, the harness shows you the tool's own words.

**It is slower than an API.** A whole program starts for each request.

**One answer at a time.** These tools answer a prompt; they do not call tools
back. The harness asks for one answer and checks it afterwards.

---

## If something goes wrong

| What you see | What it means |
| --- | --- |
| `claude is not on this machine` | The tool is not on your PATH. Install it, open a new terminal. |
| `did not answer when asked for its version` | The tool is there but broken, or waiting for a sign-in. Run it once by hand. |
| `refused the request` | The tool answered and said no. The words after it are the tool's own. |
| `ran past its N second limit` | Raise `provider.timeout_seconds`. Starting a program is slower than a request. |
| `provider.endpoint must be empty` | A signed-in tool has no address to call. Leave `endpoint` empty. |
| A trust error after editing the file | Run `harness trust` again. Editing makes it untrusted on purpose. |

On Windows these tools usually install as a small `.CMD` wrapper. The harness
finds it; you do not need the full path.

## If your tool takes different arguments

These tools change. Rather than waiting for a new harness, set the arguments
yourself:

```json
{
  "providers": {
    "copilot": {
      "kind": "copilot-cli",
      "model": "gpt-5",
      "endpoint": "",
      "command": ["copilot"],
      "arguments": ["-p", "--no-color", "--model", "{model}"]
    }
  }
}
```

`{model}` becomes the model for that request. If a request names no model, that
argument and the flag in front of it are both dropped.

For a tool the harness has never heard of, use `"kind": "assistant-cli"` and
supply all the arguments yourself.

---

See also [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) for the provider details and
[TEAM_NOTES.md](TEAM_NOTES.md) for the full rules on notes.
