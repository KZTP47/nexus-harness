# Models running on your own machine

The one assistant nobody has to approve. No seat, no key, no administrator, and
nothing you say to it leaves the building. On a locked-down company machine it is
often the only one that will ever work.

## What changed

The harness could always use one — the settings have taken an Ollama address for
as long as there have been settings. What it could not do was **find** one.
Somebody with Ollama running and a model pulled still had to know the port
number, know what the model was called, and write both into a file by hand. That
is a strange thing to ask for the one route that needs nobody's permission.

Now **Your team** goes and looks, and lists what it finds. One press to use one.

## What it looks for

| | Where | What it needs |
| --- | --- | --- |
| **Ollama** | `127.0.0.1:11434` | Get it from ollama.com, then `ollama pull qwen2.5-coder:7b` |
| **LM Studio** | `127.0.0.1:1234` | Get it from lmstudio.ai, load a model, press **Start Server** |

Anything else that answers the OpenAI shape — llama.cpp, vLLM, a machine of your
own — works too, by address. There is no finding those by guessing.

Both are asked for at the loopback address and nowhere else. A model server
reachable across the network is somebody else's machine, and this is about what
is on yours.

## When one is not running

It says so, and says what to do about it. "Nothing found" is a worse answer than
"here is what you could have and where to get it" — especially for the route that
needs nobody's permission.

## What gets written

```json
{
  "providers": {
    "qwen2-5-coder": {
      "kind": "ollama",
      "model": "qwen2.5-coder:7b",
      "endpoint": "http://127.0.0.1:11434",
      "api_key_env": ""
    }
  }
}
```

No key, because there is nothing to pay and nobody to prove yourself to.

Adding one **does not change** which assistant is used by default. Whatever was
already the default was somebody's decision, and adding one more assistant is not
the moment to overturn it.

The route name comes from the model's own name, with the version tag and the
punctuation taken off — `qwen2.5-coder:7b` becomes `qwen2-5-coder`, which is what
anybody would have called it anyway.
