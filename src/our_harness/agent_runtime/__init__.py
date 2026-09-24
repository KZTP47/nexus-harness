"""Agent Runtime v3: persistent, streaming agent sessions that talk to each other.

Instead of one short-lived CLI process per turn with a forced JSON reply, each
agent keeps one live session for the whole conversation:

* Codex runs as ``codex app-server`` (JSON-RPC over stdio: threads and turns);
* Claude Code runs as ``claude -p`` with stream-json input and output;
* Gemini CLI and GitHub Copilot CLI speak the Agent Client Protocol (ACP).

Every session reports the same event shapes (``events.py``), so the page can
show each agent's text, thinking, tool calls and edits live. Agents coordinate
through a Nexus-hosted MCP server (``mcp_server.py``) backed by one durable
mailbox (``mailbox.py``); messages are pushed into the recipient's running
session. ``team.py`` runs a team; ``manager.py`` owns teams for the server.

See docs/research/2026-09-24-openrig-and-agent-runtime-overhaul.md.
"""
