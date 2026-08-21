# Gemini and Codex, on the subscription you already have

Both of these were nearly here already and neither could be used.

**Gemini** has a command line and signs in with a Google account, exactly the way
Claude and Copilot do. It just had no place in the harness at all.

**Codex** had a whole provider, older and better tested than most of this, and
the app could not find it. Its desktop app keeps it in a folder of its own and
nothing ever puts it on the path, so the app said Codex was not on this machine
while it sat there signed in — and sent people off installing what they already
had.

## Gemini

Install the command line and sign in with the Google account you already use:

```bash
npm install -g @google/gemini-cli
```

Then run `gemini` once, on its own, and let it sign you in. Open **Your team** in
the app and press **Set them up**.

**If yours is a work account, Google will refuse everything until you name a
Cloud project.** Their own message for this is a link and a shrug. The harness
says what to do instead:

> Google will not answer this account until it is told which Cloud project to
> bill the work to. It is not a sign-in problem and signing in again will not
> help. Put the project id in this route's settings as `google_project`.

```json
{
  "providers": {
    "gemini": {
      "kind": "gemini-cli",
      "model": "",
      "google_project": "your-project-id"
    }
  }
}
```

A personal Google account needs none of that. Leave `model` empty to use
whatever Gemini picks.

## Codex

If you have the Codex desktop app, you already have it — the harness looks
inside the app's own folder now. Otherwise:

```bash
npm install -g @openai/codex
```

Sign in with your ChatGPT account. Open **Your team** and press **Set them up**.
A copy you put on the path yourself always wins over the one in the desktop app,
because somebody who did that meant that one.

## Using an API key instead

Every one of these can take a key, for anyone who has one and would rather spend
that than a subscription seat. Name the environment variable the key lives in,
and the harness hands it to the tool:

```json
{
  "providers": {
    "gemini": { "kind": "gemini-cli", "model": "", "api_key_env": "MY_GEMINI_KEY" }
  }
}
```

| Route | The variable the tool itself reads |
| --- | --- |
| `claude-cli` | `ANTHROPIC_API_KEY` |
| `copilot-cli` | `GH_TOKEN` |
| `codex-cli` | `OPENAI_API_KEY` |
| `gemini-cli` | `GEMINI_API_KEY` |
| `m365-copilot` | none — Microsoft allows no key at all |

You name where the key comes from; the harness puts it where the tool looks. The
two do not have to match, so several routes can hold different keys.

**A key is only ever handed over when a route asks for one by name.** Everything
else is stripped before the tool runs. A key that arrives because it happened to
be set on the machine is a key nobody decided to spend, and the whole point of a
subscription route is that it spends nothing.

And a route that names a key which is not set says so, rather than quietly
falling back to the subscription. A route doing something other than what it says
on it is worse than a route that stops.

## The ones that were always key-only

`openai`, `anthropic`, `gemini` (the API, not the command line) and
`openai-compatible` have always needed `api_key_env` and still do. Nothing
changed for them.
