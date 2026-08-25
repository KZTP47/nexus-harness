# Web AI chats

Nexus can use an ordinary provider website as an agent route. Each connection
keeps its own provider login and each Nexus pair chat keeps its own provider
conversation URL.

## Two browser transports

The provider definition chooses the browser transport.

- **Embedded** is the default. ChatGPT, Gemini, and Copilot run in sandboxed
  Electron views with one persistent partition per provider.
- **Secure external browser** is used when a provider rejects embedded
  Chromium. Claude currently uses this transport.

Claude's Cloudflare verification returns HTTP 403 and repeats indefinitely in
Electron, even after issuing a clearance cookie. It also challenges Chrome when
Chrome is launched with an automation identity. Nexus therefore starts the
installed Google Chrome or Microsoft Edge normally, with a dedicated persistent
profile, and attaches only after the browser is running. The browser keeps
`navigator.webdriver` disabled and reaches Claude's real sign-in page.

The external browser uses a random loopback-only debugging port. It does not
read or alter the person's everyday Chrome or Edge profile. Nexus closes the
controlled browser when the desktop app closes.

Provider sign-in may replace the original tab (for example, an OAuth tab can
finish at Claude and close its opener). **Use this chat in Nexus** resolves the
currently selected, visible provider conversation at click time instead of
assuming the tab that began sign-in is still alive. A later transient page
replacement is recovered before Nexus reads or writes the conversation.

## Connecting Claude

1. Open **Web AI chats** and choose **Claude**.
2. Sign in in the secure Chrome or Edge window Nexus opens.
3. Start or open the Claude conversation to use.
4. Return to the small Nexus control window and press **Use this chat in
   Nexus**.

The dedicated profile is stored below the Nexus desktop app's user-data folder
under `external-web-chat/claude`, so the login survives ordinary restarts.

## Conversation identity

The browser transport does not determine chat identity. Nexus uses the tuple
`connection id + Nexus conversation key`, and stores the provider's specific
conversation URL after the provider creates it. Two GPT Codex ↔ Claude2 chats
therefore use two controlled browser pages and two saved Claude conversation
URLs, even though both share the same authenticated Claude profile.

## Message submission

ChatGPT and Claude receive turns through Chromium's native editing/input path.
Nexus does not treat text that merely appears after direct DOM mutation as a
submitted message: modern React and ProseMirror editors can render that text
without committing it to their application state. The provider must render the
one-use Nexus turn marker as a new user message before any reply is accepted.
This preserves background operation while preventing unsent drafts and stale
provider replies from masquerading as completed turns.

## Shared collaboration context

Web-provider agents participate in the same durable collaboration ledger as
desktop and command-line agents. Nexus includes the current goal, latest shared
state, ledger-relative paths, and that agent's unseen ledger entries in each
turn. The full readable Markdown mirror remains available locally in the pair
chat. Nexus does not assume that a provider website can read project files, so
the per-agent delta is always carried through the browser transport as well.

Text copied from any agent is explicitly delimited as conversation evidence;
it cannot grant file authority, change the response schema, or become a Nexus
instruction merely because it appears in the shared ledger.
