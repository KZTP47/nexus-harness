from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "src/our_harness/ui/app.js").read_text(encoding="utf-8")
MARKUP = (ROOT / "src/our_harness/ui/index.html").read_text(encoding="utf-8")
STYLES = (ROOT / "src/our_harness/ui/styles.css").read_text(encoding="utf-8")


def between(start: str, end: str) -> str:
    return APP[APP.index(start):APP.index(end, APP.index(start))]


class WebChatRendererReliabilityTests(unittest.TestCase):
    def test_manager_shows_local_state_before_best_effort_python_sync(self) -> None:
        manager = between("async function openWebChatManager", "async function showFullWebChatInsideNexus")
        self.assertLess(manager.index("showModal()"), manager.index("await Promise.allSettled"))
        self.assertIn("refreshLocalWebChatConnections()", manager)
        self.assertIn("void heartbeatWebChats", manager)
        self.assertNotIn("await heartbeatWebChats", manager)

        viewer = between("async function showFullWebChatInsideNexus", "async function hideEmbeddedWebChat")
        self.assertIn("await refreshLocalWebChatConnections()", viewer)
        self.assertNotIn("await heartbeatWebChats", viewer)

    def test_explicit_selection_is_applied_before_heartbeat_and_failure_is_retained(self) -> None:
        callback = between(
            "window.harnessDesktop.onWebChatsChanged(async (chats, selected)",
            "if (canUseWebChats())",
        )
        self.assertLess(
            callback.index("await assignSelectedWebChatToPendingAgent(selected)"),
            callback.index("heartbeatWebChats"),
        )
        assignment = between(
            "async function assignSelectedWebChatToPendingAgent", "function acceptLocalWebChatConnections",
        )
        self.assertIn("target.selectedChat = {...chat}", assignment)
        self.assertIn("target.lastError", assignment)
        self.assertNotIn("expiresAt < Date.now()", assignment)
        # Reconnecting the exact route is already durable and may clear its
        # pending selection without rewriting the board.  A real route change
        # must still retain the selection until the board save succeeds.
        board_assignment = assignment[
            assignment.index("const worked = await changeTheSwarmBoard"):]
        self.assertLess(
            board_assignment.index("if (worked && assigned)"),
            board_assignment.index("webChatAssignTarget = null"),
        )
        self.assertIn("catch (error)", callback)
        self.assertIn("webChatAssignTarget.selectedChat = {...selected}", callback)
        self.assertIn("Retry assignment", APP)
        self.assertIn("Add as a new agent instead", APP)

    def test_concurrent_relays_have_request_scoped_terminal_receipts(self) -> None:
        bridge = between("async function serviceWebChatBridge", "function startWebChatBridge")
        relay = between("async function relayOneWebChatRequest", "async function serviceWebChatBridge")
        self.assertIn("const webChatRelayStatuses = new Map()", APP)
        self.assertIn("one?.request_id", between("function webChatRelayKey", "function renderWebChatRelayStatuses"))
        self.assertIn("one?.route", between("function webChatRelayKey", "function renderWebChatRelayStatuses"))
        self.assertIn("const settled = await Promise.allSettled", bridge)
        self.assertIn("settled.forEach((result, index)", bridge)
        self.assertIn('"unreconciled"', bridge)
        self.assertNotIn('$("webChatSaid")', relay)
        self.assertIn("confirmation.accepted === true", relay)
        self.assertIn("confirmation.accepted === false", relay)
        self.assertIn("The provider prompt was not resent", relay)
        self.assertNotIn("returned its visible reply to Nexus", APP)

    def test_relay_delivery_list_is_visible_and_stateful(self) -> None:
        self.assertIn('id="webChatRelayActivity"', MARKUP)
        self.assertIn('id="webChatRelayStatuses"', MARKUP)
        self.assertIn("only after Nexus confirms that exact request", MARKUP)
        self.assertIn(".web-chat-relay-status[data-state=\"confirmed\"]", STYLES)
        self.assertIn(".web-chat-relay-status[data-state=\"unreconciled\"]", STYLES)
        self.assertIn(".web-chat-assignment-repair", STYLES)

    def test_settings_copy_disagreement_requires_an_explicit_recovery_choice(self) -> None:
        self.assertIn('id="webChatSettingsRecovery"', MARKUP)
        self.assertIn('id="webChatRecoveryBanner"', MARKUP)
        self.assertIn('id="desktopSettingsRecoveryCard"', MARKUP)
        self.assertIn("Nexus has not restored or deleted either copy automatically", MARKUP)
        self.assertIn(">Restore these chats</button>", MARKUP)
        self.assertIn(">Start with no recovered web chats</button>", MARKUP)
        self.assertIn(".web-chat-settings-recovery", STYLES)
        self.assertIn(".web-chat-recovery-banner", STYLES)
        self.assertIn(".desktop-settings-recovery-card", STYLES)
        recovery = between(
            "function webChatRecoveryNeedsChoice", "async function heartbeatWebChats",
        )
        self.assertIn("desktopSettingsRecoveryStatus()", recovery)
        self.assertIn("resolveDesktopSettingsRecovery(action)", recovery)
        self.assertIn('["restore", "discard_web_chats"]', recovery)
        self.assertIn("Nothing was changed", recovery)
        self.assertIn("Your other desktop settings were preserved", recovery)
        self.assertIn('count > 0 ? "Restore recovered web chats" : "Repair settings copies"', recovery)
        self.assertIn("await refreshLocalWebChatConnections()", recovery)
        self.assertIn("void heartbeatWebChats(true, false)", recovery)
        self.assertNotIn("resolveDesktopSettingsRecovery(\"restore\")", between(
            "async function openWebChatManager", "async function showFullWebChatInsideNexus",
        ))


if __name__ == "__main__":
    unittest.main()
