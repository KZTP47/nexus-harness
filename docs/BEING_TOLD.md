# Being told when a run finishes

The timer runs your checks at two in the morning and leaves the report where you
can find it. That stops one step short: you still have to go and look. This is
the last step — a line in the place you already watch, the moment something
needs you.

---

## This part needs a key. There is no way around that

Everything else in the harness works on the machine in front of you. This does
not. Slack, Discord, Telegram, Teams and email all want something you have to go
and get, and the harness cannot make one for you:

| Where | What it wants | Where to get one |
| --- | --- | --- |
| Slack | an incoming webhook address | Settings, Manage apps, Incoming Webhooks |
| Discord | a webhook address | right-click the channel, Edit Channel, Integrations |
| Microsoft Teams | a webhook address | the channel's menu, Connectors, Incoming Webhook |
| Telegram | a bot token | message @BotFather and ask for a bot |
| Email | the password for the sending account, and the mail server | whoever runs your mail |
| Anywhere else | the address to post to | whatever you are sending to |

```bash
harness tell kinds
```

prints the same thing with the full instructions, and it says on the first line
that every one needs a key.

Nothing here is a trial, a free tier, or something the harness quietly arranges.
If you have no key, nothing is sent, and the harness says exactly which variable
is empty rather than failing in a way you have to guess about.

---

## The secret is never written down here

What gets saved is the **name of an environment variable** — `SLACK_WEBHOOK`,
say — and never what is in it. That is how the harness already handles the key
for a model, and it is what makes the file safe to commit.

If you paste the webhook address into the box asking for the variable name — and
people do, because it is the box next to the word "webhook" — it is refused, and
the refusal says why.

**Anything that decides where a secret goes is held the same way.** The mail
server is the clearest case. It is not a secret, but whoever sets it decides
where your mail password is sent — point it at a server somebody else runs and
the next run hands them your password. So it lives in a variable on this machine
too (`MAIL_SERVER`, written as `host:port`), never in a file that somebody could
change in a pull request.

What the file *does* hold, besides names, is who gets told and which account it
comes from. Neither of those can move a secret anywhere.

Set the variable the way your machine sets variables. On Windows:

```bash
setx SLACK_WEBHOOK "https://hooks.slack.com/services/..."
```

Then open a new terminal, because a variable set that way reaches only the ones
opened afterwards.

---

## Setting one up

In the panel: **Automations**, then **Tell me when it finishes**. Or:

```bash
harness tell add "Our room" slack --secret-in SLACK_WEBHOOK
```

For email, both variables:

```bash
harness tell add "By mail" email --secret-in MAIL_PASSWORD --server-in MAIL_SERVER --to them@example.com --sent-from us@example.com
```

```bash
harness tell list
```

says which are ready and which are waiting on a key. Fifty is the most you can
set up: every one of them is read whenever anything anywhere in the harness is
cleaned, and that is not a cost worth paying for a list nobody reads.

```bash
harness tell try "Our room"
```

sends one message so you can watch it arrive.

Also: `harness tell remove <name>`.

---

## When you get told

Only when a run does **not** pass. A run at two in the morning that went fine is
not news, and something that tells you every night is something you stop reading
by the end of the week.

The message is short: what ran, that it did not pass, and one line of why.

---

## What it will not do

- **Send your key anywhere.** The key is used to reach the place you chose, and
  nothing else. Telegram is the one where the token is part of the address it is
  asked at — that is how Telegram is built, and it is why nothing here ever
  writes an address into a message, a log, or a run's own record.
- **Send a secret your run printed.** Everything is cleaned first. The thing that
  failed may have printed a key on its way out, and a chat room is a very public
  place for that to land. The key being used right now is handed to the cleaner
  by name rather than looked for, so it is hidden even if the folder these are
  written in cannot be read at all.
- **Talk to anything over plain http, except this machine.** A webhook address
  read on the way is your run reports read on the way.
- **Follow a redirect.** A webhook that answers "go over there" is either broken
  or somebody moving your reports somewhere you did not agree to.
- **Stop a run.** Being unable to reach Slack is not a reason to lose the record
  of what your suite did.
- **Send a whole log.** Three thousand letters, and it says it was cut short.
- **Hold up your run.** A far end that never answers is given up on after
  twenty-five seconds. The waiting is done from outside, not by the socket,
  because a socket's own limit does not cover looking the name up — and a
  hostname pointing at a nameserver that never answers used to hang the whole
  nightly run, not just the message. Given up on is not the same as finished:
  Python cannot safely stop a thread waiting on a name, so it is left, and left
  ones are counted. Past eight, nothing new is started and it says so, rather
  than leaking one every night with nobody watching.
- **Send your mail password in the clear.** If the mail server will not turn the
  connection secure first, nothing is sent.
