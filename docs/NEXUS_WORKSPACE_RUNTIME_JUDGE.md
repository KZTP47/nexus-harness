# Independent release-judge protocol

Judges receive the user requirements, the defect register, acceptance matrix,
current diff, and test artifacts. They do not receive an instruction to approve.
They must inspect current code and run fresh counterexamples.

Use at least three independent gates:

1. **State/engine judge:** run identity, fencing, cross-process coordination,
   restart, cancellation, timers, mutation saga, and outcome semantics.
2. **Evidence/security judge:** redaction, immutable definitions, transcripts,
   projections, authority/provenance, deterministic receipts, and independent
   provider bindings.
3. **Human-interface judge:** wrong-run attribution, reconnect truthfulness,
   keyboard/focus/screen-reader behavior, narrow/zoom layouts, and progressive
   disclosure.
4. **Migration/trust-boundary judge:** malicious workers, store integrity,
   atomic submission, lease takeover, executable manifests, and legacy cutover
   and recovery without dual ownership or data loss.
5. **Conversation/resource judge:** conversation-store ownership and publication,
   recipient-specific delivery, physical-provider scheduling, and executable
   mutation-saga compensation under crashes and external edits.

Each finding must include severity, invariant, current file/line or runtime
trace, deterministic reproduction, observed/expected result, and required gate.
`PASS` requires:

- no reproducible P0/P1 scoped defect;
- every acceptance category has current artifacts;
- no test passes merely by checking for source strings;
- no completion claim relies on provider self-report;
- no waived failure broadens authority, loses history, exposes a secret, or
  permits an ambiguous destructive side effect.

Any failed judge returns the work to implementation. After fixes, all judges run
again on the complete integrated tree rather than only the changed test.
