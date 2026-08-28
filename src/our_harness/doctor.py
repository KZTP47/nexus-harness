from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import LoadedConfig, is_project_local_config_trusted
from .detect import combined_commands, detect_project
from .plugins import load_plugins
from .providers import ProviderRegistry, codex_cli_preflight


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    message: str


def _provider_check(config: LoadedConfig) -> Check:
    name = config.get("provider.name")
    key_env = config.get("provider.api_key_env")
    if key_env and not os.environ.get(key_env) and not os.environ.get("HARNESS_API_KEY"):
        return Check("fail", "provider", f"{key_env} is not set")
    if name == "local":
        command = config.get("provider.command", [])
        if not command or not shutil.which(command[0]):
            return Check("fail", "provider", "The local provider command is not available")
        return Check("ok", "provider", f"Local provider command found: {command[0]}")
    endpoint = str(config.get("provider.endpoint")).rstrip("/")
    if name == "ollama":
        try:
            with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=2) as response:
                if response.status == 200:
                    return Check("ok", "provider", f"Ollama is reachable at {endpoint}")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            return Check("warn", "provider", f"Ollama is not reachable at {endpoint}")
    return Check("ok", "provider", f"Provider configuration is present: {name}")


def _codex_profile_checks(config: LoadedConfig) -> list[Check]:
    checks: list[Check] = []
    registry = ProviderRegistry(config)
    for profile in registry.profiles():
        if profile.name != "codex-cli":
            continue
        try:
            version, _status = codex_cli_preflight(
                list(profile.command),
                auth_mode=profile.auth_mode,
                timeout_seconds=min(10, profile.timeout_seconds),
                model=profile.model,
            )
        except Exception as exc:
            checks.append(Check("fail", f"provider:{profile.id}", str(exc)))
        else:
            checks.append(
                Check(
                    "ok",
                    f"provider:{profile.id}",
                    f"Codex CLI is executable and signed in with ChatGPT ({version or 'version reported'})",
                )
            )
    return checks


def _subscription_profile_checks(config: LoadedConfig) -> list[Check]:
    """Verify named command-line routes, not merely the legacy default route."""

    from .providers.subscription_cli import SUBSCRIPTION_KINDS, connection_status

    checks: list[Check] = []
    for profile in ProviderRegistry(config).profiles():
        if profile.name not in SUBSCRIPTION_KINDS or profile.name == "codex-cli":
            continue
        try:
            status = connection_status(
                profile.name,
                timeout_seconds=min(10, profile.timeout_seconds),
                use_cache=True,
                probe=True,
                command=profile.command or None,
            )
        except Exception as exc:
            checks.append(Check("fail", f"provider:{profile.id}", str(exc)))
            continue
        authentication = str(status.get("authentication") or "unknown")
        if not status.get("installed"):
            checks.append(Check("fail", f"provider:{profile.id}", "The configured command is not installed"))
        elif authentication == "signed-out":
            checks.append(Check("fail", f"provider:{profile.id}", "The command is installed but signed out"))
        elif authentication == "signed-in":
            checks.append(Check("ok", f"provider:{profile.id}", "The command is installed and its sign-in is ready"))
        else:
            checks.append(Check(
                "warn", f"provider:{profile.id}",
                "The command is installed, but it has no safe local sign-in check; its first small request is the final readiness test",
            ))
    return checks


def run_doctor(config: LoadedConfig) -> dict[str, Any]:
    checks: list[Check] = []
    version_ok = sys.version_info >= (3, 11)
    checks.append(Check("ok" if version_ok else "fail", "python", f"Python {sys.version.split()[0]}"))
    checks.append(Check("ok", "config", f"Config schema {config.get('schema_version')}; {len(config.sources)} file layer(s)"))
    local_config = config.project_root / ".harness" / "config.local.json"
    if local_config.is_file() and not is_project_local_config_trusted(config.project_root, local_config):
        checks.append(
            Check(
                "warn",
                "local_config_trust",
                "config.local.json has no matching user trust record. Run harness init again or pass it with --config after review.",
            )
        )
    project_source = str((config.project_root / ".harness" / "config.json").resolve())
    project_values = sum(source == project_source for source in config.provenance.values())
    checks.append(
        Check(
            "ok",
            "capability_trust",
            f"Effective executable capabilities passed final provenance checks; {project_values} value(s) come from shareable project config",
        )
    )
    checks.append(Check("ok" if config.project_root.is_dir() else "fail", "project", str(config.project_root)))
    checks.append(
        Check(
            "ok",
            "memory",
            (
                f"Persistent source memory is enabled at {config.get('memory.database')}"
                if config.get("memory.enabled")
                else "Memory is disabled: source indexing, retrieval, episodes, refinements, review packets, and run history are not retained; current run state is process-local."
            ),
        )
    )
    checks.append(_provider_check(config))
    checks.extend(_codex_profile_checks(config))
    checks.extend(_subscription_profile_checks(config))
    try:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
        db.close()
        checks.append(Check("ok", "sqlite", "SQLite and FTS5 are available"))
    except sqlite3.Error:
        checks.append(Check("warn", "sqlite", "SQLite is available without FTS5; memory search will use substring matching"))
    detections = detect_project(config.project_root)
    checks.append(Check("ok" if detections[0].stack != "unknown" else "warn", "stack", ", ".join(item.stack for item in detections)))
    commands = combined_commands(detections, "test")
    if config.get("project.test_commands"):
        commands = config.get("project.test_commands")
    missing = sorted({command[0] for command in commands if command and not shutil.which(command[0]) and not (config.project_root / command[0]).exists()})
    if missing:
        checks.append(Check("fail", "test_tools", f"Missing executables: {', '.join(missing)}"))
    elif commands:
        checks.append(Check("ok", "test_tools", f"Detected {len(commands)} test command(s)"))
    else:
        checks.append(Check("warn", "test_tools", "No test command was detected; set project.test_commands"))
    if config.get("workflow.name") == "gauntlet":
        for kind in ("security", "performance"):
            configured = list(config.get(f"project.{kind}_commands", []))
            missing_tools = sorted(
                {
                    command[0]
                    for command in configured
                    if command and not shutil.which(command[0]) and not (config.project_root / command[0]).exists()
                }
            )
            if not configured:
                checks.append(Check("fail", f"{kind}_tools", f"Gauntlet requires project.{kind}_commands"))
            elif missing_tools:
                checks.append(Check("fail", f"{kind}_tools", f"Missing executables: {', '.join(missing_tools)}"))
            else:
                checks.append(Check("ok", f"{kind}_tools", f"Configured {len(configured)} {kind} command(s)"))
    if config.get("execution.mode") == "docker":
        checks.append(Check("ok" if shutil.which("docker") else "fail", "docker", "Docker execution selected"))
    git_level = "ok" if shutil.which("git") else "warn"
    checks.append(Check(git_level, "git", "Git found" if git_level == "ok" else "Git not found"))
    for server in config.get("mcp.servers", []):
        if server.get("transport", "stdio") == "stdio":
            command = server.get("command", "")
            checks.append(Check("ok" if shutil.which(command) else "fail", f"mcp:{server.get('name', command)}", f"Command: {command}"))
    registry = load_plugins(config)
    for callback in registry.doctor_checks:
        value = callback(config)
        if isinstance(value, Check):
            checks.append(value)
        elif isinstance(value, dict) and all(isinstance(value.get(key), str) for key in ("level", "name", "message")):
            checks.append(Check(value["level"], value["name"], value["message"]))
        else:
            checks.append(Check("fail", "plugin", "A plugin doctor check returned an invalid result"))
    levels = [item.level for item in checks]
    exit_code = 2 if "fail" in levels else 1 if "warn" in levels else 0
    return {"exit_code": exit_code, "checks": [asdict(item) for item in checks], "provenance": config.provenance}
