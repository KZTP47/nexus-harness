from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, web_chats
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.providers import base as provider_base


class EffectiveDispatchIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()

    def config(self, command: list[str], *, model: str = "model-a") -> LoadedConfig:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["provider"].update({
            "name": "local", "model": model, "command": list(command),
        })
        return LoadedConfig(data, self.root, [], {})

    @staticmethod
    def executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def test_path_resolution_change_changes_only_the_non_secret_fingerprint(self) -> None:
        first = self.root / "path-a" / "agent-tool"
        second = self.root / "path-b" / "agent-tool"
        first.parent.mkdir()
        second.parent.mkdir()
        self.executable(first, "first binary")
        self.executable(second, "second binary")
        provider = provider_base.LocalProcessProvider(
            self.config(["agent-tool", "--json"])
        )

        with mock.patch.object(provider_base.shutil, "which", return_value=str(first)):
            before = provider.effective_dispatch_fingerprint()
        with mock.patch.object(provider_base.shutil, "which", return_value=str(second)):
            after = provider.effective_dispatch_fingerprint()

        self.assertNotEqual(
            before["effective_dispatch_fingerprint_sha256"],
            after["effective_dispatch_fingerprint_sha256"],
        )
        persisted = json.dumps({"before": before, "after": after})
        self.assertNotIn(str(first), persisted)
        self.assertNotIn(str(second), persisted)
        self.assertRegex(
            before["effective_dispatch_fingerprint_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_binary_replacement_at_the_same_path_changes_identity(self) -> None:
        current = self.root / "agent-tool"
        retired = self.root / "retired-agent-tool"
        self.executable(current, "first binary")
        provider = provider_base.LocalProcessProvider(
            self.config([str(current), "--json"])
        )
        before = provider.effective_dispatch_fingerprint()

        current.rename(retired)
        self.executable(current, "replacement binary with a different identity")
        after = provider.effective_dispatch_fingerprint()

        self.assertNotEqual(
            before["effective_dispatch_fingerprint_sha256"],
            after["effective_dispatch_fingerprint_sha256"],
        )

    def test_relevant_provider_configuration_changes_identity(self) -> None:
        current = self.root / "agent-tool"
        self.executable(current, "stable binary")
        first = provider_base.LocalProcessProvider(
            self.config([str(current), "--profile", "one"], model="model-a")
        ).effective_dispatch_fingerprint()
        second = provider_base.LocalProcessProvider(
            self.config([str(current), "--profile", "two"], model="model-b")
        ).effective_dispatch_fingerprint()

        self.assertNotEqual(
            first["effective_dispatch_fingerprint_sha256"],
            second["effective_dispatch_fingerprint_sha256"],
        )
        self.assertEqual(first["effective_dispatch_version"], 1)
        self.assertEqual(
            first["effective_dispatch_contract"], "local/effective-dispatch/v1"
        )

    def test_route_context_separates_alias_config_from_effective_path(self) -> None:
        first = self.root / "path-a" / "agent-tool"
        second = self.root / "path-b" / "agent-tool"
        first.parent.mkdir()
        second.parent.mkdir()
        self.executable(first, "first binary")
        self.executable(second, "second binary")
        config = self.config(["agent-tool", "--json"])
        config.data["providers"] = {
            "worker": {
                "kind": "local", "model": "model-a",
                "command": ["agent-tool", "--json"],
            },
        }

        with mock.patch.object(provider_base.shutil, "which", return_value=str(first)):
            _kind, before = chat._route_failure_context(config, "worker")
        with mock.patch.object(provider_base.shutil, "which", return_value=str(second)):
            _kind, after = chat._route_failure_context(config, "worker")

        self.assertEqual(
            before["route_fingerprint_sha256"], after["route_fingerprint_sha256"]
        )
        self.assertNotEqual(
            before["effective_dispatch_fingerprint_sha256"],
            after["effective_dispatch_fingerprint_sha256"],
        )

    def test_web_conversations_share_one_vendor_principal_but_not_other_vendors(self) -> None:
        broker = web_chats.WebChatBroker()
        previous = web_chats.active()
        web_chats.replace_active(broker)
        self.addCleanup(web_chats.replace_active, previous)
        broker.heartbeat([
            {"id": "chatgpt-a1", "provider": "ChatGPT", "title": "A", "url": "https://chatgpt.com/c/a"},
            {"id": "chatgpt-b2", "provider": "ChatGPT", "title": "B", "url": "https://chatgpt.com/c/b"},
            {"id": "gemini-c3", "provider": "Google Gemini", "title": "C", "url": "https://gemini.google.com/app/c"},
        ])
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

        _kind, first = chat._route_failure_context(config, "web:chatgpt-a1")
        _kind, alias = chat._route_failure_context(config, "web:chatgpt-b2")
        _kind, independent = chat._route_failure_context(config, "web:gemini-c3")

        self.assertNotEqual(
            first["effective_dispatch_fingerprint_sha256"],
            alias["effective_dispatch_fingerprint_sha256"],
        )
        self.assertEqual(
            first["provider_principal_fingerprint_sha256"],
            alias["provider_principal_fingerprint_sha256"],
        )
        self.assertNotEqual(
            first["provider_principal_fingerprint_sha256"],
            independent["provider_principal_fingerprint_sha256"],
        )
        self.assertRegex(
            first["provider_principal_fingerprint_sha256"], r"^[0-9a-f]{64}$",
        )

    def test_api_route_aliases_with_the_same_account_slot_are_one_principal(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        shared = {
            "kind": "openai", "model": "gpt-test", "endpoint": "https://api.openai.com/v1",
            "api_key_env": "SHARED_OPENAI_KEY",
        }
        data["providers"] = {"review-a": dict(shared), "review-b": dict(shared)}
        config = LoadedConfig(data, self.root, [], {})

        _kind, first = chat._route_failure_context(config, "review-a")
        _kind, second = chat._route_failure_context(config, "review-b")

        self.assertEqual(
            first["provider_principal_fingerprint_sha256"],
            second["provider_principal_fingerprint_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
