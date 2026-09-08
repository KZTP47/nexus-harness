# t3code provenance and adoption scope

Nexus evaluated [KZTP47/t3code][upstream] at the pinned commit
[`eb115063634c416c6362cc407f8572cb0c136ddf`][commit]. The changes below adapt
selected implementations and state-management patterns to Nexus's Python
engine and existing browser UI. They do not import the t3code application,
replace Nexus with its Effect/React architecture, or adopt its project
instructions as Nexus policy.

“Adapted” means the corresponding behavior is present in this change.
“Evaluated” means the source was inspected but that implementation was not
adopted. The original symbols and commit links identify the provenance;
Nexus-specific extensions are called out separately.

## Implemented adaptations

| Upstream source and symbols | Nexus implementation and differences |
| --- | --- |
| [pendingUserInput.ts][up-input]: `resolvePendingUserInputAnswer`, `buildPendingUserInputAnswers`; [orchestration.ts][up-contract]: `UserInputAttachmentAnswerPayload` | [user_questions.py](../src/our_harness/user_questions.py) `answer_record` and the goal answer forms in [app.js](../src/our_harness/ui/app.js) preserve question IDs, selected options, and typed answers separately from assistant-authored question wording. Nexus validates against the saved questions and records the chosen audience. This does not add question attachments. |
| [pendingRequests.ts][up-requests]: `derivePendingRequests` and its closed-request sets | [goal_decisions.py](../src/our_harness/goal_decisions.py) `submission`, `receipted`, `repeated`, `resolved`, `page`, and `prompt` adapt exact-request terminal receipts. Nexus adds authenticated goal binding, an exact submission digest, recipient-scoped durable answers, normalized repeated-question checks tied to observed progress, and a `read_user_decisions` context operation. These additions are Nexus logic, not an upstream semantic-answer ledger. |
| [ProviderCommandReactor.ts][up-reactor]: `ensureSessionForThread`; [Utils.ts][up-workspace]: `resolveThreadWorkspaceCwd` | [models.py](../src/our_harness/models.py) `ProviderWorkspaceContext` and [input_context.py](../src/our_harness/providers/input_context.py) `workspace_instructions` carry engine-owned selected-project and execution-copy identity. Provider transport directories remain separate. The subscription adapter uses an isolated transport directory for workspace-bound requests without an explicit working directory. Nexus does not import t3code's persistent session directory or worktree recreation. |
| [ClaudeAdapter.ts][up-claude]: `buildUserMessage`, `buildClaudeImageContentBlock`, `buildUserMessageEffect`, `isOverloadedResult` | [claude_input.py](../src/our_harness/providers/claude_input.py) `build_user_message` supplies native base64 image blocks before the final text block. `terminal_result` requires a terminal stream result; [subscription_cli.py](../src/our_harness/providers/subscription_cli.py) also rejects reported service failures. Nexus supplies image bytes without enabling native file-read tools for this purpose. |
| [CodexAdapter.ts][up-codex]: `resolveAttachment`; [CodexSessionRuntime.ts][up-codex-runtime]: `buildTurnStartParams` | [image_inputs.py](../src/our_harness/providers/image_inputs.py) `read_image_inputs` bounds and validates original attachment bytes, including a digest when supplied. [codex_cli.py](../src/our_harness/providers/codex_cli.py) materializes those bytes in the private transport directory for native `--image` operands. Unlike upstream's data-URI input, this uses Nexus's existing CLI protocol. A result file alone is insufficient without `turn.completed`. |
| [imageDimensions.ts][up-dimensions]: `readImageDimensions`, PNG/GIF/WebP/JPEG readers, `exifOrientationSwapsAxes` | [images.py](../src/our_harness/images.py) `attachment_image_metadata` and `_attachment_exif_rotates` adapt the header parser to Python, including EXIF orientation and three WebP forms. Nexus adds stricter signature checks. [chat.py](../src/our_harness/chat.py) retains original bytes, MIME, size, digest, and available dimensions without resizing. Header metadata is not a full image decode. |
| [threads.ts][up-threads]: `makeEnvironmentThreadState`, committed-state tracking, stream-item application, and stale-owner guards | [app.js](../src/our_harness/ui/app.js) `goalSnapshotIdentity`, `rememberChatGoalSnapshot`, and `rememberGoalSnapshotInventory` adapt monotonic projection. Nexus uses server goal revisions and local read tickets, with exact goal/chat/project/source identity. Older reads cannot restore resolved questions. Fresh same-revision policy projections remain possible, and a fresh authoritative inventory can remove missing goals. This does not add WebSocket subscriptions or persistent client snapshot caching. |

Nexus also adds the explicit **Reconsider using saved answers** action through
`goal_decisions.reconsideration_sources`, `GoalStore.reconsider_interrupts`, and
`LongHorizonRuntime.reconsider` in
[long_horizon.py](../src/our_harness/long_horizon.py). Each pending ordinary
question must have an earlier ordinary answer visible to its requesting agent.
An exact question match is preferred, but any such eligible answer permits a
reread, including for a paraphrased question. Recorded decision scopes must
match; older answers without scope metadata remain readable. The operation
requires the exact goal revision, all current pending IDs, and settled
requester tasks, and excludes authority/risk cards. It preserves the earlier
answer, marks the current card **superseded**, and resumes the requester to
reread history and inspect current facts. It supplies no new answer, does not
mark the question resolved, and grants no permission. This explicit recovery
path is separate from automatic normalized repeat suppression.

## Disposition of the complete evaluation

The IDs retain the evaluation's scope so an evaluated idea is not mistaken for
an implemented feature.

| ID | Evaluated unit or pattern | Disposition in this change |
| --- | --- | --- |
| A01 | Structured answers: [pendingUserInput.ts][up-input], `UserInputAttachmentAnswerPayload` | **Adapted.** Raw answers, saved question validation, audience, durable decision retrieval, exact response receipts, and Nexus's explicit reconsideration action. |
| A02 | Provider/workspace identity: [ensureSessionForThread][up-reactor] | **Pattern adapted.** Typed engine workspace context and separate transport cwd. A persistent-session transplant would require a separate provider-runtime migration; Nexus's bounded dispatch already owns session effects. |
| A03 | Native image input and attributed metadata: [ClaudeAdapter][up-claude], [CodexAdapter][up-codex], `ChatImageAttachment`/`SnapShotSource` in [contracts][up-contract] | **Partially adapted.** Original native image bytes, digest/MIME/dimensions, and honest unsupported-input behavior. Accessibility trees and application/window capture metadata require platform capture integration beyond the existing uploaded-image workflow. |
| A04 | Terminal-wins request replay: [derivePendingRequests][up-requests] | **Pattern adapted.** Exact submission receipts plus monotonic UI snapshots. Nexus adds bounded repeat detection; this is not general semantic equivalence matching. |
| A05 | Question draft/upload ownership: [questionAttachments.ts][up-question-attachments], [attachmentUploadQueue.ts][up-upload] | **Evaluated.** Per-question files would add a new upload workflow; current goal answers and follow-ups accept text, while initial attachments already have owned storage. |
| A06 | Authored media reference distinct from display URL: [mediaReference.ts][up-media] | **Evaluated.** The diagnosed destination confusion occurs at provider input, addressed by A02/A03; no media copy-menu defect required this separate UI helper. |
| A07 | Transactional event/projection/receipt commit: [OrchestrationEngine.ts][up-engine], `processEnvelope` | **Existing Nexus boundary retained and extended.** Answer records, resolution events, and submission receipts use the existing goal mutation transaction. That equivalent transaction boundary avoids a second event store or engine. |
| A08 | Reconnect and stale-snapshot projection: [threads.ts][up-threads] | **Partially adapted.** Revision/read-ticket projection and causal inventory omission fit the existing polling UI. Full subscriptions, persistent caches, and synchronization markers would require a separate transport/state migration. |
| A09 | Turn-window history and stable keyset cursors: [threadDetailCursor.ts][up-cursor], older-page merging in [threads.ts][up-threads] | **Evaluated.** Nexus already has authenticated sequence-based dialogue pagination; importing turn-history cursors would duplicate that mechanism and require a history-model migration. |
| A10 | Bounded stream coalescing: [ThreadLiveEventCoalescer.ts][up-coalescer], [LiveStreamBudget.ts][up-budget] | **Evaluated.** The affected Nexus UI polls durable snapshots; this live subscription coalescer belongs to a distinct streaming-transport change. |
| A11 | Queue-plus-active-work drain barrier: [DrainableWorker.ts][up-drain] | **Evaluated.** Existing deterministic worker-entry/release barriers already prove execution overlap; replacing the worker abstraction adds no needed evidence for this workflow. |
| A12 | Provider capability algebra: [ProviderAdapter.ts][up-adapter] | **Evaluated.** Targeted image-contract checks were tightened. General model-switch, compaction, and rollback capabilities require a separate adapter/API migration beyond this input correction. |
| A13 | Image layout, scroll anchors, composer event boundaries: [imageDimensions.ts][up-dimensions], [scrollAnchor.ts][up-scroll], [composerEventScope.ts][up-composer] | **Partially adapted.** Header metadata supports the image contract. The virtualizer-specific scroll anchor and upstream composer boundaries require a separate transcript/composer redesign, not this input/replay fix. |
| A14 | Bounded image conversion: [imageCompression.ts][up-compression] | **Evaluated.** Compression/downscaling can lose small screenshot text, conflicting with the original-image fidelity objective; Nexus preserves the bytes. |
| A15 | Reserved context budget: [ProviderCommandReactor.ts][up-reactor], `formatThreadTitleContext` | **Concept adapted.** Nexus reserves decision context separately and offers paged retrieval. The title-regeneration algorithm itself was not copied: provider context must preserve later authoritative corrections, not favor the first message solely because it came first. |

Nexus retains its authenticated Git and non-Git private workspaces, source
identity checks, publication conflict checks, provider-effect reconciliation,
exact verification approvals, and completion requirements. Upstream's
best-effort worktree recreation, lexical path helpers, inactivity/PR-based
settlement, and provider-turn completion are not substitutes for these
boundaries.

## Verification scope and limitations

The regression sources are
[test_goal_decisions.py](../tests/test_goal_decisions.py),
[structured-goal-answers.test.js](../desktop/structured-goal-answers.test.js),
[goal-snapshot-ordering.test.js](../desktop/goal-snapshot-ordering.test.js),
[test_native_input_context.py](../tests/test_native_input_context.py), and
[test_attachment_input_fidelity.py](../tests/test_attachment_input_fidelity.py).
They exercise raw answer separation, recipient scope, retry/restart behavior,
stale snapshots, actual provider payload construction, original image bytes,
workspace context, and terminal-result failures. These checks do not establish
a live-model quality benchmark.

Native image delivery does not guarantee accurate screenshot reading or OCR.
A typed user correction remains distinct from an assistant's transcription.
Repeat detection covers exact request replay and normalized question/progress
conditions; it does not prove that arbitrary paraphrases are equivalent.
User evidence retains its original recipient through independent review and
task handoff. Scoped provider-context projection includes legacy answer/steering
records when their audience is authenticated; reassignment invalidates cached
private decision/conversation pages. The saved history is retained.

User answers do not grant new filesystem or command authority, and provider
completion does not establish that a Nexus goal has passed verification.

## Upstream notice

The pinned [upstream LICENSE][up-license] identifies the following notice.
It is retained here with the source provenance. Legal owns licensing
interpretation and approval decisions.

```text
MIT License

Copyright (c) 2026 T3 Tools Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

[upstream]: https://github.com/KZTP47/t3code
[commit]: https://github.com/KZTP47/t3code/commit/eb115063634c416c6362cc407f8572cb0c136ddf
[up-input]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/web/src/pendingUserInput.ts
[up-contract]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/contracts/src/orchestration.ts
[up-requests]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/client-runtime/src/pendingRequests.ts
[up-reactor]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/orchestration/Layers/ProviderCommandReactor.ts
[up-workspace]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/checkpointing/Utils.ts
[up-claude]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/provider/Layers/ClaudeAdapter.ts
[up-codex]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/provider/Layers/CodexAdapter.ts
[up-codex-runtime]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/provider/Layers/CodexSessionRuntime.ts
[up-dimensions]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/shared/src/imageDimensions.ts
[up-threads]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/client-runtime/src/state/threads.ts
[up-question-attachments]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/web/src/questionAttachments.ts
[up-upload]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/web/src/lib/attachmentUploadQueue.ts
[up-media]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/client-runtime/src/mediaReference.ts
[up-engine]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/orchestration/Layers/OrchestrationEngine.ts
[up-cursor]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/orchestration/threadDetailCursor.ts
[up-coalescer]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/orchestration/ThreadLiveEventCoalescer.ts
[up-budget]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/orchestration/LiveStreamBudget.ts
[up-drain]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/shared/src/DrainableWorker.ts
[up-adapter]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/server/src/provider/Services/ProviderAdapter.ts
[up-scroll]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/packages/client-runtime/src/work-log/scrollAnchor.ts
[up-composer]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/web/src/components/chat/composerEventScope.ts
[up-compression]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/apps/web/src/lib/imageCompression.ts
[up-license]: https://github.com/KZTP47/t3code/blob/eb115063634c416c6362cc407f8572cb0c136ddf/LICENSE
