# Reliable agent communication

Nexus treats an installed desktop app, a command-line sign-in, a usable
subscription request, and an active agent session as four separate facts. One
must never be used as proof of another.

## Delivery contract

Every cross-agent handoff has:

- a unique message ID and conversation thread ID;
- a shared-goal ID derived from the project and its current jobs;
- an explicit sender and receiver;
- a queued or acknowledged state; and
- an attempt count without raw provider diagnostics or account identity.

A message is acknowledged only after the receiving agent answers a turn that
included it. A timeout, provider refusal, app restart, or closed window leaves
it queued for the next run. Delivery is therefore at least once. Prompts should
be written so seeing the same message twice is harmless.

Communication lines remain capability boundaries. The human-readable shared
page can keep the whole audit trail, but an agent prompt receives only the
person's shared status, that agent's own entries, and entries from agents it is
currently allowed to hear from. Changing the project jobs creates a new goal
ID, so old mail cannot leak into a different task.

## Provider connection states

The UI reports these separately:

1. not installed;
2. installed, authentication unknown;
3. command-line sign-in required;
4. command-line sign-in confirmed; and
5. connected but degraded by the last real request.

Sign-in is always a user action. Nexus opens the provider's own interactive
command and never captures its credentials or account-status output. It never
silently substitutes an API key or a different paid provider.

## Design provenance

This design was informed by the durable inbox, acknowledgement, explicit reply
thread, capability, and shared-epic concepts in the MIT-licensed
[Traycer repository](https://github.com/traycerai/traycer). Nexus's Python
implementation is original and does not include Traycer source code.
