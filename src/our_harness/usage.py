from __future__ import annotations

import fnmatch
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .config import LoadedConfig
from .models import HarnessError, ProviderResponse


@dataclass(frozen=True)
class PriceSnapshot:
    id: str
    provider: str
    model_pattern: str
    input_per_million_microusd: int
    cached_input_per_million_microusd: int
    cache_write_per_million_microusd: int
    output_per_million_microusd: int
    effective_at: str
    source_url: str


@dataclass(frozen=True)
class UsageRecord:
    run_id: str
    request_id: str
    node_id: str
    agent_id: str
    role: str
    provider_profile_id: str
    provider: str
    model: str
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    tool_use_tokens: int | None
    billed_output_tokens: int | None
    latency_ms: int
    cost_microusd: int | None
    price_status: str
    price_snapshot_id: str | None
    created_at_ms: int


class PriceCatalog:
    def __init__(self, config: LoadedConfig):
        self.allow_unpriced_remote_calls = bool(config.get("pricing.allow_unpriced_remote_calls", False))
        self.snapshots = tuple(self._parse(item) for item in config.get("pricing.snapshots", []))

    @staticmethod
    def _parse(value: dict[str, Any]) -> PriceSnapshot:
        return PriceSnapshot(
            id=str(value["id"]),
            provider=str(value["provider"]),
            model_pattern=str(value["model_pattern"]),
            input_per_million_microusd=int(value["input_per_million_microusd"]),
            cached_input_per_million_microusd=int(value.get("cached_input_per_million_microusd", value["input_per_million_microusd"])),
            cache_write_per_million_microusd=int(value.get("cache_write_per_million_microusd", value["input_per_million_microusd"])),
            output_per_million_microusd=int(value["output_per_million_microusd"]),
            effective_at=str(value["effective_at"]),
            source_url=str(value["source_url"]),
        )

    def resolve(self, provider: str, model: str, preferred_id: str | None = None) -> PriceSnapshot | None:
        matches = [
            item
            for item in self.snapshots
            if item.provider == provider and fnmatch.fnmatchcase(model, item.model_pattern)
            and (preferred_id is None or item.id == preferred_id)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: (item.effective_at, item.id))[-1]

    def preflight(self, provider: str, model: str, pricing_ref: str | None = None) -> PriceSnapshot | None:
        """Fail before a remote call when its configured price is unknown."""
        if provider == "codex-cli":
            if pricing_ref:
                raise HarnessError("codex-cli subscription profiles do not accept pricing_ref")
            return None
        snapshot = self.resolve(provider, model, pricing_ref)
        if snapshot is None and provider not in {"ollama", "local"} and not self.allow_unpriced_remote_calls:
            suffix = f" using pricing_ref {pricing_ref}" if pricing_ref else ""
            raise HarnessError(f"No configured price snapshot for {provider}/{model}{suffix}")
        return snapshot

    @staticmethod
    def cost(response: ProviderResponse, snapshot: PriceSnapshot) -> int:
        total_input = max(0, int(response.input_tokens or 0))
        cached = max(0, int(response.cached_input_tokens or 0))
        cache_write = max(0, int(response.cache_write_input_tokens or 0))
        if cached > total_input and snapshot.provider != "anthropic":
            raise HarnessError("Cached input token count exceeds total input tokens")
        # OpenAI and Gemini report cached input as a subset of input. Anthropic
        # reports uncached, cache-read, and cache-write categories separately.
        uncached = total_input if snapshot.provider == "anthropic" else total_input - cached
        billed_output = max(0, int(response.billed_output_tokens if response.billed_output_tokens is not None else response.output_tokens or 0))
        numerator = (
            uncached * snapshot.input_per_million_microusd
            + cached * snapshot.cached_input_per_million_microusd
            + cache_write * snapshot.cache_write_per_million_microusd
            + billed_output * snapshot.output_per_million_microusd
        )
        return (numerator + 999_999) // 1_000_000

    def record(
        self,
        response: ProviderResponse,
        *,
        run_id: str,
        node_id: str,
        agent_id: str,
        role: str,
        provider_profile_id: str,
        provider: str,
        model: str,
        latency_ms: int,
        pricing_ref: str | None = None,
        request_id: str | None = None,
    ) -> UsageRecord:
        snapshot = self.resolve(provider, model, pricing_ref)
        if provider == "codex-cli":
            price_status, cost, snapshot_id = "subscription-unpriced", None, None
        elif provider in {"ollama", "local"} and snapshot is None:
            price_status, cost, snapshot_id = "local-zero", 0, None
        elif snapshot is None:
            if not self.allow_unpriced_remote_calls:
                price_status = "unknown"
            else:
                price_status = "unpriced-allowed"
            cost, snapshot_id = None, None
        else:
            price_status, cost, snapshot_id = "configured", self.cost(response, snapshot), snapshot.id
        return UsageRecord(
            run_id=run_id,
            request_id=request_id or uuid.uuid4().hex,
            node_id=node_id,
            agent_id=agent_id,
            role=role,
            provider_profile_id=provider_profile_id,
            provider=provider,
            model=model,
            input_tokens=response.input_tokens,
            cached_input_tokens=response.cached_input_tokens,
            cache_write_input_tokens=response.cache_write_input_tokens,
            output_tokens=response.output_tokens,
            reasoning_tokens=response.reasoning_tokens,
            tool_use_tokens=response.tool_use_tokens,
            billed_output_tokens=response.billed_output_tokens,
            latency_ms=max(0, latency_ms),
            cost_microusd=cost,
            price_status=price_status,
            price_snapshot_id=snapshot_id,
            created_at_ms=int(time.time() * 1000),
        )
