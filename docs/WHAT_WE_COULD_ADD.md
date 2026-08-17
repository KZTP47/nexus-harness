# What we could add

Measured against two harnesses worth measuring against: **hermes-agent** from
Nous Research, and **deepseek-harness** from DeepSeek. Both were read as code,
not as adverts.

Everything below is something they have and we do not. Anything they have that
we already have is left out, and so is anything that needs an API key, because
we have seats and no keys.

---

## Worth having

### 1. Run things on a timer, with nobody watching

**What it is.** Tell the harness "run the whole suite every night at two, and
leave the report where I can find it in the morning." It does, without anybody
starting it.

**Where they do it.** Hermes has a real scheduler built in. DeepSeek has a
smaller version — a reminder that only fires while a session is open — which is
not the same thing.

**What it would take.** A small module beside `resident.py`. The background job
runner and its job store already exist; this is mostly wiring a clock to them.

**Worth it: high.** Our own notes already list this as missing, and it needs
nothing but this machine's clock.

### 2. Let an agent call for help part way through

**What it is.** While working, an agent can say "somebody go and check this one
thing" and start a short-lived helper using a seat we already pay for, instead
of only the fixed planner, writer and reviewer wired up beforehand.

**Where they do it.** DeepSeek has this properly: one agent starts a real child
agent, including a real Claude Code child and a real Codex child, and gets a
report back.

**What it would take.** A new tool in `agent_tools.py`, using the provider
routes we already have for the command line tools. Today the fan-out is fixed
boxes in a graph, not something a model can ask for.

**Worth it: high.** It is new capability on top of seats we already pay for.

### 3. Real code navigation instead of guessing

**What it is.** Instead of guessing where a function is defined by matching
text, ask the tool built for that language and get the exact answer — the same
thing that powers "jump to definition" in an editor.

**Where they do it.** DeepSeek gives the model four fixed moves: go to
definition, find references, go to implementation, and hover.

**What it would take.** Changes in `indexer.py` and `context.py`, which today
fall back to matching text for every language except Python. It needs a
language server installed on the machine, which is free and needs no account.

**Worth it: high** for anything that is not Python. Our own architecture notes
already admit this gap.

### 4. Being an agent inside somebody else's editor

**What it is.** A standard way for editors to talk to an agent, so the harness
could run inside a tool somebody already has open rather than only in its own
panel.

**Where they do it.** DeepSeek exposes the whole harness this way; Hermes has
the other half of the same conversation.

**What it would take.** A new module beside `mcp.py`. We already speak the
client half of a very similar protocol, so this is the mirror image of code we
have.

**Worth it: medium.** Real reach, but new surface to keep working, and our own
panel already does the job for most people.

### 5. A nudge before the hard stop

**What it is.** Today the loop stops after a fixed number of tries. This adds a
friendly warning earlier — "you have called the same tool with the same input
three times" — so a stuck agent gets a chance to notice before its whole budget
is gone.

**Where they do it.** DeepSeek has a guard that watches for exactly this.

**What it would take.** A few lines inside the loop in `agent_tools.py`.

**Worth it: medium.** Cheap, and it saves wasted turns.

### 6. A running to-do list you can watch

**What it is.** A plain list — read the file, write the fix, run the tests —
kept up to date as the work goes, shown to the person watching.

**Where they do it.** DeepSeek keeps one list per session, shown to both the
model and the person.

**What it would take.** A small addition to `agent_tools.py` and a panel in the
control panel.

**Worth it: low to medium.** Our workflow board already lights each step as it
runs, so most of this is there.

---

## Left out on purpose

- **Chat platform bots** (Telegram, Discord, Slack, WhatsApp, Signal), voice,
  and image or video generation. Every one needs an outside account or bot
  token that our seats do not cover.
- **Squeezing finished runs into training data.** Hermes does this to train
  future models. We do not train models.
- **Cloud sandboxes** that need their own account and key.
- **Their goal tracker and their context shrinker.** We already have the
  requirement list the reviewer checks against, and a bounded context builder.
  Two ways of doing one thing is worse than one.
