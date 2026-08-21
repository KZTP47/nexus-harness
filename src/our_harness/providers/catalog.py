from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool
    structured_output: bool
    native_tools: bool
    parallel_tools: bool
    continuation: bool
    prompt_caching: bool
    embeddings: bool
    reasoning_controls: bool
    model_discovery: bool


@dataclass(frozen=True)
class ModelCatalogEntry:
    provider: str
    model: str
    display_name: str
    source_url: str
    catalog_date: str = "2026-08-14"


PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "openai": ProviderCapabilities(True, True, True, True, True, True, True, True, True),
    "anthropic": ProviderCapabilities(True, True, True, True, True, True, False, True, True),
    "gemini": ProviderCapabilities(False, True, True, True, True, True, True, True, True),
    "ollama": ProviderCapabilities(True, True, True, True, True, False, True, False, True),
    "openai-compatible": ProviderCapabilities(True, False, True, True, True, False, True, False, False),
    "local": ProviderCapabilities(False, True, False, False, False, False, False, False, False),
    "codex-cli": ProviderCapabilities(False, True, False, False, False, False, False, True, False),
    "claude-cli": ProviderCapabilities(False, False, False, False, False, False, False, False, False),
    "copilot-cli": ProviderCapabilities(False, False, False, False, False, False, False, False, False),
    "assistant-cli": ProviderCapabilities(False, False, False, False, False, False, False, False, False),
    "gemini-cli": ProviderCapabilities(False, False, False, False, False, False, False, False, False),
    "m365-copilot": ProviderCapabilities(False, False, False, False, False, False, False, False, False),
}


# This is an offline onboarding list, not a claim that the account can use each
# model. Provider IDs remain free-form and validation never depends on this list.
MODEL_CATALOG: tuple[ModelCatalogEntry, ...] = (
    ModelCatalogEntry("openai", "gpt-5.6-sol", "GPT-5.6 Sol", "https://developers.openai.com/api/docs/models"),
    ModelCatalogEntry("openai", "gpt-5.6-terra", "GPT-5.6 Terra", "https://developers.openai.com/api/docs/models"),
    ModelCatalogEntry("openai", "gpt-5.6-luna", "GPT-5.6 Luna", "https://developers.openai.com/api/docs/models"),
    ModelCatalogEntry("anthropic", "claude-fable-5", "Claude Fable 5", "https://platform.claude.com/docs/en/about-claude/models/overview"),
    ModelCatalogEntry("anthropic", "claude-opus-5", "Claude Opus 5", "https://platform.claude.com/docs/en/about-claude/models/overview"),
    ModelCatalogEntry("anthropic", "claude-sonnet-5", "Claude Sonnet 5", "https://platform.claude.com/docs/en/about-claude/models/overview"),
    ModelCatalogEntry("anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5", "https://platform.claude.com/docs/en/about-claude/models/overview"),
    ModelCatalogEntry("gemini", "gemini-3.6-flash", "Gemini 3.6 Flash", "https://ai.google.dev/api/models"),
    ModelCatalogEntry("gemini", "gemini-3.5-flash", "Gemini 3.5 Flash", "https://ai.google.dev/api/models"),
    ModelCatalogEntry("gemini", "gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "https://ai.google.dev/api/models"),
)


def capabilities_for(provider: str) -> ProviderCapabilities:
    return PROVIDER_CAPABILITIES[provider]


def offline_models(provider: str) -> list[ModelCatalogEntry]:
    return [entry for entry in MODEL_CATALOG if entry.provider == provider]
