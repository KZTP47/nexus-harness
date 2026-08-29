from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.config import (
    DEFAULT_CONFIG,
    PROJECT_RUNTIME_IGNORE_FOOTER,
    PROJECT_RUNTIME_IGNORE_LINES,
    RESOURCE_LIMIT_MAXIMA,
    SHARED_NON_ESCALATING_LIMITS,
    HarnessError,
    ensure_private_runtime_ignores,
    load_config,
    load_isolated_config,
    trust_project_local_config,
    write_default_project_config,
)
from our_harness.detect import combined_commands, detect_project
from our_harness.providers import create_embedding_provider


class ConfigTests(unittest.TestCase):
    @staticmethod
    def _make_directory_link(link: Path, target: Path) -> None:
        if os.name == "nt":
            made = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if made.returncode != 0:
                raise unittest.SkipTest(
                    "This Windows host cannot create a directory reparse point: "
                    + made.stderr.decode(errors="replace")
                )
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_custom_test_evidence_contract_is_strict_and_requires_local_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / ".harness"
            harness.mkdir()
            contract = {
                "command": ["company-test", "--json"],
                "format": "json-stdout",
                "total_field": "summary.executed",
                "failed_field": "summary.failed",
            }
            shared = harness / "config.json"
            shared.write_text(json.dumps({"project": {"test_evidence_contracts": [contract]}}), encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "test_evidence_contracts.*not been told to trust"):
                load_config(root)
            local = harness / "config.local.json"
            local.write_text(json.dumps({"project": {"test_evidence_contracts": [contract]}}), encoding="utf-8")
            loaded = load_config(root, explicit=local)
            self.assertEqual(loaded.get("project.test_evidence_contracts"), [contract])
            probed = {
                **contract,
                "requirement_probes": {
                    "langgraph_enforcement": "summary.langgraph_enforced",
                },
            }
            valid = json.loads(json.dumps(DEFAULT_CONFIG))
            valid["project"]["test_evidence_contracts"] = [probed]
            from our_harness.config import validate_config
            validate_config(valid)
            broken = json.loads(json.dumps(DEFAULT_CONFIG))
            broken["project"]["test_evidence_contracts"] = [{**contract, "format": "regex"}]
            with self.assertRaisesRegex(HarnessError, "format must be json-stdout"):
                validate_config(broken)

    def test_digest_bound_trust_refuses_changed_or_noncanonical_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            local = root / ".harness" / "config.local.json"
            local.parent.mkdir(parents=True)
            reviewed = b'{"provider":{"name":"ollama"}}'
            local.write_bytes(reviewed)
            trust_store = Path(temporary) / "user" / "trusted-projects.json"
            with patch("our_harness.config.project_trust_store_path", return_value=trust_store):
                with self.assertRaisesRegex(ValueError, "changed after review"):
                    trust_project_local_config(root, local, expected_sha256="0" * 64)
                self.assertFalse(trust_store.exists())
                trust_project_local_config(
                    root, local, expected_sha256=hashlib.sha256(reviewed).hexdigest()
                )
                self.assertTrue(load_config(root))
                outside = root / "reviewed-copy.json"
                outside.write_bytes(reviewed)
                with self.assertRaisesRegex(ValueError, "exact .harness"):
                    trust_project_local_config(
                        root, outside, expected_sha256=hashlib.sha256(reviewed).hexdigest()
                    )

    def test_shipped_local_config_has_no_authority_without_user_trust_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            local = root / ".harness" / "config.local.json"
            local.write_text(
                json.dumps({"plugins": {"enabled": ["shipped"], "paths": ["plugin.py"]}}),
                encoding="utf-8",
            )
            trust_store = root.parent / f"trust-{root.name}.json"
            with patch("our_harness.config.project_trust_store_path", return_value=trust_store):
                with self.assertRaisesRegex(HarnessError, "trusted local"):
                    load_config(root)
                trusted = load_config(root, explicit=local)
                self.assertEqual(trusted.get("plugins.enabled"), ["shipped"])

    def test_user_trust_record_binds_root_and_local_config_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            (root / ".harness").mkdir()
            local = root / ".harness" / "config.local.json"
            local.write_text(
                json.dumps({"project": {"test_commands": [["python", "-m", "pytest"]]}}),
                encoding="utf-8",
            )
            trust_store = Path(temporary) / "user" / "trusted-projects.json"
            with patch("our_harness.config.project_trust_store_path", return_value=trust_store):
                trust_project_local_config(root, local)
                self.assertEqual(load_config(root).get("project.test_commands"), [["python", "-m", "pytest"]])
                local.write_text(
                    json.dumps({"project": {"test_commands": [["python", "evil.py"]]}}), encoding="utf-8"
                )
                with self.assertRaisesRegex(HarnessError, "harness trust"):
                    load_config(root)

    def test_init_creates_out_of_project_trust_record_for_detected_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            root.mkdir()
            trust_store = base / "user-config" / "trusted-projects.json"
            with patch("our_harness.config.project_trust_store_path", return_value=trust_store):
                write_default_project_config(
                    root, "ollama", "coder", "http://127.0.0.1:11434", "",
                    [["python", "-m", "pytest"]],
                )
                self.assertTrue(trust_store.is_file())
                self.assertEqual(load_config(root).get("project.test_commands"), [["python", "-m", "pytest"]])
            self.assertFalse(str(root) in trust_store.read_text(encoding="utf-8"))

    def test_shared_config_cannot_select_primary_or_embedding_remote_authority(self) -> None:
        attempts = [
            {"provider": {"name": "openai"}},
            {"provider": {"name": "anthropic"}},
            {"provider": {"name": "openai-compatible"}},
            {"memory": {"embedding_provider": "openai", "embedding_model": "text-embedding-3-small"}},
            {"memory": {"embedding_model": "nomic-embed-text"}},
        ]
        for layer in attempts:
            with self.subTest(layer=layer), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
                with patch.dict(os.environ, {"HARNESS_API_KEY": "project-must-not-route-this"}, clear=False):
                    with self.assertRaisesRegex(HarnessError, "trusted|shareable|embedding"):
                        load_config(root)

    def test_trusted_embedding_route_can_use_provider_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "memory": {
                            "embedding_provider": "openai",
                            "embedding_model": "text-embedding-3-small",
                            "allow_remote_embeddings": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(root, explicit=root / ".harness" / "config.local.json")
            provider = create_embedding_provider(config)
            self.assertEqual(provider.settings["endpoint"], "https://api.openai.com/v1")
            self.assertEqual(provider.settings["api_key_env"], "OPENAI_API_KEY")

    def test_every_shared_resource_budget_is_non_escalating(self) -> None:
        for dotted in sorted(SHARED_NON_ESCALATING_LIMITS):
            section, name = dotted.split(".", 1)
            default = DEFAULT_CONFIG[section][name]
            maximum = RESOURCE_LIMIT_MAXIMA[dotted]
            if default >= maximum:
                continue
            with self.subTest(dotted=dotted), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.json").write_text(
                    json.dumps({section: {name: default + 1}}), encoding="utf-8"
                )
                with self.assertRaisesRegex(HarnessError, "cannot raise the trusted limit"):
                    load_config(root)

    def test_resource_hard_ceilings_apply_but_trusted_layers_can_raise_defaults(self) -> None:
        representative = (
            "provider.max_output_tokens",
            "project.max_file_bytes",
            "execution.timeout_seconds",
            "execution.max_output_bytes",
            "memory.retention_days",
            "context.max_chars",
            "workflow.max_elapsed_seconds",
            "mcp.max_response_bytes",
        )
        for dotted in representative:
            section, name = dotted.split(".", 1)
            maximum = RESOURCE_LIMIT_MAXIMA[dotted]
            with self.subTest(dotted=dotted), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                local = root / ".harness" / "config.local.json"
                local.write_text(json.dumps({section: {name: maximum}}), encoding="utf-8")
                self.assertEqual(load_config(root, explicit=local).get(dotted), maximum)
                local.write_text(json.dumps({section: {name: maximum + 1}}), encoding="utf-8")
                with self.assertRaisesRegex(HarnessError, "at most"):
                    load_config(root, explicit=local)

    def test_resource_hard_ceilings_match_public_schema(self) -> None:
        schema = json.loads((Path(__file__).resolve().parents[1] / "harness.schema.json").read_text(encoding="utf-8"))
        for dotted, maximum in RESOURCE_LIMIT_MAXIMA.items():
            section, name = dotted.split(".", 1)
            with self.subTest(dotted=dotted):
                self.assertEqual(schema["properties"][section]["properties"][name]["maximum"], maximum)

    def test_review_panel_bounds_and_lens_cardinality_are_validated(self) -> None:
        attempts = [
            ({"workflow": {"reviewers": 0}}, "workflow.reviewers"),
            ({"workflow": {"review_parallelism": 6}}, "workflow.review_parallelism"),
            ({"workflow": {"reviewers": 2, "reviewer_lenses": ["only-one"]}}, "reviewer_lenses"),
            ({"workflow": {"reviewers": 1, "reviewer_lenses": ["bad\npolicy"]}}, "reviewer_lenses"),
        ]
        for layer, message in attempts:
            with self.subTest(layer=layer), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
                with self.assertRaisesRegex(HarnessError, message):
                    load_config(root)

    def test_tool_loop_limits_are_typed_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"workflow": {"max_tool_calls": 0}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarnessError, "workflow.max_tool_calls"):
                load_config(root)
            (root / ".harness" / "config.json").write_text(
                json.dumps({"workflow": {"max_tool_calls": 3, "max_tool_output_bytes": 4096, "max_tool_total_bytes": 2048}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HarnessError, "must not exceed"):
                load_config(root)

    def test_context_tool_execution_time_is_unlimited_by_default_and_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            self.assertEqual(
                load_config(root).get("workflow.context_tool_execution_seconds"), 0
            )
            shared = root / ".harness" / "config.json"
            shared.write_text(json.dumps({
                "workflow": {"context_tool_execution_seconds": 7200}
            }), encoding="utf-8")
            self.assertEqual(
                load_config(root).get("workflow.context_tool_execution_seconds"),
                7200,
            )
            shared.write_text(json.dumps({
                "workflow": {"context_tool_execution_seconds": -1}
            }), encoding="utf-8")
            with self.assertRaisesRegex(
                HarnessError, "workflow.context_tool_execution_seconds"
            ):
                load_config(root)

    def test_project_local_environment_and_cli_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"provider": {"model": "project-model"}, "execution": {"timeout_seconds": 10}}), encoding="utf-8"
            )
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"provider": {"model": "local-model"}}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"HARNESS_MODEL": "env-model", "HARNESS__WORKFLOW__MAX_ITERATIONS": "7"}, clear=False):
                config = load_config(root, cli_overrides={"provider": {"model": "cli-model"}})
            self.assertEqual(config.get("provider.model"), "cli-model")
            self.assertEqual(config.get("execution.timeout_seconds"), 10)
            self.assertEqual(config.get("workflow.max_iterations"), 7)
            self.assertEqual(config.provenance["provider.model"], "command line")

    def test_unknown_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text('{"mystery": true}', encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "Unknown config key"):
                load_config(root)

    def test_memory_enabled_requires_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"memory": {"enabled": "false"}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarnessError, "memory.enabled"):
                load_config(root)

    def test_provider_api_and_cache_options_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            config_path = root / ".harness" / "config.json"
            config_path.write_text(
                json.dumps({"provider": {"api_mode": "responses", "prompt_cache_retention": "in_memory"}}),
                encoding="utf-8",
            )
            config = load_config(root)
            self.assertEqual(config.get("provider.api_mode"), "responses")
            self.assertEqual(config.get("provider.prompt_cache_retention"), "in_memory")

            config_path.write_text(json.dumps({"provider": {"api_mode": "legacy"}}), encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "provider.api_mode"):
                load_config(root)

            config_path.write_text(
                json.dumps({"provider": {"prompt_cache_retention": "forever"}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarnessError, "provider.prompt_cache_retention"):
                load_config(root)

    def test_shared_project_config_cannot_select_credentials_or_remote_endpoint(self) -> None:
        attempts = [
            {"provider": {"api_key_env": "AWS_SECRET_ACCESS_KEY"}},
            {"provider": {"endpoint": "https://collector.example.test/v1"}},
            {"provider": {"command": ["python", "provider.py"]}},
            {
                "mcp": {
                    "servers": [
                        {
                            "name": "remote",
                            "transport": "http",
                            "url": "https://collector.example.test/mcp",
                            "allowed_tools": ["lookup"],
                        }
                    ]
                }
            },
            {
                "mcp": {
                    "servers": [
                        {
                            "name": "local-authority",
                            "transport": "http",
                            "url": "http://127.0.0.1:9912",
                            "allowed_tools": ["lookup"],
                        }
                    ]
                }
            },
        ]
        for layer in attempts:
            with self.subTest(layer=layer), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.json").write_text(json.dumps(layer), encoding="utf-8")
                with self.assertRaisesRegex(HarnessError, "trusted local"):
                    load_config(root)

    def test_capability_trust_is_decided_from_final_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / ".harness"
            harness.mkdir()
            shared = harness / "config.json"
            local = harness / "config.local.json"

            shared.write_text(
                json.dumps(
                    {
                        "provider": {"endpoint": "http://127.0.0.1:9911"},
                        "execution": {"inherit_environment": ["PATH", "AWS_SECRET_ACCESS_KEY"]},
                        "mcp": {
                            "servers": [
                                {
                                    "name": "collector",
                                    "transport": "http",
                                    "url": "https://collector.example.test/mcp",
                                    "allowed_tools": ["search"],
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HarnessError, "execution.inherit_environment"):
                load_config(root)

            local.write_text(
                json.dumps(
                    {
                        "provider": {"endpoint": "http://127.0.0.1:11434"},
                        "execution": {"inherit_environment": ["PATH"]},
                        "mcp": {"servers": []},
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(root, explicit=local)
            # Windows hands out short names for temporary folders, and the
            # harness writes down the long one. Same file, two spellings.
            self.assertEqual(
                Path(config.provenance["provider.endpoint"]).resolve(), local.resolve()
            )
            self.assertEqual(config.get("mcp.servers"), [])

    def test_shared_endpoint_cannot_compose_with_trusted_credential_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            root.mkdir()
            harness = root / ".harness"
            harness.mkdir()
            user = base / "user.json"
            user.write_text(json.dumps({"provider": {"api_key_env": "OPENAI_API_KEY"}}), encoding="utf-8")
            (harness / "config.json").write_text(
                json.dumps(
                    {
                        "provider": {
                            "name": "openai-compatible",
                            "endpoint": "http://127.0.0.1:9911/v1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch("our_harness.config.user_config_path", return_value=user):
                with self.assertRaisesRegex(HarnessError, "provider.endpoint"):
                    load_config(root)

    def test_shared_config_cannot_escalate_docker_or_reduce_policy_denials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            root.mkdir()
            harness = root / ".harness"
            harness.mkdir()
            user = base / "user.json"
            user.write_text(
                json.dumps(
                    {
                        "execution": {
                            "deny_executables": ["format", "diskpart", "shutdown", "reboot", "rm"],
                            "deny_argument_sequences": ["--force", "reset --hard", "clean -fd", "push --force", "checkout --"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            shared = harness / "config.json"
            shared.write_text(
                json.dumps(
                    {
                        "execution": {
                            "mode": "docker",
                            "docker_image": "attacker/image:latest",
                            "docker_network": "host",
                            "deny_executables": [],
                            "deny_argument_sequences": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch("our_harness.config.user_config_path", return_value=user):
                with self.assertRaisesRegex(HarnessError, "Docker execution"):
                    load_config(root)

            shared.write_text(
                json.dumps({"execution": {"deny_executables": [], "deny_argument_sequences": []}}),
                encoding="utf-8",
            )
            with patch("our_harness.config.user_config_path", return_value=user):
                with self.assertRaisesRegex(HarnessError, "deny_executables"):
                    load_config(root)
                with self.assertRaisesRegex(HarnessError, "deny_executables"):
                    load_config(root, explicit=shared)

    def test_shared_config_cannot_weaken_git_or_workflow_safety_policies(self) -> None:
        attacks = (
            ({"git": {"protected_branches": ["main"]}}, "git.protected_branches"),
            ({"git": {"required_branch_prefix": ""}}, "git.required_branch_prefix"),
            ({"git": {"required_branch_prefix": "other/"}}, "git.required_branch_prefix"),
            ({"workflow": {"require_review": False}}, "workflow.require_review"),
            ({"workflow": {"rollback_on_exhaustion": False}}, "workflow.rollback_on_exhaustion"),
        )
        for shared_layer, message in attacks:
            with self.subTest(shared_layer=shared_layer), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "project"
                root.mkdir()
                harness = root / ".harness"
                harness.mkdir()
                user = base / "user.json"
                user.write_text(
                    json.dumps(
                        {
                            "git": {
                                "protected_branches": ["main", "master", "release"],
                                "required_branch_prefix": "codex/",
                            },
                            "workflow": {"require_review": True, "rollback_on_exhaustion": True},
                        }
                    ),
                    encoding="utf-8",
                )
                (harness / "config.json").write_text(json.dumps(shared_layer), encoding="utf-8")
                with patch("our_harness.config.user_config_path", return_value=user):
                    with self.assertRaisesRegex(HarnessError, message):
                        load_config(root)

    def test_trusted_later_layer_may_replace_git_and_workflow_safety_policies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            root.mkdir()
            harness = root / ".harness"
            harness.mkdir()
            user = base / "user.json"
            user.write_text(
                json.dumps(
                    {
                        "git": {
                            "protected_branches": ["main", "master", "release"],
                            "required_branch_prefix": "codex/",
                        },
                        "workflow": {"require_review": True, "rollback_on_exhaustion": True},
                    }
                ),
                encoding="utf-8",
            )
            (harness / "config.json").write_text(
                json.dumps(
                    {
                        "git": {"protected_branches": [], "required_branch_prefix": ""},
                        "workflow": {"require_review": False, "rollback_on_exhaustion": False},
                    }
                ),
                encoding="utf-8",
            )
            local = harness / "config.local.json"
            local.write_text(
                json.dumps(
                    {
                        "git": {"protected_branches": ["main"], "required_branch_prefix": "team/"},
                        "workflow": {"require_review": False, "rollback_on_exhaustion": False},
                    }
                ),
                encoding="utf-8",
            )
            with patch("our_harness.config.user_config_path", return_value=user):
                config = load_config(root, explicit=local)
            self.assertEqual(config.get("git.protected_branches"), ["main"])
            self.assertEqual(config.get("git.required_branch_prefix"), "team/")
            self.assertFalse(config.get("workflow.require_review"))
            self.assertFalse(config.get("workflow.rollback_on_exhaustion"))

    def test_shared_config_may_tighten_git_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            root.mkdir()
            (root / ".harness").mkdir()
            user = base / "user.json"
            user.write_text(
                json.dumps(
                    {"git": {"protected_branches": ["main", "master"], "required_branch_prefix": "codex/"}}
                ),
                encoding="utf-8",
            )
            (root / ".harness" / "config.json").write_text(
                json.dumps(
                    {
                        "git": {
                            "protected_branches": ["main", "master", "release"],
                            "required_branch_prefix": "codex/team/",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch("our_harness.config.user_config_path", return_value=user):
                config = load_config(root)
            self.assertEqual(config.get("git.required_branch_prefix"), "codex/team/")

    def test_isolated_config_overrides_remain_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_isolated_config(
                Path(temporary),
                {
                    "execution": {
                        "mode": "docker",
                        "docker_image": "trusted/image:1",
                        "docker_network": "none",
                        "inherit_environment": ["PATH", "PRIVATE_BENCHMARK_VALUE"],
                        "deny_executables": [],
                        "deny_argument_sequences": [],
                    },
                    "provider": {
                        "name": "local",
                        "endpoint": "http://127.0.0.1:9911",
                        "command": ["python", "scripted_provider.py"],
                    },
                },
            )
            self.assertEqual(config.provenance["execution.mode"], "isolated override")
            self.assertEqual(config.get("provider.command"), ["python", "scripted_provider.py"])

    def test_remote_endpoints_require_https_even_in_trusted_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"provider": {"endpoint": "http://collector.example.test/v1"}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(HarnessError, "HTTPS"):
                load_config(root)

    def test_negative_and_invalid_nested_limits_are_rejected(self) -> None:
        attempts = [
            ({"provider": {"max_output_tokens": -1}}, "provider.max_output_tokens"),
            ({"provider": {"role_output_caps": {"planner": 0}}}, "provider.role_output_caps.planner"),
            ({"providers": {"local": {"kind": "ollama", "model": "qwen", "endpoint": "http://127.0.0.1:11434", "role_output_caps": {"unknown": 10}}}}, "role_output_caps.unknown"),
            ({"memory": {"max_results": 101}}, "memory.max_results"),
            ({"mcp": {"servers": [{"name": "tools", "transport": "pipe"}]}}, "mcp.servers"),
            ({"plugins": {"paths": ["../outside.py"]}}, "plugins.paths"),
        ]
        for layer, message in attempts:
            with self.subTest(layer=layer), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.local.json").write_text(json.dumps(layer), encoding="utf-8")
                with self.assertRaisesRegex(HarnessError, message):
                    load_config(root)

    def test_shared_project_config_cannot_auto_enable_plugin_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            plugin = root / "project_plugin.py"
            sentinel = root / "plugin-ran"
            plugin.write_text(
                "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").write_text('ran')\n",
                encoding="utf-8",
            )
            (root / ".harness" / "config.json").write_text(
                json.dumps({"plugins": {"enabled": ["project_plugin"], "paths": ["project_plugin.py"]}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HarnessError, "trusted local"):
                load_config(root)
            self.assertFalse(sentinel.exists())

    def test_init_config_has_relative_storage_and_detected_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='sample'\nversion='1'\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / ".harness").mkdir()
            (root / ".harness" / ".gitignore").write_text("custom-entry\n", encoding="utf-8")
            detections = detect_project(root)
            with patch("our_harness.config.project_trust_store_path", return_value=root / "test-trust.json"):
                path = write_default_project_config(
                    root, "ollama", "coder", "http://127.0.0.1:11434", "", combined_commands(detections, "test")
                )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["memory"]["database"], ".harness/memory/harness.db")
            self.assertEqual(data["project"]["test_commands"], [])
            local = json.loads((root / ".harness" / "config.local.json").read_text(encoding="utf-8"))
            self.assertEqual(local["provider"]["endpoint"], "http://127.0.0.1:11434")
            self.assertEqual(local["project"]["test_commands"], [["python", "-m", "pytest"]])
            self.assertNotIn(str(root), path.read_text(encoding="utf-8"))
            ignore_text = (root / ".harness" / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("custom-entry", ignore_text)
            self.assertIn("config.local.json", ignore_text)
            self.assertIn("checkpoints/", ignore_text)

    def test_init_gitignore_keeps_runtime_private_and_definitions_trackable(self) -> None:
        """Every known in-project writer is classified on a fresh checkout."""

        private = (
            "config.local.json",
            "project-authority.json",
            "memory/harness.db",
            "runs/run.json",
            "backups/tx/manifest.json",
            "checkpoints/resume.zip",
            "cache/provider.json",
            "runtime/resident.sqlite3",
            "bundles/evidence.zip",
            "chats/agent.json",
            "pages/shared.json",
            "vault/lesson.md",
            "swarm-mutation-sagas/saga.json",
            "transaction.lock",
            "desktop-deployment.lock",
            "desktop-deployment.owner.json",
            "qa/runs/run/result.json",
            "qa/tmp/browser.js",
            "qa/history.json",
            "qa/candidates.json",
            "qa/pipeline-preservation.lock",
            "qa/pipelines-before-checks/tx/journal.json",
            "qa/final-swarm-report.json",
            "pipelines/last-run.json",
            "pipelines/evidence/run/assistant-output.json",
            "pipelines/drafts/generated.json",
            "timers/.what-happened.json",
            "timers/what-happened.json",
            "timers/.what-happened.json.could-not-be-read",
            "timers/running.lock",
            "pipelines/nightly.json.123.part",
            "workflows/release.tmp",
        )
        shared = (
            "config.json",
            "project.json",
            "qa/suite.json",
            "qa/workflows.json",
            "qa/environments.json",
            "qa/baselines/home.png",
            "pipelines/nightly.json",
            "pipelines/before/nightly.json",
            "timers/nightly.json",
            "telling/ops.json",
            "workflows/release.json",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", str(root)], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            with patch(
                "our_harness.config.project_trust_store_path",
                return_value=root / "test-trust.json",
            ):
                write_default_project_config(
                    root, "ollama", "coder", "http://127.0.0.1:11434",
                )

            for relative in (*private, *shared):
                path = root / ".harness" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("private or shared fixture\n", encoding="utf-8")

            def is_ignored(relative: str) -> bool:
                checked = subprocess.run(
                    [
                        "git", "-C", str(root), "check-ignore", "--quiet",
                        "--no-index", "--", f".harness/{relative}",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertIn(checked.returncode, (0, 1), checked.stderr.decode(errors="replace"))
                return checked.returncode == 0

            for relative in private:
                with self.subTest(private=relative):
                    self.assertTrue(is_ignored(relative))
            for relative in shared:
                with self.subTest(shared=relative):
                    self.assertFalse(is_ignored(relative))

    def test_init_reasserts_private_rule_after_existing_negation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / ".harness"
            harness.mkdir()
            (harness / ".gitignore").write_text(
                "custom-entry\npipelines/evidence/\n!pipelines/evidence/\n",
                encoding="utf-8",
            )
            with patch(
                "our_harness.config.project_trust_store_path",
                return_value=root / "test-trust.json",
            ):
                write_default_project_config(
                    root, "ollama", "coder", "http://127.0.0.1:11434",
                )
            lines = (harness / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[-1], PROJECT_RUNTIME_IGNORE_FOOTER)
            self.assertGreater(
                max(index for index, line in enumerate(lines) if line == "pipelines/evidence/"),
                max(index for index, line in enumerate(lines) if line == "!pipelines/evidence/"),
            )

    def test_loading_existing_project_migrates_ignore_append_only_and_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / ".harness"
            harness.mkdir()
            config_path = harness / "config.json"
            config_path.write_text("{}\n", encoding="utf-8")
            ignore_path = harness / ".gitignore"
            original = (
                "# Keep this project-specific comment\n"
                "custom-private-folder/\n"
                "pipelines/evidence/\n"
                "!pipelines/evidence/\n"
            )
            ignore_path.write_text(original, encoding="utf-8")
            missing_user = root / "missing-user-config.json"
            with (
                patch("our_harness.config.user_config_path", return_value=missing_user),
                patch(
                    "our_harness.config.project_trust_store_path",
                    return_value=root / "missing-trust.json",
                ),
            ):
                loaded = load_config(root)
                self.assertEqual(loaded.project_root, root.resolve())
                first = ignore_path.read_text(encoding="utf-8")
                self.assertTrue(first.startswith(original))
                self.assertIn("custom-private-folder/", first)
                self.assertGreater(
                    first.rfind("pipelines/evidence/"),
                    first.rfind("!pipelines/evidence/"),
                )
                self.assertEqual(config_path.read_text(encoding="utf-8"), "{}\n")
                load_config(root)
                self.assertEqual(ignore_path.read_text(encoding="utf-8"), first)

    def test_existing_project_migration_rejects_linked_harness_before_any_io(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            external = base / "external-harness"
            root.mkdir()
            external.mkdir()
            sentinel = external / ".gitignore"
            original = b"EXTERNAL SENTINEL\x00must stay byte-identical\n"
            sentinel.write_bytes(original)
            (external / "config.json").write_text("{}\n", encoding="utf-8")
            self._make_directory_link(root / ".harness", external)

            with self.assertRaisesRegex(HarnessError, "Linked path components"):
                load_config(root)
            self.assertEqual(sentinel.read_bytes(), original)
            self.assertFalse(list(external.glob(".gitignore.*.part")))
            with self.assertRaisesRegex(HarnessError, "Linked path components"):
                write_default_project_config(
                    root, "ollama", "coder", "http://127.0.0.1:11434",
                )
            self.assertEqual(sentinel.read_bytes(), original)
            self.assertFalse(list(external.glob(".gitignore.*.part")))

    def test_existing_project_migration_rejects_linked_ignore_before_read_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            harness = root / ".harness"
            external = base / "external-ignore"
            harness.mkdir(parents=True)
            external.mkdir()
            (harness / "config.json").write_text("{}\n", encoding="utf-8")
            sentinel = external / "sentinel.bin"
            original = b"EXTERNAL IGNORE SENTINEL\x00must stay byte-identical\n"
            sentinel.write_bytes(original)
            linked_ignore = harness / ".gitignore"
            if os.name == "nt":
                # A junction is a Windows reparse point and does not require
                # Developer Mode or symlink privileges on supported hosts.
                self._make_directory_link(linked_ignore, external)
            else:
                linked_ignore.symlink_to(sentinel)

            with self.assertRaisesRegex(HarnessError, "Linked path components"):
                load_config(root)
            self.assertEqual(sentinel.read_bytes(), original)
            self.assertTrue(linked_ignore.is_symlink() or linked_ignore.exists())
            self.assertFalse(list(harness.glob(".gitignore.*.part")))

    def test_ignore_migration_can_be_called_directly_for_an_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            path = ensure_private_runtime_ignores(root)
            self.assertEqual(path, root / ".harness" / ".gitignore")
            self.assertEqual(
                set(PROJECT_RUNTIME_IGNORE_LINES)
                - set(path.read_text(encoding="utf-8").splitlines()),
                set(),
            )

    def test_repository_gitignore_matches_the_init_privacy_contract(self) -> None:
        repository_ignore = (
            Path(__file__).resolve().parents[1] / ".harness" / ".gitignore"
        ).read_text(encoding="utf-8").splitlines()
        for entry in PROJECT_RUNTIME_IGNORE_LINES:
            with self.subTest(entry=entry):
                self.assertIn(entry, repository_ignore)

    def test_init_keeps_remote_provider_route_only_in_ignored_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trust_store = root / "test-trust.json"
            with patch("our_harness.config.project_trust_store_path", return_value=trust_store):
                path = write_default_project_config(
                    root,
                    "openai",
                    "gpt-5",
                    "https://api.openai.com/v1",
                    "OPENAI_API_KEY",
                )
            shared = json.loads(path.read_text(encoding="utf-8"))
            local = json.loads((root / ".harness" / "config.local.json").read_text(encoding="utf-8"))
            self.assertEqual(shared["provider"]["name"], "ollama")
            self.assertEqual(local["provider"]["name"], "openai")
            self.assertEqual(local["provider"]["model"], "gpt-5")
            self.assertEqual(local["provider"]["endpoint"], "https://api.openai.com/v1")
            with patch("our_harness.config.project_trust_store_path", return_value=trust_store):
                self.assertEqual(load_config(root).get("provider.name"), "openai")

    def test_shared_config_cannot_lower_trusted_review_or_git_policy(self) -> None:
        attempts = [
            {"workflow": {"reviewers": 2}},
            {"workflow": {"review_parallelism": 1}},
            {"git": {"enabled": False}},
            {"workflow": {"name": "project-plugin-policy"}},
        ]
        trusted = {
            "workflow": {"reviewers": 3, "review_parallelism": 2, "require_review": True},
            "git": {"enabled": True},
        }
        for project_layer in attempts:
            with self.subTest(layer=project_layer), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                trusted_path = root / "user-config.json"
                trusted_path.write_text(json.dumps(trusted), encoding="utf-8")
                (root / ".harness" / "config.json").write_text(json.dumps(project_layer), encoding="utf-8")
                with patch("our_harness.config.user_config_path", return_value=trusted_path):
                    with self.assertRaisesRegex(HarnessError, "trusted|shareable|review|Git|workflow"):
                        load_config(root)

    def test_project_commands_require_trusted_local_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            command_layer = {"project": {"test_commands": [["python", "-m", "pytest"]]}}
            (root / ".harness" / "config.json").write_text(json.dumps(command_layer), encoding="utf-8")
            with self.assertRaisesRegex(HarnessError, "command|trusted"):
                load_config(root)
            (root / ".harness" / "config.json").write_text("{}", encoding="utf-8")
            (root / ".harness" / "config.local.json").write_text(json.dumps(command_layer), encoding="utf-8")
            self.assertEqual(load_config(root, explicit=root / ".harness" / "config.local.json").get("project.test_commands"), [["python", "-m", "pytest"]])


class DetectionTests(unittest.TestCase):
    def test_go_detection_requests_machine_readable_test_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "go.mod").write_text("module example\n", encoding="utf-8")
            stacks = detect_project(root)
            self.assertEqual(combined_commands(stacks, "test"), [["go", "test", "-json", "./..."]])

    def test_polyglot_detection_uses_manifest_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "lint": "eslint ."}}), encoding="utf-8")
            (root / "tsconfig.json").write_text("{}", encoding="utf-8")
            (root / "go.mod").write_text("module sample\n", encoding="utf-8")
            stacks = detect_project(root)
            self.assertEqual([item.stack for item in stacks], ["typescript", "go"])
            self.assertIn(["npm", "run", "test"], combined_commands(stacks, "test"))
            self.assertIn(["go", "test", "-json", "./..."], combined_commands(stacks, "test"))

    def test_a_beginner_project_with_only_test_files_is_still_python(self) -> None:
        """Someone learning has a couple of files and a test, and no packaging file."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (root / "test_calc.py").write_text("import unittest\n", encoding="utf-8")
            stacks = detect_project(root)
            self.assertEqual([item.stack for item in stacks], ["python"])
            self.assertEqual(stacks[0].evidence, ["test_calc.py"])
            self.assertEqual(combined_commands(stacks, "test"), [["python", "-m", "unittest", "discover"]])

    def test_tests_in_a_tests_folder_are_found_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests" / "calc_test.py").write_text("import unittest\n", encoding="utf-8")
            stacks = detect_project(root)
            self.assertEqual([item.stack for item in stacks], ["python"])
            self.assertEqual(stacks[0].evidence, ["tests/calc_test.py"])

    def test_a_packaging_file_still_wins_over_the_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (root / "test_calc.py").write_text("import unittest\n", encoding="utf-8")
            stacks = detect_project(root)
            self.assertEqual([item.stack for item in stacks], ["python"])
            self.assertEqual(stacks[0].evidence, ["pyproject.toml"])
            self.assertGreater(stacks[0].confidence, 0.6)

    def test_python_files_without_any_test_are_still_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            self.assertEqual([item.stack for item in detect_project(root)], ["unknown"])

    def test_the_listed_evidence_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(30):
                (root / f"test_{index}.py").write_text("import unittest\n", encoding="utf-8")
            stacks = detect_project(root)
            self.assertLessEqual(len(stacks[0].evidence), 8)


if __name__ == "__main__":
    unittest.main()
