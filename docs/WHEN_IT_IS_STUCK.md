# When it is stuck

A run used to find out it was getting nowhere by running out. The loop stops
after a set number of tool calls, which is a stop and not a warning: by then the
budget is gone, and nothing was said while there was still something to do about
it.

Two things fix that. The harness says something while there is still time, and
the agent says what it is doing so you can see it.

---

## A word in its ear

A tool result can come back with one extra line on it — a `notice`. That is the
harness talking, not your project. There are two of them, and they only turn up
when they are true.

**"You have now asked read_file the same thing 3 times and got the same answer
back."** Three, because one repeat by accident says nothing and three is a
pattern. It comes with what to do instead: ask something different, use a
different tool, or answer with what you have and say plainly what is missing.

Counted per agent, not across the whole run. Two agents asking the same sensible
question is not one agent going round in circles, and adding them up told the
second one something about itself that never happened. The count is kept with
the rest of the budget, so a run picked up after an approval or a restart has
not forgotten.

**"2 tool calls left out of 12."** Said with three calls to go, which is the
point where "carry on looking" stops being a plan and putting an answer together
starts being one.

Both are said to the agent, and both are shown to you.

### Where it does not go

Not inside what the tool said. Put there, the same question came back different
the second time, and the word — which is about this moment — was written into
the copy kept for a restart to replay later. It rides on the envelope beside the
result, so the tool's own answer is the same every time it is asked.

### It is a warning, not a reprieve

The hard stop still stops. A notice buys nobody an extra call.

---

## What it is doing

The agent keeps a short list — read the file, write the fix, run the tests — and
says how each one is going: **waiting**, **going**, **done** or **dropped**. It
shows up under **What it is doing** in the Workflow view, and it stays there
after the run ends.

It sends the whole list every time, so changing its mind replaces the list
rather than adding to it. Twenty steps is the most, and a step longer than two
hundred letters is refused rather than quietly cut short — a list nobody can
read at a glance is not what this is for, and half a step with nobody told is
worse than being asked to say it shorter.

This is the one thing a person watching can actually read. Everything else is
tool calls.

---

## What it costs

One tool call each time the list is sent, out of the same budget as everything
else. That is the trade: the agent spends a call on saying what it is doing, and
you can see whether it is worth watching.

The two notices cost nothing. They ride on results that were coming anyway.

---

## Where the numbers live

```text
workflow.max_tool_calls
```

Everything else is fixed in the code, because they are not really settings: three
repeats and three calls left are where the warnings are worth having, and a
different number in a settings file would only be a different number.
