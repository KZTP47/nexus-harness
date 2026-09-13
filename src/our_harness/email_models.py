"""Bounded CLI model suggestions; a catalog is not account entitlement."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

from .config import is_project_shared_config_trusted
from .models import HarnessError
from .providers.registry import ProviderRegistry
from .providers.catalog import offline_models
from .providers.codex_cli import _run_bounded
from .providers.subscription_cli import available

_TTL = 300.0
_CACHE = {}
_LOCK = threading.RLock()
_CODEX_DOC = 'https://learn.chatgpt.com/docs/models'
_CLAUDE_DOC = 'https://code.claude.com/docs/en/model-config'


def _fallback(registry, profile):
    items = []
    if profile.name == 'claude-cli':
        # These are documented suggestions, not a claim that an older CLI or
        # the signed-in account supports them. Aliases follow CLI configuration.
        for value, label in [('default', 'Claude account default'), ('fable', 'Fable (CLI alias)'),
                             ('opus', 'Opus (CLI alias)'), ('sonnet', 'Sonnet (CLI alias)'),
                             ('haiku', 'Haiku (CLI alias)'), ('claude-fable-5-1', 'Claude Fable 5.1')]:
            items.append(dict(id=value, label=label, source=_CLAUDE_DOC))
        for item in offline_models('anthropic'):
            items.append(dict(id=item.model, label=item.display_name, source=item.source_url))
    elif profile.name == 'codex-cli':
        # Verified official CLI documentation and installed refreshed catalog,
        # 2026-09-12. Refresh replaces these suggestions with this CLI's list.
        for value, label in [('gpt-6-astra', 'GPT-6 Astra'), ('gpt-5.6-sol', 'GPT-5.6 Sol'),
                             ('gpt-5.6-terra', 'GPT-5.6 Terra'), ('gpt-5.6-luna', 'GPT-5.6 Luna'),
                             ('gpt-5.5', 'GPT-5.5'), ('gpt-5.3-codex-spark', 'GPT-5.3 Codex Spark')]:
            items.append(dict(id=value, label=label, source=_CODEX_DOC))
    for item in registry.model_catalog(profile.id):
        items.append(dict(id=item.model, label=item.display_name, source=item.source_url or 'Configured route'))
    return _unique(items)


def _unique(items):
    result = {}
    for item in items:
        if item['id']:
            result.setdefault(item['id'], item)
    return list(result.values())


def _trusted(config, profile):
    # load_config normally rejects this authority before constructing a
    # registry. Repeat the executable boundary for directly constructed configs.
    shared = str((config.project_root / '.harness' / 'config.json').resolve())
    prefix = 'providers.' + profile.id + '.' if config.get('providers', {}) else 'provider.'
    controlled = any(key.startswith(prefix) and str(source) == shared
                     for key, source in config.provenance.items())
    return not controlled or is_project_shared_config_trusted(config.project_root)


def _fingerprint(config, profile, command):
    metadata = []
    for part in command:
        try:
            path = Path(part)
            stat = path.stat()
            metadata.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
        except (OSError, ValueError):
            pass
    material = [str(config.project_root.resolve()), profile.id, profile.name, profile.model,
                profile.auth_mode, profile.endpoint, command, metadata]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _discover(command):
    # The CLI performs its own authentication. Never read token/config files or
    # start a model turn. An empty temporary cwd excludes project instructions.
    with tempfile.TemporaryDirectory(prefix='nexus-model-catalog-') as folder:
        for bundled in (False, True):
            try:
                result = _run_bounded([*command, 'debug', 'models', *(['--bundled'] if bundled else [])],
                                      cwd=Path(folder), stdin_text=None,
                                      timeout_seconds=3.0 if bundled else 8.0,
                                      max_output_bytes=2_000_000)
                if result.timed_out or result.output_truncated or result.exit_code:
                    continue
                value = json.loads(result.stdout)
                rows = value.get('models') if isinstance(value, dict) else None
                if not isinstance(rows, list):
                    continue
                source = 'Installed Codex bundled catalog' if bundled else 'Installed Codex refreshed catalog'
                items = [dict(id=row['slug'], label=str(row.get('display_name') or row['slug']), source=source)
                         for row in rows if isinstance(row, dict) and row.get('visibility') == 'list'
                         and isinstance(row.get('slug'), str) and 0 < len(row['slug']) <= 200]
                if items:
                    return _unique(items)
            except (OSError, ValueError, HarnessError):
                continue
    return None


def model_options(config, profile_id, refresh=False):
    """Fast cached suggestions; only explicit refresh executes a trusted CLI."""
    registry = ProviderRegistry(config)
    profile = registry.profile(profile_id)
    fallback = _fallback(registry, profile)
    if profile.name != 'codex-cli' or not _trusted(config, profile):
        return fallback
    raw = list(profile.command)
    if not raw:
        return fallback
    resolved = available('codex-cli', raw)
    if not resolved:
        return fallback
    command = [resolved, *raw[1:]]
    key = _fingerprint(config, profile, command)
    with _LOCK:
        cached = _CACHE.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < _TTL:
            return [dict(item) for item in cached[1]]
    if not refresh:
        return fallback
    items = _discover(command)
    if items:
        if profile.model and profile.model not in {item['id'] for item in items}:
            items.append(dict(id=profile.model, label=profile.model, source='Configured route (not listed by CLI)'))
        with _LOCK:
            if len(_CACHE) >= 128:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic(), items)
        return [dict(item) for item in items]
    return fallback
