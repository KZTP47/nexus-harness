# Public provider activity in team chat

Current request: fix the confirmed omission of public provider messages and native
tool activity from Nexus chat. Owner of every item: primary agent.

| ID | Observable outcome | Owner paths / dependencies | Evidence | State |
|---|---|---|---|---|
| PA1 | Codex public messages and tool lifecycle events arrive before final reply | providers, bounded capture | Streaming subprocess fixture | verified |
| PA2 | Standard Claude CLI exposes public text, tool requests and results | subscription_cli; PA1 transport | Claude stream fixture, existing image tests | verified |
| PA3 | Activity survives closed UI/restart with correct goal, agent and dispatch identity | long_horizon, goal_dialogue; PA1 | Real store and chat projection tests; stale lease/effect rejection | verified |
| PA4 | Chat shows public text and expandable native tools without duplicate finals or tool rows | goal_chat_projection, ui; PA3 | Renderer, projection and packaged collaboration/restart checks pass | verified |
| PA5 | Private reasoning, credentials and unbounded output never enter public activity | provider activity contract; PA1–4 | Negative, redaction, overflow and failure tests | verified |
| PA6 | Existing collaboration and provider contracts pass; app/installer/shortcut rebuilt | all joined work | Regression suites, packaged checks and mandatory post hook passed | verified |

Use the existing authenticated dialogue archive for operator-only activity, outside
the teammates' shared prompt history. Provider activity is observational and grants
no execution authority. Existing custom CLI argument contracts remain supported;
their stream capability must not be silently assumed.

Implementation uses the versioned `public-provider-activity/v1` contract and a
per-dispatch frozen effect/lease/agent identity. Activity is redacted before its
authenticated archive append. The existing archive chain, page API and transcript
projection handle recovery; no extra telemetry store or provider execution path
was introduced. Tool lifecycle records collapse into one stable expandable row.
Public messages matching the final structured response are left to the existing
action handler. Large events carry an explicit truncation notice.

Scope: Codex CLI and standard Claude CLI team calls. Custom Claude commands keep
their configured argument contract and display a coverage notice. This cannot
recover activity discarded by older builds, expose private reasoning, or invent
events absent from a provider's public stream. Providers retain their existing
execution permissions, final response validation and budgets.

Protocol references: [Codex public item types](https://github.com/openai/codex/blob/main/sdk/typescript/src/items.ts)
and [Claude CLI stream output](https://code.claude.com/docs/en/cli-reference).

Verification (2026-09-14):

- Nine new activity tests passed on source and packaged Python. The real-store
  integration also passed with callbacks running on separate stdout-reader threads.
- 194 existing provider/input/dialogue/projection/progress tests were exercised.
  The three initial callback-argument compatibility failures were corrected;
  the full affected 34-test Codex suite then passed. The other 160 passed.
- All 443 desktop tests passed. The provider tool row was additionally checked
  against the packaged renderer, including expansion persistence and attribution;
  its generated screenshot was visually inspected.
- Packaged team-chat acceptance passed actual chat controls, dialogue, file work,
  steering, approval, independent verification and saved-chat restart.
- Final provider source was staged into the built runtime for the packaged checks;
  the mandatory post-work gate rebuilds the distributable from product source.
- `git diff --check` passed. Existing unrelated worktree edits were preserved.
- Final post-work gate passed at 2026-09-14T00:42:25Z; app, NSIS installer and
  desktop shortcut/icon refreshed. All ten changed product files were then
  verified byte-identical between repository source and the rebuilt app.

## Current request: optional collapsible public reasoning summaries

Owner: primary agent. This extends the existing activity work; PA1–PA6 above
remain the completed prior request.

| ID | Observable outcome | Paths / dependencies | Evidence | State |
|---|---|---|---|---|
| RS1 | Documented public Codex summaries appear; raw thinking remains excluded | provider_activity | Parser positive/negative tests passed | verified |
| RS2 | Optional summary capture never blocks or fails an agent call, or adds calls | summary dispatch, archive | Stalled/failed sink and integration call-count tests passed | verified |
| RS3 | Operator observations cannot alter shared prompt counts or invalidate conversation reads | goal_dialogue, long_horizon | Context equality, archive migration and freshness tests passed | verified |
| RS4 | Summaries are collapsed in both chat sizes, labelled and safe to expand | projection, UI | Browser rendering and restart/dedup tests passed; screenshot inspected | verified |
| RS5 | Agent collaboration and final-goal workflow remain verified; deployment completes | joined work | Regression, packaged shared-project collaboration and final post hook passed | verified |

Summary scope: Codex's documented `ReasoningItem` is a public summary, distinct
from raw reasoning fields. Claude thinking/redacted-thinking blocks remain
excluded; no model is prompted or configured to generate additional summaries.
The shared bounded optional queue never waits for capacity and never propagates
writer failures to a provider turn. Backlogged, stale or unavailable summaries
may be omitted so agent work continues. Summaries do not grant execution or
completion authority.

Participant conversation counts and freshness now use a versioned authenticated
projection excluding all operator-only observations. Existing archives migrate
from their authenticated rows. Operator-only rows do not consume agent-facing
pagination slots, and summaries are excluded from ordinary chat history too.

Summary verification (2026-09-14): 207 Python checks and all 443 desktop tests
passed. All 13 activity/summary checks also passed on packaged Python, and the
packaged renderer passed its collapsible-summary check. Packaged team-chat
acceptance passed in facilitator/shared-project mode, covering agent coordination,
file work, steering, independent verification, approval and restart. Screenshot
inspection confirmed readable summaries and safe text rendering in both views.
Final post-work deployment passed at 2026-09-14T01:05:14Z. App, installer and
desktop shortcut/icon were refreshed; all eight final product files matched the
rebuilt application byte for byte.
