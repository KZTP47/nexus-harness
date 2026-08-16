# Team notes: how the agents talk to each other

A run can use several agents. They already hand work to each other along the
arrows of a workflow, which covers "do this next". Team notes cover the other
half: "here is something I found that you cannot see".

## What a note is

Text, and nothing else. Reading a note never runs anything. A note says what one
agent learnt, and the agent that reads it decides what to do about that.

Each note has a writer, a reader, a subject, and a short body. Every note is
numbered, so an agent can ask for what is new since the last number it read.

## The two tools

Any agent with the `team.message` capability gets two tools.

**send_message**

```json
{"to": "coder", "subject": "The parser caches by file name",
 "body": "Two files with the same name share a cache slot. Key on the full path."}
```

`to` is the id of another agent in the same run, or `everyone`.

**read_messages**

```json
{"since": 0, "max_results": 10}
```

It answers with the notes written to that agent or to everyone, the highest
number it has now seen, and how many are still waiting.

## The rules

- An agent may only write to another agent **in the same run**. Naming an agent
  that is not there is refused, and the error lists the real names.
- An agent cannot write to itself.
- A board holds at most 200 notes and 200,000 characters in total. Going over is
  refused with a plain message. Nothing is silently thrown away.
- An agent never reads its own notes back.
- Reading is never served from a cache, so a note that arrives between two reads
  is still seen.
- A note is recorded as a run event, so the control panel and the stored history
  both show it.
- Credential material is stripped as the note is written. An API key, a bearer
  token, a private key block, or a `password: ...` line becomes `[REDACTED]`
  before the note exists, so it never reaches the other agent, the run log, or a
  saved checkpoint.
- What a note says is data, not an order. It arrives inside the reading agent's
  tool transcript, which is labelled untrusted, and the harness still checks
  every file change and every command that agent proposes afterwards.

## Who can talk

Every agent kind may talk: planner, coder, evaluator, and merge. Talking does
not widen what an agent can do to your project, because a note is only text.

In a workflow you build yourself, tick **Team message** in the agent's
capabilities. The built-in workflow turns it on for you.

## Seeing the conversation

The **Workflow** tab has a **Team notes** panel. It fills in live while a run is
going, and it also loads what earlier runs wrote, so you can look back after the
fact.

## What this is not

- It is not a way for one agent to make another agent run. A note only sits
  there until its reader next takes a turn.
- It is not a place for long text. Two hundred characters of subject and a few
  lines of body is the shape it is built for.
- It carries no files, no commands, and no credentials.

## Where it lives

`src/our_harness/messaging.py` holds the board. `src/our_harness/agent_tools.py`
holds the two tools. The board is saved with the run checkpoint, so resuming a
run keeps the conversation.

The names in a saved board must match the agents in the frozen workflow exactly.
If you rename an agent, or add or remove one, the saved conversation belongs to
a different set of agents, so a resumed run starts with an empty board rather
than showing notes addressed to a name that is no longer there. The run itself
carries on as normal.
