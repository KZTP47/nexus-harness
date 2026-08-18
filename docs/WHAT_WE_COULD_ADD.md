# What we could add

Measured against two harnesses worth measuring against: **hermes-agent** from
Nous Research, and **deepseek-harness** from DeepSeek. Both were read as code,
not as adverts.

Everything below is something they have and we do not. Anything they have that
we already have is left out, and so is anything that needs an API key, because
we have seats and no keys.

---

## Worth having

### 1. Run things on a timer, with nobody watching — **built**

Done. `timer.py` runs your automations every hour, every day, every weekday or
once a week. The harness does not stay running: your machine's own scheduler is
asked to run `harness timer run` every so often, which is what makes this
survive closing the window and restarting. It will not run two at once, it comes
back from a week off with one run rather than a hundred, and it says so when an
automation stops to ask a person. See [ON_A_TIMER.md](ON_A_TIMER.md).

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

### 4. Being an agent inside somebody else's editor — **built**

Done. `editor.py` answers an editor down the pipe between them. It offers three
things that only read - where is it, what this project already knows, which
automations are saved - and two that run commands, which are not offered at all
unless you started it with `--let-it-run-things`. `harness editor setup` prints
what to paste and where; it never edits your editor's settings. See
[INSIDE_YOUR_EDITOR.md](INSIDE_YOUR_EDITOR.md).

### 5. A nudge before the hard stop — **built**

Done. A tool result comes back with a `notice` on it when the same question has
come back with the same answer three times, or when three calls are left. Both
are said to the agent and shown to the person watching. The hard stop still
stops; a warning buys nobody an extra call. See
[WHEN_IT_IS_STUCK.md](WHEN_IT_IS_STUCK.md).

### 6. A running to-do list you can watch — **built**

Done. `keep_a_list` lets an agent say what it is doing - read the file, write
the fix, run the tests - and whether each one is waiting, going, done or
dropped. It shows under **What it is doing** in the Workflow view and stays
there after the run. The board lights the steps *we* gave the run; this is the
steps the agent gave itself, which is the half nobody could see. See
[WHEN_IT_IS_STUCK.md](WHEN_IT_IS_STUCK.md).

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
