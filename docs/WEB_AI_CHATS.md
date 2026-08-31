# Web AI chats

Nexus can use an ordinary provider website as an agent route. Provider login is
isolated from the person's everyday browser and from other provider kinds, but
multiple Nexus connections for the same provider deliberately share that
provider's Nexus-owned account session. They are separate conversations, not
separate paid accounts or independent quotas. Each Nexus pair chat keeps its
own provider conversation URL.

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

**Run relayed web AI turns in the background** keeps this ordinary browser
window minimized while agents work. It is intentionally not Chromium headless
mode, because providers such as Claude reject that browser identity. Connecting,
signing in, and an explicit **Show secure browser** action still bring the
provider window forward. The choice is saved in the desktop app settings.

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

Specific conversation bindings are immutable during ordinary navigation.
OAuth pages, account/settings pages, and a manually opened different chat do
not retarget an agent. A generic new-chat page may bind once to the specific
conversation created by its first submitted turn; any later change requires
the explicit **Use this chat in Nexus** action. Nexus durably verifies that
binding before attaching files or pressing Send, so a persistence failure is a
known not-sent outcome rather than a prompt delivered to an untracked thread.

Connections that share one provider profile also share one physical consumer
session and its rate/quota boundary. The engine serializes those turns through
one provider resource while still allowing different provider profiles to run
in parallel. Separate conversation cards never imply separate paid accounts.

## Message submission

ChatGPT and Claude receive turns through Chromium's native editing/input path.
Nexus does not treat text that merely appears after direct DOM mutation as a
submitted message: modern React and ProseMirror editors can render that text
without committing it to their application state. The provider must render the
one-use Nexus turn marker as a new user message before any reply is accepted.
That receipt is found by the unique marker itself as well as provider-owned DOM
attributes, so a Claude wrapper/test-id change cannot turn a visibly submitted
message into a false unknown outcome. Replies are still accepted only when the
actual reply node follows that unique marker in document order.
This preserves background operation while preventing unsent drafts and stale
provider replies from masquerading as completed turns.

Provider delivery and Nexus protocol validation are recorded separately. If a
visible reply arrives but fails a required collaboration schema, Nexus reports
that concrete validation failure and may request one format-only correction; it
does not claim that provider delivery was unknown. Only a dispatch that cannot
be paired with its unique marked turn remains outcome-unknown and is never
resent automatically.

After Electron captures a reply, it returns an idempotent completion receipt
to the Python goal engine. A transient network failure retries only that exact
receipt; it never resubmits the provider prompt. The engine retains a bounded
receipt window so a lost HTTP acknowledgement can be reconciled safely. The
Web AI panel shows relay and receipt state per request, preventing one
concurrent route's success from hiding another route's failure.

An outcome-unknown turn is not a dead end. **Repair connection** classifies the
saved failure without spending a model request and offers **Inspect provider
conversation**. That action opens the exact bound provider thread so the user
can check whether the turn arrived before deciding to retry. Nexus never turns
that inspection into an automatic resend.

If a web provider returns useful natural-language collaboration text but still
fails the required control schema after one format-only correction, Nexus keeps
the exact text as an attributed team contribution. It explicitly marks
completion/progress as unverified and infers no machine state from the prose;
the agent remains available for later rounds instead of being discarded as if
it never answered.

The Electron connection heartbeat reports availability only. A provider chat
joins a board only after **Use this chat in Nexus** or **Add to board**; a global
connection can no longer re-add a removed agent, leak into another project, or
block the pending-turn courier when a board is full.

Desktop connection metadata is written with an atomic replacement and a
last-known-good copy. If disk space or access prevents saving, the Web AI chats
panel says so and keeps the in-memory connection visible; fix the disk problem
and press **Use this chat in Nexus** again rather than discovering the loss only
after restart.

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
