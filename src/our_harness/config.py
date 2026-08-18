from __future__ import annotations

import copy
import datetime
import fnmatch
import hashlib
import json
import math
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import HarnessError
from .safety import put_this_file_in_place, read_this_file_patiently


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "provider": {
        "name": "ollama",
        "model": "qwen2.5-coder:7b",
        "endpoint": "http://127.0.0.1:11434",
        "api_key_env": "",
        "api_mode": "auto",
        "prompt_cache_key": "",
        "prompt_cache_retention": "",
        "temperature": 0.2,
        "max_output_tokens": 8192,
        "role_output_caps": {
            "planner": 1_000_000,
            "coder": 1_000_000,
            "evaluator": 1_000_000,
            "merge": 1_000_000,
        },
        "timeout_seconds": 180,
        "command": [],
    },
    "providers": {},
    "agents": {},
    "pricing": {"allow_unpriced_remote_calls": False, "snapshots": []},
    "project": {
        "standards_files": ["AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "README.md"],
        "ignore": [
            ".git", ".harness", ".venv", "venv", "node_modules", "dist", "build",
            "target", "vendor", "coverage", "__pycache__",
        ],
        "max_file_bytes": 1_000_000,
        "test_commands": [],
        "lint_commands": [],
        "build_commands": [],
        "security_commands": [],
        "performance_commands": [],
    },
    "execution": {
        "mode": "process",
        "timeout_seconds": 180,
        "max_output_bytes": 250_000,
        "max_changed_files": 24,
        "max_changed_bytes": 2_000_000,
        # Real toolchains need these to find their own installs: Python resolves
        # per-user site-packages through APPDATA, npm and git read the profile
        # folders, and every one of them is a path, not a secret.
        "inherit_environment": [
            "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LANG", "LC_ALL",
            "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
        ],
        # A project may add to this list but never take from it. More names
        # that are never a coding job, such as mkfs and Format-Volume, are
        # refused by the runner itself; see ALWAYS_DENIED in execution.py.
        "deny_executables": ["format", "diskpart", "shutdown", "reboot"],
        "deny_argument_sequences": ["--force", "reset --hard", "clean -fd", "push --force"],
        "docker_image": "python:3.12-slim",
        "docker_network": "none",
    },
    "git": {
        "enabled": True,
        "allow_commit": False,
        "allow_push": False,
        "allow_merge": False,
        "protected_branches": ["main", "master"],
        "required_branch_prefix": "",
    },
    "memory": {
        "enabled": True,
        "database": ".harness/memory/harness.db",
        "max_results": 8,
        "retention_days": 180,
        "embedding_provider": "",
        "embedding_model": "",
        "allow_remote_embeddings": False,
    },
    "context": {
        "max_chars": 120_000,
        "reserve_chars": 20_000,
        "recent_event_chars": 24_000,
        "memory_chars": 20_000,
        "workspace_chars": 50_000,
    },
    "workflow": {
        "name": "planner-coder-reviewer",
        "max_iterations": 4,
        "max_elapsed_seconds": 1800,
        "repeat_failure_limit": 2,
        "max_tool_calls": 12,
        "max_tool_output_bytes": 32_000,
        "max_tool_total_bytes": 128_000,
        "reviewers": 1,
        "review_parallelism": 1,
        "reviewer_lenses": [],
        "temperature_decay": 0.75,
        "rollback_on_exhaustion": True,
        "require_review": True,
        "require_executable_counterexamples": False,
    },
    "mcp": {"servers": [], "max_response_bytes": 1_000_000, "timeout_seconds": 60},
    "ui": {"host": "127.0.0.1", "port": 8765, "open_browser": True},
    "qa": {
        "suite": ".harness/qa/suite.json",
        "workers": 4,
        "default_timeout_seconds": 120,
        "artifacts_dir": ".harness/qa/runs",
        "keep_runs": 20,
        "max_evidence_chars": 4000,
        "max_response_bytes": 1_000_000,
        "allow_hosts": ["127.0.0.1", "localhost", "::1"],
        "flaky_min_runs": 5,
        "flaky_threshold": 0.2,
    },
    "plugins": {"enabled": [], "paths": []},
}


CREDENTIAL_PROVIDER_NAMES = frozenset({"openai", "openai-compatible", "anthropic", "gemini"})

# These ceilings are deliberately well above the defaults so trusted local
# configuration can support large projects without allowing unbounded integer
# values to disable the harness' memory, output, and deadline controls.
RESOURCE_LIMIT_MAXIMA: dict[str, int] = {
    "provider.max_output_tokens": 1_000_000,
    "provider.timeout_seconds": 3_600,
    "project.max_file_bytes": 100_000_000,
    "execution.timeout_seconds": 3_600,
    "execution.max_output_bytes": 100_000_000,
    "execution.max_changed_files": 10_000,
    "execution.max_changed_bytes": 1_000_000_000,
    "memory.max_results": 100,
    "memory.retention_days": 3_650,
    "context.max_chars": 10_000_000,
    "context.reserve_chars": 10_000_000,
    "context.recent_event_chars": 10_000_000,
    "context.memory_chars": 10_000_000,
    "context.workspace_chars": 10_000_000,
    "workflow.max_iterations": 100,
    "workflow.max_elapsed_seconds": 86_400,
    "workflow.repeat_failure_limit": 100,
    "workflow.max_tool_calls": 100,
    "workflow.max_tool_output_bytes": 2_000_000,
    "workflow.max_tool_total_bytes": 20_000_000,
    "workflow.reviewers": 5,
    "workflow.review_parallelism": 5,
    "mcp.max_response_bytes": 100_000_000,
    "mcp.timeout_seconds": 3_600,
    "qa.workers": 32,
    "qa.default_timeout_seconds": 3_600,
    "qa.keep_runs": 1_000,
    "qa.max_evidence_chars": 1_000_000,
    "qa.max_response_bytes": 100_000_000,
    "qa.flaky_min_runs": 1_000,
}

# A checked-in project may tighten these budgets, but only a trusted layer may
# expand them beyond the default/user policy that existed before the project
# layer was merged.
SHARED_NON_ESCALATING_LIMITS = frozenset(
    key
    for key in RESOURCE_LIMIT_MAXIMA
    # Reviewer cardinality is a declared workflow shape, not a caller-sized
    # byte/deadline/tool budget. It remains subject to the global hard cap.
    if key not in {"workflow.reviewers", "workflow.review_parallelism"}
)


@dataclass(frozen=True)
class LoadedConfig:
    data: dict[str, Any]
    project_root: Path
    sources: list[Path]
    provenance: dict[str, str]
    trusted_floor: dict[str, Any] | None = None

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.data
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def user_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "our-harness" / "config.json"


def project_trust_store_path() -> Path:
    return user_config_path().parent / "trusted-projects.json"


def _project_trust_key(root: Path) -> str:
    value = str(root.resolve())
    if os.name == "nt":
        value = value.casefold()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_project_local_config_trusted(root: Path, local_path: Path | None = None) -> bool:
    path = (local_path or root / ".harness" / "config.local.json").resolve()
    if not path.is_file():
        return False
    store_path = project_trust_store_path()
    try:
        store = json.loads(store_path.read_text(encoding="utf-8"))
        record = store.get("projects", {}).get(_project_trust_key(root), {})
        metadata = path.stat()
        return (
            store.get("schema_version") == 1
            and record.get("config_sha256") == _file_sha256(path)
            and record.get("config_size") == metadata.st_size
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def is_project_shared_config_trusted(root: Path) -> bool:
    """Whether somebody has said the shared settings file in this project is theirs.

    A settings file that travels with a repository can name the commands the
    harness may run, so nothing reads those until a person says the file is
    theirs. Until this existed there was no way to say it: cloning a project
    whose shared file named its own test command left you told to trust a file,
    and told there was no file to trust.
    """

    path = (root / ".harness" / "config.json").resolve()
    if not path.is_file():
        return False
    try:
        store = json.loads(project_trust_store_path().read_text(encoding="utf-8"))
        record = store.get("projects", {}).get(_project_trust_key(root), {})
        metadata = path.stat()
        return (
            store.get("schema_version") == 1
            and record.get("shared_sha256") == _file_sha256(path)
            and record.get("shared_size") == metadata.st_size
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def trust_project_local_config(root: Path, local_path: Path | None = None) -> Path:
    resolved_root = root.resolve()
    path = (local_path or resolved_root / ".harness" / "config.local.json").resolve(strict=True)
    metadata = path.stat()
    store_path = project_trust_store_path()
    try:
        store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.is_file() else {}
    except (OSError, ValueError, TypeError):
        store = {}
    if not isinstance(store, dict):
        store = {}
    projects = store.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    kept = projects.get(_project_trust_key(resolved_root))
    kept = dict(kept) if isinstance(kept, dict) else {}
    shared = resolved_root / ".harness" / "config.json"
    if path == shared.resolve():
        kept["shared_sha256"] = _file_sha256(path)
        kept["shared_size"] = metadata.st_size
    else:
        kept["config_sha256"] = _file_sha256(path)
        kept["config_size"] = metadata.st_size
    projects[_project_trust_key(resolved_root)] = kept
    store = {"schema_version": 1, "projects": projects}
    store_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = store_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, store_path)
    return store_path


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    markers = (".git", ".harness", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml")
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return current


def _merge(base: dict[str, Any], incoming: dict[str, Any], provenance: dict[str, str], source: str, prefix: str = "") -> None:
    for key, value in incoming.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if prefix in {"providers", "agents"} and isinstance(value, dict) and key not in base:
            base[key] = {}
            _merge(base[key], value, provenance, source, dotted)
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value, provenance, source, dotted)
        else:
            base[key] = copy.deepcopy(value)
            provenance[dotted] = source


def _read_json(path: Path) -> dict[str, Any]:
    try:
        # Patiently: the panel writes this file while everything else is
        # reading it, and on Windows a reader loses that race as easily as a
        # writer does.
        value = json.loads(read_this_file_patiently(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Cannot read config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"Config root must be an object: {path}")
    return value


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _environment_layer() -> dict[str, Any]:
    result: dict[str, Any] = {}
    aliases = {
        "HARNESS_PROVIDER": ("provider", "name"),
        "HARNESS_MODEL": ("provider", "model"),
        "HARNESS_ENDPOINT": ("provider", "endpoint"),
        "HARNESS_TIMEOUT_SECONDS": ("execution", "timeout_seconds"),
    }
    for name, raw in os.environ.items():
        if name in aliases:
            parts = aliases[name]
        elif name.startswith("HARNESS__"):
            parts = tuple(part.lower() for part in name[9:].split("__") if part)
        else:
            continue
        if not parts:
            continue
        cursor = result
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _coerce(raw)
    return result


def _check_known_keys(value: dict[str, Any], defaults: dict[str, Any], prefix: str = "") -> None:
    for key, child in value.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if key not in defaults:
            raise HarnessError(f"Unknown config key: {dotted}")
        if isinstance(defaults[key], dict) and not isinstance(child, dict):
            raise HarnessError(f"Config key must be an object: {dotted}")
        if isinstance(child, dict):
            if not isinstance(defaults[key], dict):
                raise HarnessError(f"Config key must not be an object: {dotted}")
            if dotted not in {"providers", "agents"}:
                _check_known_keys(child, defaults[key], dotted)


def _is_loopback_endpoint(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _validate_endpoint(value: object, name: str = "provider.endpoint") -> None:
    if not isinstance(value, str) or not value:
        raise HarnessError(f"{name} must be a non-empty URL")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise HarnessError(f"{name} must be a valid URL") from exc
    if not parsed.scheme or not parsed.hostname:
        raise HarnessError(f"{name} must be a valid URL")
    if parsed.scheme != "https" and not _is_loopback_endpoint(value):
        raise HarnessError(f"Remote {name} URLs must use HTTPS")
    # A name and password written into the address itself. Every named route
    # was checked for this and the plain one was not, so the one setting a
    # person makes without knowing about routes was the one that let a password
    # through - into the settings file, and from there onto the screen the
    # first time anything went wrong.
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HarnessError(
            f"{name} must not contain a name and password, a query, or a #part. "
            "Put the key in an environment variable and name it in api_key_env."
        )


def _same_source(provenance: dict[str, str], key: str, source: str) -> bool:
    return provenance.get(key) == source


def _normalized_policy(values: object) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip().casefold() for value in values}


def _validate_embedding_route(
    data: dict[str, Any],
    provenance: dict[str, str],
    shared_source: str,
    trusted_floor: dict[str, Any] | None = None,
) -> None:
    def project_controls(key: str) -> bool:
        return _same_source(provenance, key, shared_source)

    def project_changed(key: str) -> bool:
        if not project_controls(key):
            return False
        if trusted_floor is None:
            return True
        section, name = key.split(".", 1)
        return data[section][name] != trusted_floor[section][name]

    provider = data["provider"]
    embedding_provider = str(data["memory"]["embedding_provider"] or "")
    embedding_model = str(data["memory"]["embedding_model"] or "")
    if not embedding_model:
        return
    if project_changed("memory.embedding_model"):
        raise HarnessError(
            "memory.embedding_model cannot enable source transmission from shareable project config; "
            "move embedding configuration to a trusted layer"
        )
    if embedding_provider and project_changed("memory.embedding_provider"):
        raise HarnessError(
            "memory.embedding_provider cannot select an embedding route from shareable project config; "
            "move embedding configuration to a trusted layer"
        )
    requested_embedding_provider = embedding_provider or str(provider["name"])
    if not embedding_provider and project_changed("provider.name"):
        raise HarnessError("provider.name cannot select the effective embedding route from shareable project config")
    if requested_embedding_provider == provider["name"] and project_changed("provider.endpoint"):
        raise HarnessError("provider.endpoint cannot select the effective embedding route from shareable project config")


def validate_embedding_provider_route(config: LoadedConfig) -> None:
    """Recheck derived embedding authority at the provider factory boundary."""
    shared_source = str((config.project_root / ".harness" / "config.json").resolve())
    _validate_embedding_route(config.data, config.provenance, shared_source, config.trusted_floor)
    if not config.get("memory.embedding_model"):
        return
    requested = str(config.get("memory.embedding_provider") or config.get("provider.name"))
    current = str(config.get("provider.name"))
    if requested == current:
        endpoint = str(config.get("provider.endpoint"))
    else:
        endpoint = {
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "ollama": "http://127.0.0.1:11434",
            "openai-compatible": "http://127.0.0.1:8000/v1",
        }.get(requested, "https://invalid.invalid")
    if not _is_loopback_endpoint(endpoint) and not config.get("memory.allow_remote_embeddings"):
        raise HarnessError(
            "Remote embeddings require memory.allow_remote_embeddings=true in trusted configuration"
        )


def _validate_capability_provenance(
    data: dict[str, Any],
    provenance: dict[str, str],
    shared_source: str,
    trusted_floor: dict[str, Any],
) -> None:
    """Reject executable authority that survives from shareable project config.

    Validation happens after every layer is merged. A trusted local, user,
    environment, explicit, or command-line value can therefore replace an
    unsafe project value. This also catches capabilities assembled from more
    than one layer, such as a trusted credential name plus a project-selected
    endpoint.
    """

    def project_controls(key: str) -> bool:
        return _same_source(provenance, key, shared_source)

    def project_changed(key: str) -> bool:
        if not project_controls(key):
            return False
        section, name = key.split(".", 1)
        return data[section][name] != trusted_floor[section][name]

    def nested(value: object, dotted: str) -> object:
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    for dotted, source in provenance.items():
        if (
            source == shared_source
            and dotted.split(".", 1)[0] in {"providers", "agents", "pricing"}
            and nested(data, dotted) != nested(trusted_floor, dotted)
        ):
            raise HarnessError(
                f"{dotted} is set in a settings file this machine has not been told "
                "to trust. A setting like that can start a program, so nothing reads "
                "it until somebody says the file is theirs. Read the file, then run: "
                "harness trust"
            )

    provider = data["provider"]
    credential_transport = provider["name"] in CREDENTIAL_PROVIDER_NAMES
    remote_transport = not _is_loopback_endpoint(provider["endpoint"])
    if credential_transport or remote_transport:
        route_keys = (
            "provider.endpoint",
            "provider.name",
            "provider.model",
            "provider.api_mode",
            "provider.prompt_cache_key",
            "provider.prompt_cache_retention",
        )
        controlled = next((key for key in route_keys if project_changed(key)), None)
        if controlled is not None:
            raise HarnessError(
                f"{controlled} cannot select or configure a credential-bearing or remote provider from shareable project config; "
                "move the provider route to trusted local, user, environment, explicit, or command-line config"
            )
    if provider["api_key_env"] and project_controls("provider.api_key_env"):
        raise HarnessError("provider.api_key_env requires trusted local, user, environment, or command-line config")
    if provider["command"] and project_controls("provider.command"):
        raise HarnessError("provider.command requires trusted local, user, environment, or command-line config")
    if project_controls("provider.endpoint"):
        if credential_transport or provider["api_key_env"] or not _is_loopback_endpoint(provider["endpoint"]):
            raise HarnessError(
                "provider.endpoint requires trusted local, user, environment, or command-line config when it can receive credentials or contact a remote host"
            )

    execution = data["execution"]
    trusted_execution = trusted_floor["execution"]
    if project_controls("execution.inherit_environment"):
        inherited = _normalized_policy(execution["inherit_environment"])
        trusted_inherited = _normalized_policy(trusted_execution["inherit_environment"])
        added = sorted(inherited - trusted_inherited)
        if added:
            raise HarnessError(
                "execution.inherit_environment cannot add names from shareable project config; "
                f"move these entries to trusted local config: {', '.join(added)}"
            )
    if execution["mode"] == "docker" and any(
        project_controls(key)
        for key in ("execution.mode", "execution.docker_image", "execution.docker_network")
    ):
        raise HarnessError(
            "Docker execution mode, image, and network require trusted local, user, environment, or command-line config"
        )
    for key in ("deny_executables", "deny_argument_sequences"):
        dotted = f"execution.{key}"
        if project_controls(dotted):
            missing = sorted(_normalized_policy(trusted_execution[key]) - _normalized_policy(execution[key]))
            if missing:
                raise HarnessError(
                    f"{dotted} cannot remove trusted policy entries from shareable project config: {', '.join(missing)}"
                )
    for dotted in sorted(SHARED_NON_ESCALATING_LIMITS):
        if not project_controls(dotted):
            continue
        section, key = dotted.split(".", 1)
        if data[section][key] > trusted_floor[section][key]:
            raise HarnessError(f"{dotted} cannot raise the trusted limit from shareable project config")

    _validate_embedding_route(data, provenance, shared_source, trusted_floor)

    if data["memory"]["allow_remote_embeddings"] and project_controls("memory.allow_remote_embeddings"):
        raise HarnessError(
            "memory.allow_remote_embeddings requires trusted local, user, environment, explicit, or command-line config"
        )

    if data["mcp"]["servers"] and project_controls("mcp.servers"):
        raise HarnessError("MCP servers require trusted local, user, environment, or command-line config")

    # A checked-in suite must not be able to point QA requests at a remote host.
    if project_controls("qa.allow_hosts"):
        remote = sorted(
            str(host)
            for host in data["qa"]["allow_hosts"]
            if str(host).lower() not in ("127.0.0.1", "localhost", "::1")
        )
        if remote:
            raise HarnessError(
                "qa.allow_hosts can only name loopback hosts in shareable project config; "
                f"move these entries to trusted local config: {', '.join(remote)}"
            )
    if (data["plugins"]["enabled"] or data["plugins"]["paths"]) and (
        project_controls("plugins.enabled") or project_controls("plugins.paths")
    ):
        raise HarnessError("Executable plugins require trusted local, user, environment, or command-line config")
    trusted_git = trusted_floor["git"]
    if project_controls("git.protected_branches"):
        missing_branches = sorted(set(trusted_git["protected_branches"]) - set(data["git"]["protected_branches"]))
        if missing_branches:
            raise HarnessError(
                "git.protected_branches cannot remove trusted branches from shareable project config: "
                + ", ".join(missing_branches)
            )
    if project_controls("git.required_branch_prefix"):
        trusted_prefix = str(trusted_git["required_branch_prefix"])
        effective_prefix = str(data["git"]["required_branch_prefix"])
        if trusted_prefix and not effective_prefix.startswith(trusted_prefix):
            raise HarnessError(
                "git.required_branch_prefix cannot weaken the trusted prefix from shareable project config"
            )
    for key in ("allow_commit", "allow_push"):
        if data["git"][key] and project_controls(f"git.{key}"):
            raise HarnessError(f"git.{key} requires trusted local, user, environment, or command-line config")
    if bool(trusted_floor["git"]["enabled"]) and not bool(data["git"]["enabled"]) and project_controls("git.enabled"):
        raise HarnessError("git.enabled cannot disable a trusted Git policy from shareable project config")
    for key in ("reviewers", "review_parallelism"):
        if project_controls(f"workflow.{key}") and int(data["workflow"][key]) < int(trusted_floor["workflow"][key]):
            raise HarnessError(f"workflow.{key} cannot reduce the trusted evaluator floor from shareable project config")
    for key in ("require_review", "rollback_on_exhaustion"):
        if (
            bool(trusted_floor["workflow"][key])
            and not bool(data["workflow"][key])
            and project_controls(f"workflow.{key}")
        ):
            raise HarnessError(f"workflow.{key} cannot disable a trusted safety policy from shareable project config")
    if project_changed("workflow.name"):
        trusted_name = str(trusted_floor["workflow"]["name"])
        selected_name = str(data["workflow"]["name"])
        if not (
            trusted_name == "planner-coder-reviewer"
            and selected_name == "gauntlet"
            and bool(data["workflow"]["require_review"])
        ):
            raise HarnessError(
                "workflow.name cannot select an untrusted or weaker workflow policy from shareable project config"
            )
    for key in ("test_commands", "lint_commands", "build_commands", "security_commands", "performance_commands"):
        dotted = f"project.{key}"
        if project_changed(dotted):
            raise HarnessError(
                f"{dotted} is set in a settings file this machine has not been told "
                "to trust. A setting like that can start a program, so nothing reads "
                "it until somebody says the file is theirs. Read the file, then run: "
                "harness trust"
            )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value: object, name: str, minimum: int, maximum: int | None = None) -> int:
    if not _is_int(value) or value < minimum or (maximum is not None and value > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise HarnessError(f"{name} must be an integer of at least {minimum}{suffix}")
    return value


def _require_string(value: object, name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise HarnessError(f"{name} must be a {qualifier}string")
    return value


def _require_string_list(value: object, name: str, *, relative: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise HarnessError(f"{name} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise HarnessError(f"{name} must not contain duplicates")
    if relative and any(Path(item).is_absolute() or ".." in Path(item).parts for item in value):
        raise HarnessError(f"{name} must contain project-relative paths without parent traversal")
    return value


def _require_relative_path(value: str, name: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise HarnessError(f"{name} must be a project-relative path without parent traversal")
    return value


def _require_argv(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise HarnessError(f"{name} must be an array of non-empty strings")
    return value


def _require_commands(value: object, name: str, *, allow_empty: bool = True) -> list[list[str]]:
    if not isinstance(value, list) or (
        not allow_empty and not value
    ) or any(
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
        for command in value
    ):
        raise HarnessError(f"{name} must be an array of non-empty argv arrays")
    return value


def validate_config(data: dict[str, Any]) -> None:
    _check_known_keys(data, DEFAULT_CONFIG)
    if not _is_int(data.get("schema_version")) or data["schema_version"] != 1:
        raise HarnessError("Unsupported config schema_version; expected 1")
    provider = data["provider"]
    provider_names = (
        "openai", "anthropic", "gemini", "ollama", "local", "openai-compatible",
        # Assistants you already pay for, driven through their own command line.
        "claude-cli", "copilot-cli", "assistant-cli",
    )
    profile_provider_names = (*provider_names, "codex-cli")
    if provider["name"] not in provider_names:
        raise HarnessError("provider.name must be one of: " + ", ".join(provider_names))
    if provider["api_mode"] not in ("auto", "responses", "chat-completions"):
        raise HarnessError("provider.api_mode must be auto, responses, or chat-completions")
    if provider["prompt_cache_retention"] not in ("", "in_memory", "24h"):
        raise HarnessError("provider.prompt_cache_retention must be empty, in_memory, or 24h")
    subscription_kinds = ("claude-cli", "copilot-cli", "assistant-cli")
    _require_string(provider["model"], "provider.model", allow_empty=provider["name"] in subscription_kinds)
    if provider["name"] in subscription_kinds:
        if provider["endpoint"]:
            raise HarnessError(f"provider.endpoint must be empty for {provider['name']}")
        if provider["api_key_env"]:
            raise HarnessError(f"provider.api_key_env must be empty for {provider['name']}; it signs in on its own")
    else:
        _require_string(provider["endpoint"], "provider.endpoint", allow_empty=False)
        _validate_endpoint(provider["endpoint"])
    for name in ("api_key_env", "prompt_cache_key", "prompt_cache_retention"):
        _require_string(provider[name], f"provider.{name}")
    if len(provider["prompt_cache_key"]) > 64:
        raise HarnessError("provider.prompt_cache_key must be at most 64 characters")
    if provider["api_key_env"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", provider["api_key_env"]):
        raise HarnessError("provider.api_key_env must be an environment variable name")
    if not isinstance(provider["temperature"], (int, float)) or isinstance(provider["temperature"], bool) or not math.isfinite(float(provider["temperature"])) or not 0 <= provider["temperature"] <= 2:
        raise HarnessError("provider.temperature must be a finite number between 0 and 2")
    _require_int(provider["max_output_tokens"], "provider.max_output_tokens", 1, RESOURCE_LIMIT_MAXIMA["provider.max_output_tokens"])
    for role, cap in provider["role_output_caps"].items():
        _require_int(
            cap,
            f"provider.role_output_caps.{role}",
            1,
            RESOURCE_LIMIT_MAXIMA["provider.max_output_tokens"],
        )
    _require_int(provider["timeout_seconds"], "provider.timeout_seconds", 1, RESOURCE_LIMIT_MAXIMA["provider.timeout_seconds"])
    _require_argv(provider["command"], "provider.command")

    profiles = data["providers"]
    if not isinstance(profiles, dict):
        raise HarnessError("providers must be an object")
    profile_id_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    provider_fields = set(DEFAULT_CONFIG["provider"]) | {
        "kind", "auth_mode", "max_concurrency", "pricing_ref", "allow_project_graphs", "max_data_class",
        "role_output_caps", "reasoning_effort",
    }
    data_classes = {"public", "project_private", "restricted"}
    for profile_id, profile in profiles.items():
        dotted = f"providers.{profile_id}"
        if not isinstance(profile_id, str) or not profile_id_pattern.fullmatch(profile_id) or not isinstance(profile, dict):
            raise HarnessError("Provider profiles require plain IDs and object values")
        unknown = sorted(set(profile) - provider_fields)
        if unknown:
            raise HarnessError(f"Unknown config key: {dotted}.{unknown[0]}")
        name = profile.get("kind", profile.get("name"))
        if name not in profile_provider_names:
            raise HarnessError(f"{dotted}.kind must name a supported provider")
        if "kind" in profile and "name" in profile and profile["kind"] != profile["name"]:
            raise HarnessError(f"{dotted}.kind conflicts with name")
        _require_string(profile.get("model"), f"{dotted}.model", allow_empty=False)
        endpoint = _require_string(profile.get("endpoint", ""), f"{dotted}.endpoint")
        if name in ("claude-cli", "copilot-cli", "assistant-cli"):
            if endpoint:
                raise HarnessError(f"{dotted}.endpoint must be empty for {name}")
        elif name != "codex-cli":
            if not endpoint:
                raise HarnessError(f"{dotted}.endpoint must be a non-empty string")
            _validate_endpoint(endpoint, f"{dotted}.endpoint")
            parsed_profile_endpoint = urllib.parse.urlsplit(endpoint)
            if parsed_profile_endpoint.username or parsed_profile_endpoint.password or parsed_profile_endpoint.query or parsed_profile_endpoint.fragment:
                raise HarnessError(
                    f"{dotted}.endpoint must not contain a name and password, a "
                    "query, or a #part. Put the key in an environment variable "
                    "and name it in api_key_env."
                )
        elif endpoint:
            raise HarnessError(f"{dotted}.endpoint must be empty for codex-cli")
        api_key_env = _require_string(profile.get("api_key_env", ""), f"{dotted}.api_key_env")
        if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise HarnessError(f"{dotted}.api_key_env must be an environment variable name")
        if name in {"openai", "anthropic", "gemini"} and not api_key_env:
            raise HarnessError(f"{dotted}.api_key_env is required for the official {name} provider")
        api_mode = _require_string(profile.get("api_mode", "auto"), f"{dotted}.api_mode", allow_empty=False)
        if api_mode not in ("auto", "responses", "chat-completions"):
            raise HarnessError(f"{dotted}.api_mode must be auto, responses, or chat-completions")
        cache_key = _require_string(profile.get("prompt_cache_key", ""), f"{dotted}.prompt_cache_key")
        if len(cache_key) > 64:
            raise HarnessError(f"{dotted}.prompt_cache_key must be at most 64 characters")
        cache_retention = _require_string(
            profile.get("prompt_cache_retention", ""), f"{dotted}.prompt_cache_retention"
        )
        if cache_retention not in ("", "in_memory", "24h"):
            raise HarnessError(f"{dotted}.prompt_cache_retention must be empty, in_memory, or 24h")
        if name == "anthropic" and cache_retention == "24h":
            raise HarnessError(f"{dotted}.prompt_cache_retention 24h is not supported by Anthropic")
        if name in {"gemini", "ollama", "local", "openai-compatible", "codex-cli"} and cache_retention:
            raise HarnessError(f"{dotted}.prompt_cache_retention is not supported by {name} profiles")
        temperature = profile.get("temperature", DEFAULT_CONFIG["provider"]["temperature"])
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(float(temperature)) or not 0 <= temperature <= 2:
            raise HarnessError(f"{dotted}.temperature must be a finite number between 0 and 2")
        _require_int(profile.get("max_output_tokens", DEFAULT_CONFIG["provider"]["max_output_tokens"]), f"{dotted}.max_output_tokens", 1, RESOURCE_LIMIT_MAXIMA["provider.max_output_tokens"])
        _require_int(profile.get("timeout_seconds", DEFAULT_CONFIG["provider"]["timeout_seconds"]), f"{dotted}.timeout_seconds", 1, RESOURCE_LIMIT_MAXIMA["provider.timeout_seconds"])
        role_output_caps = profile.get("role_output_caps", {})
        if not isinstance(role_output_caps, dict):
            raise HarnessError(f"{dotted}.role_output_caps must be an object")
        unknown_roles = sorted(set(role_output_caps) - {"planner", "coder", "evaluator", "merge"})
        if unknown_roles:
            raise HarnessError(f"Unknown config key: {dotted}.role_output_caps.{unknown_roles[0]}")
        for role, cap in role_output_caps.items():
            _require_int(
                cap,
                f"{dotted}.role_output_caps.{role}",
                1,
                RESOURCE_LIMIT_MAXIMA["provider.max_output_tokens"],
            )
        _require_int(profile.get("max_concurrency", 1), f"{dotted}.max_concurrency", 1, 32)
        command = _require_argv(profile.get("command", []), f"{dotted}.command")
        if name == "local" and not command:
            raise HarnessError(f"{dotted}.command must not be empty for a local provider")
        auth_mode = _require_string(profile.get("auth_mode", ""), f"{dotted}.auth_mode")
        reasoning_effort = profile.get("reasoning_effort")
        if reasoning_effort is not None and reasoning_effort not in ("none", "low", "medium", "high", "xhigh", "max"):
            raise HarnessError(f"{dotted}.reasoning_effort is invalid")
        if name == "codex-cli":
            if not command:
                raise HarnessError(f"{dotted}.command must not be empty for codex-cli")
            if auth_mode != "chatgpt":
                raise HarnessError(f"{dotted}.auth_mode must be chatgpt for codex-cli")
            if api_key_env:
                raise HarnessError(f"{dotted}.api_key_env must be empty for codex-cli")
            if profile.get("pricing_ref"):
                raise HarnessError(f"{dotted}.pricing_ref is not supported by codex-cli subscription profiles")
            if api_mode != "auto":
                raise HarnessError(f"{dotted}.api_mode must be auto for codex-cli")
        elif auth_mode:
            raise HarnessError(f"{dotted}.auth_mode is supported only by codex-cli")
        if name != "codex-cli" and reasoning_effort is not None:
            raise HarnessError(f"{dotted}.reasoning_effort is supported only by codex-cli")
        _require_string(profile.get("pricing_ref", ""), f"{dotted}.pricing_ref")
        if not isinstance(profile.get("allow_project_graphs", False), bool):
            raise HarnessError(f"{dotted}.allow_project_graphs must be a boolean")
        if profile.get("max_data_class", "project_private") not in data_classes:
            raise HarnessError(f"{dotted}.max_data_class must be public, project_private, or restricted")

    agents = data["agents"]
    if not isinstance(agents, dict):
        raise HarnessError("agents must be an object")
    agent_fields = {
        "provider_ref", "role", "model", "system_prompt", "capabilities",
        "temperature", "max_output_tokens", "reasoning_effort",
    }
    for agent_id, agent in agents.items():
        dotted = f"agents.{agent_id}"
        if not isinstance(agent_id, str) or not profile_id_pattern.fullmatch(agent_id) or not isinstance(agent, dict):
            raise HarnessError("Agent entries require plain IDs and object values")
        unknown = sorted(set(agent) - agent_fields)
        if unknown:
            raise HarnessError(f"Unknown config key: {dotted}.{unknown[0]}")
        provider_ref = _require_string(agent.get("provider_ref"), f"{dotted}.provider_ref", allow_empty=False)
        if provider_ref not in profiles:
            raise HarnessError(f"{dotted}.provider_ref names an unknown provider profile")
        role = _require_string(agent.get("role", agent_id), f"{dotted}.role", allow_empty=False)
        if len(role) > 64:
            raise HarnessError(f"{dotted}.role must be at most 64 characters")
        _require_string(agent.get("model", ""), f"{dotted}.model")
        prompt = _require_string(agent.get("system_prompt", ""), f"{dotted}.system_prompt")
        if len(prompt) > 100_000:
            raise HarnessError(f"{dotted}.system_prompt must be at most 100000 characters")
        capabilities = _require_string_list(agent.get("capabilities", []), f"{dotted}.capabilities")
        if any(not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", item) for item in capabilities):
            raise HarnessError(f"{dotted}.capabilities entries must use lower-case capability names")
        if agent.get("temperature") is not None:
            temperature = agent["temperature"]
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(float(temperature)) or not 0 <= temperature <= 2:
                raise HarnessError(f"{dotted}.temperature must be a finite number between 0 and 2")
        if agent.get("max_output_tokens") is not None:
            _require_int(agent["max_output_tokens"], f"{dotted}.max_output_tokens", 1, RESOURCE_LIMIT_MAXIMA["provider.max_output_tokens"])
        effort = agent.get("reasoning_effort")
        if effort is not None and effort not in ("none", "low", "medium", "high", "xhigh", "max"):
            raise HarnessError(f"{dotted}.reasoning_effort is invalid")

    pricing = data["pricing"]
    if not isinstance(pricing["allow_unpriced_remote_calls"], bool):
        raise HarnessError("pricing.allow_unpriced_remote_calls must be a boolean")
    if not isinstance(pricing["snapshots"], list):
        raise HarnessError("pricing.snapshots must be an array")
    price_ids: set[str] = set()
    price_fields = {
        "id", "provider", "model_pattern", "input_per_million_microusd",
        "cached_input_per_million_microusd", "cache_write_per_million_microusd",
        "output_per_million_microusd", "effective_at", "source_url",
    }
    for index, item in enumerate(pricing["snapshots"]):
        dotted = f"pricing.snapshots[{index}]"
        if not isinstance(item, dict) or set(item) - price_fields:
            raise HarnessError(f"{dotted} contains unsupported fields")
        for required in ("id", "provider", "model_pattern", "input_per_million_microusd", "output_per_million_microusd", "effective_at", "source_url"):
            if required not in item:
                raise HarnessError(f"{dotted}.{required} is required")
        price_id = _require_string(item["id"], f"{dotted}.id", allow_empty=False)
        if price_id in price_ids:
            raise HarnessError("pricing snapshot IDs must be unique")
        price_ids.add(price_id)
        if item["provider"] not in provider_names:
            raise HarnessError(f"{dotted}.provider must name a supported provider")
        _require_string(item["model_pattern"], f"{dotted}.model_pattern", allow_empty=False)
        for field in ("input_per_million_microusd", "cached_input_per_million_microusd", "cache_write_per_million_microusd", "output_per_million_microusd"):
            if field in item:
                _require_int(item[field], f"{dotted}.{field}", 0, 1_000_000_000_000)
        if not isinstance(item["effective_at"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["effective_at"]):
            raise HarnessError(f"{dotted}.effective_at must use YYYY-MM-DD")
        try:
            datetime.date.fromisoformat(item["effective_at"])
        except ValueError as exc:
            raise HarnessError(f"{dotted}.effective_at must be a valid calendar date") from exc
        _validate_endpoint(item["source_url"], f"{dotted}.source_url")

    price_by_id = {item["id"]: item for item in pricing["snapshots"]}
    for profile_id, profile in profiles.items():
        pricing_ref = profile.get("pricing_ref")
        if not pricing_ref:
            continue
        snapshot = price_by_id.get(pricing_ref)
        if snapshot is None:
            raise HarnessError(f"providers.{profile_id}.pricing_ref names an unknown price snapshot")
        provider_name = profile.get("kind", profile.get("name"))
        if snapshot["provider"] != provider_name or not fnmatch.fnmatchcase(profile["model"], snapshot["model_pattern"]):
            raise HarnessError(f"providers.{profile_id}.pricing_ref does not match its provider and model")

    project = data["project"]
    _require_string_list(project["standards_files"], "project.standards_files", relative=True)
    _require_string_list(project["ignore"], "project.ignore")
    _require_int(project["max_file_bytes"], "project.max_file_bytes", 1, RESOURCE_LIMIT_MAXIMA["project.max_file_bytes"])
    for group in ("test_commands", "lint_commands", "build_commands", "security_commands", "performance_commands"):
        _require_commands(project[group], f"project.{group}")

    execution = data["execution"]
    if data["execution"]["mode"] not in ("process", "docker"):
        raise HarnessError("execution.mode must be process or docker")
    for name, minimum in (("timeout_seconds", 1), ("max_output_bytes", 1024), ("max_changed_files", 1), ("max_changed_bytes", 1)):
        dotted = f"execution.{name}"
        _require_int(execution[name], dotted, minimum, RESOURCE_LIMIT_MAXIMA[dotted])
    for name in ("inherit_environment", "deny_executables", "deny_argument_sequences"):
        _require_string_list(execution[name], f"execution.{name}")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in execution["inherit_environment"]):
        raise HarnessError("execution.inherit_environment entries must be environment variable names")
    for name in ("docker_image", "docker_network"):
        _require_string(execution[name], f"execution.{name}", allow_empty=False)

    git = data["git"]
    for name in ("enabled", "allow_commit", "allow_push", "allow_merge"):
        if not isinstance(git[name], bool):
            raise HarnessError(f"git.{name} must be a boolean")
    _require_string_list(git["protected_branches"], "git.protected_branches")
    _require_string(git["required_branch_prefix"], "git.required_branch_prefix")
    if git["allow_merge"]:
        raise HarnessError("git.allow_merge is reserved and must remain false")

    memory = data["memory"]
    if not isinstance(memory["enabled"], bool):
        raise HarnessError("memory.enabled must be a boolean")
    _require_string(memory["database"], "memory.database", allow_empty=False)
    if Path(memory["database"]).is_absolute() or ".." in Path(memory["database"]).parts:
        raise HarnessError("memory.database must be a project-relative path without parent traversal")
    _require_int(memory["max_results"], "memory.max_results", 1, RESOURCE_LIMIT_MAXIMA["memory.max_results"])
    _require_int(memory["retention_days"], "memory.retention_days", 1, RESOURCE_LIMIT_MAXIMA["memory.retention_days"])
    _require_string(memory["embedding_provider"], "memory.embedding_provider")
    _require_string(memory["embedding_model"], "memory.embedding_model")
    if not isinstance(memory["allow_remote_embeddings"], bool):
        raise HarnessError("memory.allow_remote_embeddings must be a boolean")

    context = data["context"]
    _require_int(context["max_chars"], "context.max_chars", 2000, RESOURCE_LIMIT_MAXIMA["context.max_chars"])
    for name in ("reserve_chars", "recent_event_chars", "memory_chars", "workspace_chars"):
        dotted = f"context.{name}"
        _require_int(context[name], dotted, 0, RESOURCE_LIMIT_MAXIMA[dotted])
    if context["reserve_chars"] >= context["max_chars"]:
        raise HarnessError("context.reserve_chars must be smaller than context.max_chars")

    workflow = data["workflow"]
    _require_string(workflow["name"], "workflow.name", allow_empty=False)
    _require_int(workflow["max_iterations"], "workflow.max_iterations", 1, RESOURCE_LIMIT_MAXIMA["workflow.max_iterations"])
    _require_int(workflow["max_elapsed_seconds"], "workflow.max_elapsed_seconds", 1, RESOURCE_LIMIT_MAXIMA["workflow.max_elapsed_seconds"])
    _require_int(workflow["repeat_failure_limit"], "workflow.repeat_failure_limit", 1, RESOURCE_LIMIT_MAXIMA["workflow.repeat_failure_limit"])
    _require_int(workflow["max_tool_calls"], "workflow.max_tool_calls", 1, RESOURCE_LIMIT_MAXIMA["workflow.max_tool_calls"])
    _require_int(workflow["max_tool_output_bytes"], "workflow.max_tool_output_bytes", 1024, RESOURCE_LIMIT_MAXIMA["workflow.max_tool_output_bytes"])
    _require_int(workflow["max_tool_total_bytes"], "workflow.max_tool_total_bytes", 1024, RESOURCE_LIMIT_MAXIMA["workflow.max_tool_total_bytes"])
    _require_int(workflow["reviewers"], "workflow.reviewers", 1, RESOURCE_LIMIT_MAXIMA["workflow.reviewers"])
    _require_int(workflow["review_parallelism"], "workflow.review_parallelism", 1, RESOURCE_LIMIT_MAXIMA["workflow.review_parallelism"])
    _require_string_list(workflow["reviewer_lenses"], "workflow.reviewer_lenses")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}", lens) for lens in workflow["reviewer_lenses"]):
        raise HarnessError("workflow.reviewer_lenses entries must be plain names of at most 64 characters")
    if workflow["reviewer_lenses"] and len(workflow["reviewer_lenses"]) != workflow["reviewers"]:
        raise HarnessError("workflow.reviewer_lenses must be empty or contain one unique name per reviewer")
    if workflow["max_tool_output_bytes"] > workflow["max_tool_total_bytes"]:
        raise HarnessError("workflow.max_tool_output_bytes must not exceed workflow.max_tool_total_bytes")
    if not isinstance(workflow["temperature_decay"], (int, float)) or isinstance(workflow["temperature_decay"], bool) or not math.isfinite(float(workflow["temperature_decay"])) or not 0 < workflow["temperature_decay"] <= 1:
        raise HarnessError("workflow.temperature_decay must be greater than 0 and at most 1")
    for name in ("rollback_on_exhaustion", "require_review", "require_executable_counterexamples"):
        if not isinstance(workflow[name], bool):
            raise HarnessError(f"workflow.{name} must be a boolean")

    mcp = data["mcp"]
    _require_int(mcp["max_response_bytes"], "mcp.max_response_bytes", 1024, RESOURCE_LIMIT_MAXIMA["mcp.max_response_bytes"])
    _require_int(mcp["timeout_seconds"], "mcp.timeout_seconds", 1, RESOURCE_LIMIT_MAXIMA["mcp.timeout_seconds"])
    if not isinstance(mcp["servers"], list):
        raise HarnessError("mcp.servers must be an array")
    server_names: set[str] = set()
    for server in mcp["servers"]:
        if not isinstance(server, dict) or set(server) - {"name", "transport", "command", "args", "url", "allowed_tools", "protocol_mode"}:
            raise HarnessError("mcp.servers entries must use only supported fields")
        name = _require_string(server.get("name"), "mcp.servers.name", allow_empty=False)
        if name in server_names:
            raise HarnessError("mcp.servers names must be unique")
        server_names.add(name)
        if server.get("transport") not in ("stdio", "http"):
            raise HarnessError("mcp.servers transport must be stdio or http")
        if server["transport"] == "stdio":
            _require_string(server.get("command"), "mcp.servers.command", allow_empty=False)
        elif "command" in server and server["command"]:
            raise HarnessError("HTTP MCP servers must not configure a command")
        if "args" in server:
            _require_argv(server["args"], "mcp.servers.args")
        if server["transport"] == "http":
            _require_string(server.get("url"), "mcp.servers.url", allow_empty=False)
            _validate_endpoint(server["url"], "mcp.servers.url")
        elif "url" in server and server["url"]:
            raise HarnessError("stdio MCP servers must not configure a URL")
        if "allowed_tools" in server:
            _require_string_list(server["allowed_tools"], "mcp.servers.allowed_tools")
        if server.get("protocol_mode", "legacy") not in ("legacy", "auto", "modern"):
            raise HarnessError("mcp.servers.protocol_mode must be legacy, auto, or modern")

    ui = data["ui"]
    if ui["host"] not in ("127.0.0.1", "localhost", "::1"):
        raise HarnessError("ui.host must be a loopback host")
    if not _is_int(ui["port"]) or not 0 <= ui["port"] <= 65535:
        raise HarnessError("ui.port must be between 0 and 65535")
    if not isinstance(ui["open_browser"], bool):
        raise HarnessError("ui.open_browser must be a boolean")

    qa = data["qa"]
    _require_string(qa["suite"], "qa.suite", allow_empty=False)
    _require_relative_path(qa["suite"], "qa.suite")
    _require_string(qa["artifacts_dir"], "qa.artifacts_dir", allow_empty=False)
    _require_relative_path(qa["artifacts_dir"], "qa.artifacts_dir")
    _require_int(qa["workers"], "qa.workers", 1, RESOURCE_LIMIT_MAXIMA["qa.workers"])
    _require_int(qa["default_timeout_seconds"], "qa.default_timeout_seconds", 1, RESOURCE_LIMIT_MAXIMA["qa.default_timeout_seconds"])
    _require_int(qa["keep_runs"], "qa.keep_runs", 0, RESOURCE_LIMIT_MAXIMA["qa.keep_runs"])
    _require_int(qa["max_evidence_chars"], "qa.max_evidence_chars", 100, RESOURCE_LIMIT_MAXIMA["qa.max_evidence_chars"])
    _require_int(qa["max_response_bytes"], "qa.max_response_bytes", 1024, RESOURCE_LIMIT_MAXIMA["qa.max_response_bytes"])
    _require_int(qa["flaky_min_runs"], "qa.flaky_min_runs", 2, RESOURCE_LIMIT_MAXIMA["qa.flaky_min_runs"])
    threshold = qa["flaky_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)) or not 0 < float(threshold) <= 0.5:
        raise HarnessError("qa.flaky_threshold must be a number greater than 0 and at most 0.5")
    _require_string_list(qa["allow_hosts"], "qa.allow_hosts")
    if not qa["allow_hosts"]:
        raise HarnessError("qa.allow_hosts must name at least one host")

    plugins = data["plugins"]
    _require_string_list(plugins["enabled"], "plugins.enabled")
    _require_string_list(plugins["paths"], "plugins.paths", relative=True)


def load_config(start: Path | None = None, explicit: Path | None = None, cli_overrides: dict[str, Any] | None = None) -> LoadedConfig:
    root = find_project_root(start)
    data = copy.deepcopy(DEFAULT_CONFIG)
    provenance = {key: "default" for key in _flatten_keys(data)}
    paths = [user_config_path(), root / ".harness" / "config.json", root / ".harness" / "config.local.json"]
    if explicit:
        paths.append(explicit.resolve())
    sources: list[Path] = []
    shared_project_path = (root / ".harness" / "config.json").resolve()
    local_project_path = (root / ".harness" / "config.local.json").resolve()
    trusted_local = is_project_local_config_trusted(root, local_project_path) or (
        explicit is not None and explicit.resolve() == local_project_path
    )
    # Only a recorded yes counts here. Pointing --config at the shared file is
    # naming a file, not reading it and saying it is yours, and a settings file
    # that travelled with a repository is exactly the one to be careful about.
    trusted_shared = is_project_shared_config_trusted(root)
    shared_source = str(shared_project_path)
    trusted_floor = copy.deepcopy(data)
    floor_captured = False
    seen_paths: set[Path] = set()
    for path in paths:
        if path.is_file():
            resolved_path = path.resolve()
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            layer = _read_json(path)
            _check_known_keys(layer, DEFAULT_CONFIG)
            is_shared_project = (
                resolved_path == shared_project_path and not trusted_shared
            ) or (resolved_path == local_project_path and not trusted_local)
            if is_shared_project:
                if not floor_captured:
                    trusted_floor = copy.deepcopy(data)
                    floor_captured = True
            _merge(data, layer, provenance, shared_source if is_shared_project else str(path))
            sources.append(path)
    env = _environment_layer()
    if env:
        _check_known_keys(env, DEFAULT_CONFIG)
        _merge(data, env, provenance, "environment")
    if cli_overrides:
        _check_known_keys(cli_overrides, DEFAULT_CONFIG)
        _merge(data, cli_overrides, provenance, "command line")
    validate_config(data)
    if (shared_project_path.is_file() and not trusted_shared) or (
        local_project_path.is_file() and not trusted_local
    ):
        _validate_capability_provenance(data, provenance, shared_source, trusted_floor)
    return LoadedConfig(data, root, sources, provenance, trusted_floor)


def load_isolated_config(root: Path, overrides: dict[str, Any] | None = None) -> LoadedConfig:
    """Build config without reading user, project, or environment layers."""
    resolved_root = root.resolve()
    data = copy.deepcopy(DEFAULT_CONFIG)
    provenance = {key: "default" for key in _flatten_keys(data)}
    if overrides:
        _check_known_keys(overrides, DEFAULT_CONFIG)
        _merge(data, overrides, provenance, "isolated override")
    validate_config(data)
    return LoadedConfig(data, resolved_root, [], provenance)


def _flatten_keys(value: dict[str, Any], prefix: str = "") -> list[str]:
    output: list[str] = []
    for key, child in value.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict):
            output.extend(_flatten_keys(child, dotted))
        else:
            output.append(dotted)
    return output


def write_default_project_config(
    root: Path,
    provider: str,
    model: str,
    endpoint: str,
    api_key_env: str = "",
    test_commands: list[list[str]] | None = None,
    lint_commands: list[list[str]] | None = None,
    build_commands: list[list[str]] | None = None,
) -> Path:
    folder = root / ".harness"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "config.json"
    if path.exists():
        raise HarnessError(f"Config already exists: {path}")
    local_path = folder / "config.local.json"
    if local_path.exists():
        raise HarnessError(f"Local config already exists: {local_path}")
    local_ignore = folder / ".gitignore"
    # Shared: config.json and the check suites. Ignored: everything a run writes.
    ignore_lines = [
        "config.local.json", "memory/", "runs/", "backups/", "checkpoints/", "cache/",
        "qa/runs/", "qa/tmp/", "qa/history.json", "qa/candidates.json",
    ]
    existing_ignore = local_ignore.read_text(encoding="utf-8") if local_ignore.exists() else ""
    existing_entries = {line.strip() for line in existing_ignore.splitlines()}
    missing_entries = [line for line in ignore_lines if line not in existing_entries]
    if missing_entries:
        updated_ignore = existing_ignore
        if updated_ignore and not updated_ignore.endswith("\n"):
            updated_ignore += "\n"
        updated_ignore += "\n".join(missing_entries) + "\n"
        put_this_file_in_place(local_ignore, updated_ignore)
    selected = copy.deepcopy(DEFAULT_CONFIG)
    provider_requires_trust = provider in CREDENTIAL_PROVIDER_NAMES or provider == "local" or not _is_loopback_endpoint(endpoint)
    if not provider_requires_trust:
        selected["provider"].update({"name": provider, "model": model})
    put_this_file_in_place(
        path, json.dumps(selected, indent=2, sort_keys=True) + "\n"
    )
    local_provider: dict[str, Any] = {"endpoint": endpoint, "api_key_env": api_key_env}
    if provider_requires_trust:
        local_provider.update({"name": provider, "model": model})
    put_this_file_in_place(
        local_path,
        json.dumps(
            {
                "provider": local_provider,
                "project": {
                    "test_commands": test_commands or [],
                    "lint_commands": lint_commands or [],
                    "build_commands": build_commands or [],
                },
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
    )
    trust_project_local_config(root, local_path)
    return path
