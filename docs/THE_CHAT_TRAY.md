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
the same provider route appears in both pairs. A lone agent keeps a direct-chat
fallback until another agent is connected.

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

Every compact and big chat starts by saying **where this chat happens**. The
conversation belongs to Nexus Harness and is saved under `.harness/chats`.
The named provider route and model say how Nexus obtains an answer; they do not
mean that Nexus has opened a matching conversation in Claude Desktop, the
Codex app, Gemini, Copilot chat, ChatGPT, or another provider app. The panel
says this explicitly for the configured provider instead of implying a link
that does not exist.

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

**What it has going on.** What this agent is doing in the run right now: which
project, which round, which part of the shared page it wrote, how long it took.
And what went wrong the last time it was asked anything.

**A box to type in.** It has three deliberately different actions:

- **Send** is intent-aware. Nexus first decides whether the request is best
  answered directly or would materially benefit from the ready agents joined
  by green communication lines. Clear collaboration wording is routed
  immediately; implicit requests use a small structured decision by the open
  agent. A routing failure safely falls back to direct chat. An unmistakable
  file/code mutation request asks for confirmation and then routes through the
  same bounded project-work transaction as the explicit Work button.
- **Ask connected agents** asks the other agent in the selected pair,
  then continues sequential discussion rounds in which every later agent sees
  the full real conversation. It ends when every participant marks the goal
  complete, or reports honestly that progress stalled or reached its safety
  ceiling.
- **Work together on project files** is explicit mutation authority. The pair
  reviews a shared plan for the project selected by this chat. Nexus
  validates relative paths and current-file baselines, applies each set
  atomically, then gives all participants the actual tree and file contents to
  verify. Their concrete failures feed another execution pass until everyone
  marks the goal complete, progress stalls, or the safety ceiling is reached.

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

The long answer request and lightweight activity reads use separate server
threads, so the UI keeps updating while a provider command is still running.
Activity records are bounded and process-local; transcripts remain the durable
record of what was actually said.

## With nothing open

The tray gets out of the way. A bar along the bottom holding nothing is a bar in
the way.
