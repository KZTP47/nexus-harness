# Browser chat latency

The reply duration is an end-to-end provider-call duration, including local
queueing, browser preparation, submission acknowledgement and reply capture.
It is not a measurement of model inference alone. Older turns have only this
total; their missing phase timings cannot be reconstructed from the total.

Nexus sends a fresh random transport marker with every browser turn. Both
submission paths acknowledge that marker only in a newly rendered or replaced
provider user bubble. This avoids an eight-second acknowledgement timeout when
a provider collapses or reformats the rest of the prompt. Draft validation still
requires the full prompt before Send. A marker in the composer, another marker,
or an unchanged old turn is not acknowledgement. Ambiguous sends are not resent.

Reply observation uses 250 ms intervals with a shorter final settle check.
The full 1.8-second quiet-text window and at least three matching observations
remain required. A visible Stop control always prevents completion, even if text
has been unchanged for a long time. A missing/remounted reply resets stability.
Idle bridge polling and provider model/context settings are unchanged.

Successful relay receipts carry numeric version-1 timing observations. Direct
chat transcripts preserve them through their existing authenticated journal and
show them in Reply details (or the expanded transcript metadata):

- Queue: time from Python admission until the desktop claims the request.
- Browser preparation: opening/loading/binding the conversation and attachments.
- Send acknowledgement: preparing and submitting input until the handshake returns.
- First visible reply: time after that handshake until a causally matched answer
  is first observed; text may still be incomplete then.
- Reply wait and capture: the full wait after the handshake until capture ends.

First visible reply is a milestone within Reply wait and capture; do not add
them together. Neither measurement isolates remote inference time: generation
can overlap the submission handshake. Additional numeric diagnostics record
browser total duration and time since the last observed text change. These are
historical observations, not cached configuration or completion evidence.
Unknown timing versions and arbitrary provider fields are omitted. Older turns
continue to work without phase metadata. `ask_once` also returns timing metadata
to its caller; collaboration projections need not display it.

## Controlled before/after evidence

Identical fake-clock fixtures were run against the pre-change `web-chats.js`
and the patched source:

| Fixture | Before | After |
| --- | --- | --- |
| Collapsed marked user bubble acknowledgement | 8,000 ms, unknown | 100 ms, acknowledged |
| Already-ready answer collection | 2,700 ms | 2,050 ms |
| Required unchanged-text interval | 1,800 ms | 1,800 ms |

These are local transport measurements, not promises about a live provider or
additive savings for every request. If generation takes longer than the old
acknowledgement timeout, those phases overlap. Tests cover wrong/stale markers,
draft-only markers, streaming pauses, reply remounts, bounded metadata and a
real broker-to-chat receipt persisted and reloaded in a temporary project.

Run the focused checks with:

```text
node --test desktop/web-chat-latency.test.js desktop/web-chats.test.js desktop/renderer-web-chats.test.js
python -m unittest tests.test_relay_timing tests.test_web_chats tests.test_web_chat_renderer_reliability tests.test_talking_to_them tests.test_swarm_chats -q
```
