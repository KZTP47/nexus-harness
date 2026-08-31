# Multi-vendor reliability evidence

This document records the source audit behind Nexus's packaged multi-vendor
acceptance gate. It is provenance, not an assertion that an upstream product
solves Nexus's delivery problem.

## Pinned upstream revisions

| Project | Audited revision | Useful mechanism | Material limitation |
| --- | --- | --- | --- |
| Microsoft AutoGen / Studio | [`027ecf0a379bcc1d09956d46d12d44a3ad9cee14`](https://github.com/microsoft/autogen/tree/027ecf0a379bcc1d09956d46d12d44a3ad9cee14) | Canonical messages, explicit model capabilities, replay provider, graph admission | Studio is a research prototype; disconnect cancels a run; no reconnect/replay UI contract |
| Dify | [`bf64390d7cd4818a84346f6a193611f60ad9699a`](https://github.com/langgenius/dify/tree/bf64390d7cd4818a84346f6a193611f60ad9699a) | Atomic event/status transitions, cursor replay, durable cancellation intent | Declared agent `idempotency_key` is not deduplicated; active agent actors cannot recover from abrupt process loss |
| Langflow | [`da3d5050e83e55f885e0627245676933a867d06b`](https://github.com/langflow-ai/langflow/tree/da3d5050e83e55f885e0627245676933a867d06b) | Typed checkpoints, atomic resume claims, persist-before-publish event cursors, deterministic Playwright provider | A graph-layer exception is fatal and there is no typed per-participant partial outcome |
| Flowise | [`9291856d1ea4a4ceea9f8fef8ce14f4f6c81e8eb`](https://github.com/FlowiseAI/Flowise/tree/9291856d1ea4a4ceea9f8fef8ce14f4f6c81e8eb) | Heterogeneous model selection and approachable execution-tree UI | Agentflow v2 is sequential, live events are not replayable, and the checked-in Cypress specs are disabled |
| AgentVerse | [`f90c4bd9680fdd3bcff8c52c9170911a59b23478`](https://github.com/OpenBMB/AgentVerse/tree/f90c4bd9680fdd3bcff8c52c9170911a59b23478) | Separate speaking-order, visibility, selection, and memory-update policies | No durable checkpoint/resume, no real test suite, only an OpenAI-compatible model adapter, and the GUI says only one person can speak |

The audits used clean shallow checkouts outside this repository. No upstream
source file was copied into Nexus. The small semantics below were independently
implemented against Nexus's existing provider, effect-journal, and authority
contracts.

## Adopted reliability rules

1. Persist the dispatch/effect boundary before contacting a provider.
2. Derive visible state from durable engine state, not renderer inference.
3. Bind an idempotent start identity to the complete admitted intent. Reusing
   the identity with a different project, team, objective, criteria, route, or
   policy fails closed.
4. Treat each expected participant as an accountable terminal outcome. A run
   with one missing reply is partial; a run with no replies is none; neither is
   painted as successful.
5. Preserve known successful replies when another participant fails.
6. Never automatically resend a provider effect whose delivery is unknown.
7. Save checkpoints and monotonic events before projecting live progress.
8. Reject resume when the project authority or non-secret provider contract
   fingerprint has changed.
9. Keep human decisions durable, revision-bound, and one-shot.
10. Prove the installed artifact through user-visible controls and real adapter
    boundaries. API calls may arrange an isolated fixture, but may not replace
    the UI action under test.

## Packaged acceptance contract

Run from `desktop` after building:

```text
npm run e2e:multi-vendor
```

`multi-vendor.e2e.js` launches `Nexus Harness.exe` with an arbitrary temporary
project and fresh local state. It configures loopback OpenAI, Anthropic, and
Gemini endpoints, then performs these actions through Playwright locators:

- add and configure three agents;
- add a project;
- compose a long-horizon goal and require every selected agent;
- verify three distinct durable provider contributions;
- create a connected two-agent chat;
- verify visible `2 of 2`, `1 of 2`, and `0 of 2` outcomes;
- restart Electron and its Python engine;
- verify the partial outcome is still present in the UI;
- fail on renderer exceptions, browser console errors, or unexpected local 5xx
  responses.

The test is intentionally forbidden from using `page.evaluate`, injecting IPC,
replacing the renderer's `request` function, or intercepting product requests.
Its loopback service is a deterministic provider-boundary fixture; requests
still traverse the packaged main process, preload, renderer, Python HTTP server,
provider registry, three production adapters, journals, transcripts, and UI.

## License provenance

- AutoGen code packages use MIT; its documentation/non-code material uses CC BY 4.0.
- AgentVerse and the inspected non-enterprise Flowise source use Apache-2.0.
- Langflow uses MIT.
- Dify uses a modified Apache-2.0 license with additional conditions.
- Flowise `packages/server/src/enterprise/**` is commercially licensed and was
  not used.

Because Nexus independently implemented semantics rather than importing source,
no third-party code notice was added for this change.
