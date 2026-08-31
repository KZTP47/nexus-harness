# The tray of chats

A board with five agents on it is five conversations. They used to be small
cards floating on the board itself, so two open at once covered the board they
were about, and a fifth was somewhere off the side.

Now every open chat is a button along the bottom, the way a taskbar works.

## What it does

- **Every open chat sits in the tray**, showing the chat's name, which assistant
  it uses, and a face. Arrows at either end for when there are more than fit.
- **Press one** and that chat opens big, over the board.
- **Hover the face** and a line is drawn to that agent's box on the board. Five
  chats called "chat with an agent" are five chats nobody can tell apart.
- **Minimise** puts it back in the tray and keeps it open. **Close this chat**
  really closes it. Two buttons because they are two different things, and
  closing would throw away which chats you had open.
- **Escape** minimises, for the same reason.

## The big chat

The left pane is the conversation switcher. It groups chats by the canonical
two-agent pair on the green communication line, shows both agents' names and
faces, and can create, switch, archive, or restore several durable chats for
that pair. Archiving never removes its transcript, attachments, provider-thread
identity, or shared-agent ledger; archived rows remain visible and reversible.
A chat under GPT Codex ↔ Claude is not reused by GPT Codex ↔ Gemini, even when
the same provider route appears in both pairs. Every agent also keeps a
permanent direct-chat group of its own, alongside all connected-pair groups,
and that group can hold several fresh, independently saved chats. Gaining or
losing a green-line peer never removes the group or its history. Those
one-agent chats remain one-agent conversations even after peers are connected:
automatic routing stays direct, team/project-work actions stay unavailable,
and connected non-members are not added to the provider context.

### Resize it to fit the work

The maximised chat is not a fixed dashboard. Five visible resize controls let
somebody change the whole window, the agent-chat list, the **Where this chat
happens** section, the transcript/activity split, and the composer/control
section. Drag a handle with a mouse or touch input. When a handle has keyboard
focus, use the arrow keys for 16-pixel steps or Shift+Arrow for 48-pixel steps.
Home or a double-click restores that one boundary; **Reset sizes** restores the
entire layout.

Sizes are kept in the browser/Electron profile and return after the app is
reopened. They are clamped to the current viewport and to functional pane
minimums, so moving from a large monitor to a small one cannot strand the close,
send, or reset controls off-screen. Narrow chat windows collapse the optional
activity pane while leaving the transcript and composer usable.

Older Nexus versions stored one transcript under an agent's stable board name
before pair chats had opaque IDs. The chat index now detects those files and
adds a clearly labelled **Recovered older chat** under that agent without
guessing which later pair owned it. The source file stays intact as recovery
evidence. The registry itself is written atomically, mirrored to a last-known-
good file, and structurally versioned before chat additions or archives; a
malformed primary registry is rebuilt from those copies instead of silently
showing an empty history.

Every chat also stores one **This chat writes to** selection. The dropdown lists
only projects that both agents work on. Ordinary conversation can continue with
no project selected, but project-file work is refused until one is chosen. The
server resolves the saved conversation again on every request, so changing a
browser field cannot substitute another pair or folder.

The same boundary applies when this folder's local project-execution authority
needs repair: **Ask _selected agent_ only** and **Ask both/all agents** remain available. Only
requests that clearly ask for file changes, explicit project work, and saved
work resumes are paused. Conversation uses a separate durable, path-scoped
journal, so it keeps duplicate-delivery and cross-window protections without
acquiring authority to execute the project.

Every compact and big chat starts by saying **where this chat happens**. The
conversation belongs to Nexus Harness and is saved under `.harness/chats`.
The named provider route and model say how Nexus obtains an answer; they do not
mean that Nexus has opened a matching conversation in Claude Desktop, the
Codex app, Gemini, Copilot chat, ChatGPT, or another provider app. The panel
says this explicitly for the configured provider instead of implying a link
that does not exist.

Every broken or not-ready agent surface uses the same **Repair connection**
flow. It selects that exact board agent, shows the real configured route and
model, and asks the engine for a typed diagnosis. Diagnosis and **Check again**
are free; **Run live test** is separately labelled because it spends one model
request in an empty temporary folder. The engine distinguishes authentication,
configuration, missing model, quota/capacity, rate limit, network, timeout,
protocol, missing route, and uncertain-outcome failures, so a malformed config
is never presented as a login problem. Recovery actions carry the exact route
and diagnosis fingerprint. A consequential Claude update/logout/login action
is refused if either changed after the panel was rendered.

**Open full Nexus chat** opens the conversation that is actually in use. In the
Electron app, **Show saved transcript file** reveals its exact JSON transcript
inside the current project. The desktop bridge refuses absolute paths, parent
directory traversal, missing files, and anything outside that project.

Three parts side by side:

**What was said.** Your turns and the assistant's, each with a face. And what
this agent said to *another* agent, in a colour of its own — because a
conversation between two of them is a conversation, and reading it somewhere
else is how you lose the thread.

Interactive collaboration is kept as a real multi-party transcript rather
than collapsed into the lead's summary. In order, the chat shows the prompt
sent to the team, every contacted agent's exact redacted reply or project
plan, every sequential discussion/review turn, each execution and verification
pass, and the lead's final completion report.
Each turn names its speaker, recipient, provider route, phase, model, and
timing when available. The connected-agent turns remain visible in later
history but are not replayed to a provider as though the lead agent had said
them; only the user/final-answer conversation continues under assistant roles.

Every team request also ends with a durable **Team response status** turn. It
states how many agents were requested and how many answers were saved, names
each missing or later-failing participant, and offers **Repair _agent_** for the
exact recorded route. A known failure can restore the original prompt into the
composer for review; that control never sends automatically. An uncertain
delivery cannot be retried from the card: Nexus says that it will not resend
and directs the user to inspect or repair the exact provider connection first.
The status survives reopening the app because it is part of the transcript,
not a temporary notification.

If an older chat has a collaboration ledger that predates the authenticated
format or fails its integrity check, **Where this chat happens** shows a
scoped **Reset collaboration record** action. It removes only that recreatable
agent-to-agent ledger after confirmation. The saved transcript, attachments,
and provider conversation identity are preserved, and no prompt is sent.

**What it has going on.** What this agent is doing in the run right now: which
project, which round, which part of the shared page it wrote, how long it took.
And what went wrong the last time it was asked anything.

**A box to type in.** It has three deliberately different actions. The labels
name their recipients, so a new user does not have to infer whether a pair chat
contacts one provider or the team:

- **Ask _selected agent_ only** is a faithful one-agent request. It sends the
  message only to the selected agent, even in a pair chat. Nexus never turns an ordinary message
  into a team discussion merely because another agent is connected. An
  unmistakable file/code mutation request still asks for confirmation before
  using the bounded project-work transaction.
- **Ask both agents** (or **Ask all _N_ agents**) contacts every participant in
  the selected saved chat,
  then continues sequential discussion rounds in which every later agent sees
  the full real conversation. It ends when every participant marks the goal
  complete, or reports honestly that progress stalled or reached its safety
  ceiling. New chats default to a finite three-round ceiling; **Unlimited** is
  an explicit opt-in for a user who intentionally wants an open-ended run.
- **Work on project files** is explicit mutation authority. It starts one
  durable long-horizon goal led by the selected agent. The agent works alone
  when that is sufficient and may create bounded subtasks, hand work off, or
  ask for targeted independent review. Nexus validates relative paths and
  current-file baselines, journals transactions before applying them, and runs
  deterministic verification before claiming completion. The older paired
  plan/review/execution ritual is still available as an explicit legacy mode
  on the board, not as the default.

When an agent needs a real user decision, Nexus can render the request inline
as a question card. The card supports recommended choices, descriptions,
single- or multi-select answers, and a custom answer. Submitting it continues
the same saved conversation. Project-work questions pause the exact durable
run and resume it with the answers; they are not reported as provider failures.

Attachments are copied into `.harness/chats/attachments`. The transcript keeps
only safe metadata. Text enters bounded context; images use each supported
provider's native multimodal input.

Assistant replies keep fenced code blocks as code. Every block has its own
**Copy code** button; if clipboard access is unavailable, the block is selected
so it can still be copied manually.

## While an agent is working

Compact and maximised chats show the same prominent activity panel as soon as
a request starts. It includes an animated spinner and progress track, a
shimmering stage label, supporting detail, and elapsed time. The animation is
disabled when the operating system requests reduced motion; the words and
elapsed time remain visible.

These are real Nexus orchestration stages, not invented provider thoughts. A
direct request says which named agent and provider route Nexus is waiting for.
Collaboration reports initial contact, each visible team-discussion round, and
the final outcome report. Project work also reports plan reviews, confined file
reads, every bounded apply pass, and every on-disk verification pass. The
provider's private reasoning is neither requested nor shown.

An active turn belongs to the saved chat ID, not to the agent card. You can
select or create another chat while the first one works, type a separate draft,
and send it without replacing the first chat's progress or transcript. Running
rows stay marked in the conversation list. Returning to one shows its own live
turns and Stop button; completion in the background never clears the draft in
the chat now on screen.

Nexus admits different chat IDs independently, including two chats under the
same agent pair. The exact same chat remains single-turn across every Nexus
window and process. Archive, project reassignment, and Start again take that
same exact-chat lease, so they cannot race a late provider answer. Stop verifies
the immutable run-to-chat identity, reaches both execution and conversation-only
journals, and its durable signal is watched by the process which owns the
provider call. It therefore stops only the requested chat while siblings keep
working.

The saved-chat registry uses a separate, short cross-process metadata
transaction. Creating, switching, archiving, restoring, or reassigning two
different chats can happen from different Nexus windows without either window
overwriting the other's registry update. This metadata boundary is released
before provider work, so it does not serialize independent model requests.

Concurrent does not mean ignoring provider limits. Named provider profiles use
their trusted `max_concurrency` as a cross-process slot pool for saved chats.
When a profile is full, the additional chat stays independently queued and can
still be stopped; it dispatches as soon as a slot is free. Different profiles
retain independent pools. Consumer web-chat routes keep their own per-
conversation Electron views and queues.

The long answer request and lightweight activity reads use separate server
threads, so the UI keeps updating while a provider command is still running.
Activity records are bounded and process-local; transcripts remain the durable
record of what was actually said.

## With nothing open

The tray gets out of the way. A bar along the bottom holding nothing is a bar in
the way.
