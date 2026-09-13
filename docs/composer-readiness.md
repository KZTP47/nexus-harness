# Chat input readiness

Typing must be available as soon as a chat composer is visible. A saved-chat
registry read/write, transcript read, provider check, running goal, or attachment
read must not disable the textarea. These conditions gate sending separately.

The old identity-changing gate disabled both textareas until the server answered.
Moving transcript loading into the background did not remove the dependency on
the preceding registry request. Slow startup and new-chat creation could therefore
still leave the visible editor unusable.

`chatComposerKey` separates local draft identity from dispatch identity. Opening a
new chat synchronously preserves the previous draft and selects a unique temporary
draft before issuing the creation request. Only a valid response confirming a
live chat can transfer that draft into the saved chat. Failure keeps the temporary
draft editable and blocks sending; explicitly retrying the same pair restores it.
Selecting another saved chat preserves its independent draft. An obsolete
response cannot mutate a replacement chat card.

These draft identities are renderer-session state, like the existing draft maps;
this change does not introduce a new durable journal or promise crash recovery
for unsent text. Network completion must also never steal focus from the user's
current control. Initial focus belongs before the request, not after history.

Verification:

- `node --test` in `desktop` includes real keyboard input with unresolved requests,
  both composer sizes, actual New chat/history controls, redraw and caret
  preservation, failed creation/retry, invalid responses, and obsolete responses.
- `node desktop/composer-readiness.smoke.js` runs the same checks in the packaged
  Electron application in isolated temporary profiles, on startup, after a process
  restart, and in a different project. No provider prompt is sent.
- Setting `NEXUS_COMPOSER_NEGATIVE_CONTROL=1` for
  `node --test desktop/composer-readiness.test.js` reinstates the former disabled
  condition in the test renderer and must fail the first-keyboard-input check.

The input check has a one-second budget for clicking and typing a complete short
draft while the request is still unresolved. It measures editor responsiveness,
not total application launch time or provider response latency.
