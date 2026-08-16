from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from ..config import DEFAULT_CONFIG, LoadedConfig
from ..models import HarnessError
from .catalog import ModelCatalogEntry, ProviderCapabilities, capabilities_for, offline_models


PROFILE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
AGENT_ID = PROFILE_ID


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    model: str
    endpoint: str
    api_key_env: str
    api_mode: str
    prompt_cache_key: str
    prompt_cache_retention: str
    temperature: float
    max_output_tokens: int
    role_output_caps: dict[str, int]
    timeout_seconds: int
    command: tuple[str, ...]
    auth_mode: str
    reasoning_effort: str | None
    max_concurrency: int
    pricing_ref: str | None
    allow_project_graphs: bool
    max_data_class: str


@dataclass(frozen=True)
class AgentSpec:
    id: str
    role: str
    provider_ref: str
    model: str | None
    system_prompt: str
    capabilities: frozenset[str]
    temperature: float | None
    max_output_tokens: int | None
    reasoning_effort: str | None


class ProviderRegistry:
    """Resolve trusted provider profiles without changing the legacy route."""

    def __init__(self, config: LoadedConfig):
        self.config = config
        raw_profiles = config.get("providers", {})
        self._profiles = self._build_profiles(raw_profiles)
        self._agents = self._build_agents(config.get("agents", {}))

    def _build_profiles(self, raw: object) -> dict[str, ProviderProfile]:
        if not isinstance(raw, dict):
            raise HarnessError("providers must be an object")
        if not raw:
            return {"default": self._profile_from("default", copy.deepcopy(self.config.data["provider"]))}
        profiles: dict[str, ProviderProfile] = {}
        for profile_id, value in raw.items():
            if not isinstance(profile_id, str) or not PROFILE_ID.fullmatch(profile_id):
                raise HarnessError("Provider profile IDs must start with a letter and contain only letters, digits, underscore, or hyphen")
            if not isinstance(value, dict):
                raise HarnessError(f"providers.{profile_id} must be an object")
            merged = copy.deepcopy(DEFAULT_CONFIG["provider"])
            merged.update(value)
            if "kind" in value:
                if "name" in value and value["name"] != value["kind"]:
                    raise HarnessError(f"providers.{profile_id}.kind conflicts with name")
                merged["name"] = value["kind"]
            if merged["name"] == "codex-cli" and "endpoint" not in value:
                merged["endpoint"] = ""
            profiles[profile_id] = self._profile_from(profile_id, merged)
        return profiles

    @staticmethod
    def _profile_from(profile_id: str, value: dict[str, Any]) -> ProviderProfile:
        return ProviderProfile(
            id=profile_id,
            name=str(value["name"]),
            model=str(value["model"]),
            endpoint=str(value["endpoint"]),
            api_key_env=str(value.get("api_key_env") or ""),
            api_mode=str(value.get("api_mode") or "auto"),
            prompt_cache_key=str(value.get("prompt_cache_key") or ""),
            prompt_cache_retention=str(value.get("prompt_cache_retention") or ""),
            temperature=float(value.get("temperature", 0.2)),
            max_output_tokens=int(value.get("max_output_tokens", 8192)),
            role_output_caps={str(key): int(cap) for key, cap in value.get("role_output_caps", {}).items()},
            timeout_seconds=int(value.get("timeout_seconds", 180)),
            command=tuple(value.get("command", [])),
            auth_mode=str(value.get("auth_mode") or ""),
            reasoning_effort=str(value["reasoning_effort"]) if value.get("reasoning_effort") else None,
            max_concurrency=int(value.get("max_concurrency", 1)),
            pricing_ref=str(value["pricing_ref"]) if value.get("pricing_ref") else None,
            allow_project_graphs=bool(value.get("allow_project_graphs", False)),
            max_data_class=str(value.get("max_data_class", "project_private")),
        )

    def _build_agents(self, raw: object) -> dict[str, AgentSpec]:
        if not isinstance(raw, dict):
            raise HarnessError("agents must be an object")
        agents: dict[str, AgentSpec] = {}
        for agent_id, value in raw.items():
            if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id) or not isinstance(value, dict):
                raise HarnessError("Agent entries require plain IDs and object values")
            provider_ref = str(value.get("provider_ref") or "")
            if provider_ref not in self._profiles:
                raise HarnessError(f"agents.{agent_id}.provider_ref names an unknown provider profile: {provider_ref}")
            agents[agent_id] = AgentSpec(
                id=agent_id,
                role=str(value.get("role") or agent_id),
                provider_ref=provider_ref,
                model=str(value["model"]) if value.get("model") else None,
                system_prompt=str(value.get("system_prompt") or ""),
                capabilities=frozenset(str(item) for item in value.get("capabilities", [])),
                temperature=float(value["temperature"]) if value.get("temperature") is not None else None,
                max_output_tokens=int(value["max_output_tokens"]) if value.get("max_output_tokens") is not None else None,
                reasoning_effort=str(value["reasoning_effort"]) if value.get("reasoning_effort") else None,
            )
        return agents

    def profile(self, profile_id: str = "default") -> ProviderProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise HarnessError(f"Unknown provider profile: {profile_id}") from exc

    def agent(self, agent_id: str) -> AgentSpec:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise HarnessError(f"Unknown agent: {agent_id}") from exc

    def profiles(self) -> list[ProviderProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def agents(self) -> list[AgentSpec]:
        return [self._agents[key] for key in sorted(self._agents)]

    def capabilities(self, profile_id: str = "default") -> ProviderCapabilities:
        return capabilities_for(self.profile(profile_id).name)

    def model_catalog(self, profile_id: str = "default") -> list[ModelCatalogEntry]:
        profile = self.profile(profile_id)
        items = offline_models(profile.name)
        if profile.model not in {item.model for item in items}:
            source = (
                "https://developers.openai.com/codex/auth"
                if profile.name == "codex-cli"
                else profile.endpoint
            )
            items.append(ModelCatalogEntry(profile.name, profile.model, profile.model, source))
        return items

    def provider_config(self, profile_id: str = "default") -> LoadedConfig:
        profile = self.profile(profile_id)
        data = copy.deepcopy(self.config.data)
        data["provider"].update(
            {
                "name": profile.name,
                "model": profile.model,
                "endpoint": profile.endpoint,
                "api_key_env": profile.api_key_env,
                "api_mode": profile.api_mode,
                "prompt_cache_key": profile.prompt_cache_key,
                "prompt_cache_retention": profile.prompt_cache_retention,
                "temperature": profile.temperature,
                "max_output_tokens": profile.max_output_tokens,
                "role_output_caps": dict(profile.role_output_caps),
                "timeout_seconds": profile.timeout_seconds,
                "command": list(profile.command),
                "auth_mode": profile.auth_mode,
                "reasoning_effort": profile.reasoning_effort,
            }
        )
        return LoadedConfig(data, self.config.project_root, self.config.sources, dict(self.config.provenance), self.config.trusted_floor)

    def agent_config(self, agent_id: str) -> LoadedConfig:
        """Return an isolated legacy-shaped config for one configured agent."""
        agent = self.agent(agent_id)
        routed = self.provider_config(agent.provider_ref)
        if agent.model is not None:
            routed.data["provider"]["model"] = agent.model
        if agent.temperature is not None:
            routed.data["provider"]["temperature"] = agent.temperature
        if agent.max_output_tokens is not None:
            routed.data["provider"]["max_output_tokens"] = agent.max_output_tokens
        if agent.reasoning_effort is not None:
            routed.data["provider"]["reasoning_effort"] = agent.reasoning_effort
        return routed

    def create(self, profile_id: str = "default"):
        from .base import create_provider

        return create_provider(self.provider_config(profile_id))

    def create_for_agent(self, agent_id: str):
        from .base import create_provider

        return create_provider(self.agent_config(agent_id))
