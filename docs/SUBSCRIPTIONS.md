# Using a subscription you already pay for

Many organisations have seats for Claude or GitHub Copilot and no API keys, and
will never get any. Those assistants each ship a command line tool that is
already signed in. The harness can drive that tool as an ordinary program, so
your existing seat does the work and no key is needed anywhere.

## What you need

One of these, installed and signed in:

| Tool | Command | Provider kind |
|---|---|---|
| Claude Code | `claude` | `claude-cli` |
| GitHub Copilot | `copilot` | `copilot-cli` |
| Anything else | yours | `assistant-cli` |

Check it works on its own first:

```bash
claude --version
```

If that prints a version, the harness can use it.

## Setting it up

Provider routes live in your own local config, `.harness/config.local.json`.
That file is never shared, and the harness only trusts it after you say so.

```json
{
  "provider": {
    "name": "claude-cli",
    "model": "claude-sonnet-4-5",
    "endpoint": "",
    "api_key_env": ""
  }
}
```

Then tell the harness the file is yours:

```bash
harness trust
```

It shows you the file, asks once, and records it. Edit the file again and it
goes back to untrusted on purpose, so a file that arrives from somewhere else
has no power until you look at it and agree.

Check it took:

```bash
harness doctor
harness qa generate --focus "the export command"
```

## Several assistants on one job

Give each one a named route, then point different agents at different routes.

```json
{
  "providers": {
    "claude": {"kind": "claude-cli", "model": "claude-sonnet-4-5", "endpoint": ""},
    "copilot": {"kind": "copilot-cli", "model": "gpt-5", "endpoint": ""},
    "local": {"kind": "ollama", "model": "qwen2.5-coder:7b", "endpoint": "http://127.0.0.1:11434"}
  }
}
```

In the **Workflow** tab, each agent box has a **Provider route** field. Set the
planner to `claude`, the coder to `copilot`, and the reviewer back to `claude`,
and one run uses all three. They pass work along the arrows, and they can write
notes to each other with the team message board. See
[TEAM_NOTES.md](TEAM_NOTES.md).

A reviewer on a different assistant from the coder is worth doing. Two models
that share no training run tend not to share the same blind spot.

## When your tool takes different arguments

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

`{model}` is replaced with the model for that request. If a request names no
model, that argument and the flag in front of it are both dropped.

Use `assistant-cli` for a tool the harness has never heard of. It has no
built-in arguments, so you supply all of them.

## How it works

The harness hands the whole prompt in on standard input and reads the answer
from what the tool prints. When a request needs structured output, the schema is
put in front of the tool as part of the prompt.

If the tool prints JSON, the answer is read from a named field. If it prints
plain text, the whole output is the answer, and a fenced code block is unwrapped
for you.

## What to expect

- **Cost is not reported.** Subscription work is recorded as
  `subscription-unpriced`, with no dollar figure, because there is no price per
  request to report.
- **Your plan's limits still apply.** Rate limits and quotas belong to the
  subscription, not to the harness.
- **No tool calls.** A command line assistant answers one prompt at a time. The
  harness asks for one answer and validates it afterwards, the same as it does
  for any provider.
- **It is slower** than an API call, because a whole program starts each time.

## If it does not work

| What you see | What it means |
|---|---|
| `claude is not on this machine` | The command is not on your PATH. Install it, then open a new terminal. |
| `did not answer when asked for its version` | The tool is there but broken, or it is waiting for a sign-in. Run it once by hand. |
| `refused the request` | The tool answered, and said no. The message after it is the tool's own words. |
| `ran past its N second limit` | Raise `provider.timeout_seconds`. A whole program starting is slower than a request. |
| `provider.endpoint must be empty` | A signed-in assistant has no address to call. Leave `endpoint` empty. |
| A trust error about the local config | Run `harness trust` after editing `.harness/config.local.json`. |

On Windows these tools are usually installed as a small `.CMD` wrapper. The
harness looks up the real path before running it, so the bare name working in
your terminal is enough.

## When a tool says you have no access and you plainly do

Two things worth knowing, both found the hard way on a machine where Claude was
working in one window and refused in another.

**More than one copy of the tool.** Claude Code can be on a machine twice: one
put there by npm, first on the path and never updated, and one the Claude
desktop app keeps up to date for itself. They do not answer the same way. The
old one refused without asking anybody - no request left the machine - and said
"your organization does not have access to Claude, please login again", which
sent somebody to their administrator about the wrong thing. The newer one asked,
and came back with the real answer: the organisation has Claude Code turned off
for subscription use. So the harness looks for the newest build it can find
rather than taking the first one on the path.

**Whether it asked anybody at all.** A refusal decided on your own machine and a
refusal from the service need two different things done about them, and the
harness says which it was. It reads the status the tool reports, because the
timing these tools print says nothing: this machine reports no time at the
service even for a refusal that really did come back from it.

## Which Copilot this is

The `copilot-cli` route drives **GitHub Copilot's** command line tool, which you
install with `npm install -g @github/copilot`.

**Microsoft 365 Copilot is a different product** and has no command line, so
there is nothing on the machine for the harness to drive. A seat for it does not
make this route work, and no amount of setting up will find it.
