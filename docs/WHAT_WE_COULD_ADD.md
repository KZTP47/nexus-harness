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

### 2. Let an agent call for help part way through — **built**

Done. `helper.py` asks one assistant one question and hands back the answer,
through the same model routes as everything else. It cannot read files, run
anything, or change anything. There is an **Ask an assistant** step in
pipelines, so a run can put a question mid-way and keep the answer with the
rest of what happened. See [PIPELINES.md](PIPELINES.md).

### 3. Real code navigation instead of guessing — **built**

Done. `navigate.py` speaks to a language server — the same tool your editor
asks — and answers where is it, what uses it, and what is it. Every answer says
whether it is exact or a guess, which is the part that decides what you do
next. There is a **Look it up** tab and a `harness look-up` command. See
[LOOK_IT_UP.md](LOOK_IT_UP.md).

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
