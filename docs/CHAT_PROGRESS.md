# Progress in team conversations

Saved team chats show timestamped Nexus progress entries between agent replies:

- A reply was requested from an agent.
- The provider returned a response for Nexus to check.
- The agent requested tools, and Nexus started handling those requests.
- Tools returned results, including failed checks or an approval requirement.
- Nexus sent tool results and updated context back to the agent.

These entries come from recorded engine events, not timers or inferred model
activity. A dispatched request does not prove the provider has started generating.
Agent explanations continue to appear as the agent's own messages. Tool inputs
and output remain expandable; completed output is placed at its return time.

`goal_chat_progress.py` supplies bounded public wording. `chat.py` projects the
authenticated event journal into the exact admitted conversation using stable
event identities and the `engine-milestones/v1` contract. Both chat renderers
display that saved projection. Existing goal polling updates open conversations;
refreshing or reopening a conversation does not create another provider request.

New tool results record their own occurrence time. Older results use the saved
batch completion time when available. Previously retired operational events
cannot be reconstructed, and entries without occurrence times are not invented.
The public dialogue archive and retained tool snapshots still recover their
respective evidence independently.

Verification reports are interpreted only for the engine's verification tool.
Successful transport does not imply checks passed, and JSON inside a file read
is never interpreted as verification status. Redaction and exact chat bindings
apply before progress is saved; the browser renders milestone text literally.

Validation: `tests/test_goal_chat_progress.py` exercises the real scheduler,
journal, dialogue archive and saved chat using deterministic provider/tool
fixtures. `desktop/team-progress.test.js` sends those intermediate snapshots
through both production chat renderers, including narrow layouts and reopening.
