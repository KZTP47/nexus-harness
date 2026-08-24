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
