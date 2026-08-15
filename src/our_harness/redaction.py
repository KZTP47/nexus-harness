from __future__ import annotations

import copy
import os
import re
from typing import Any

from .config import LoadedConfig


REDACTED = "[REDACTED]"
_SENSITIVE_NAME = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|client[_-]?secret|credential|cookie|passwd|password|private[_-]?key|secret)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|client[_-]?secret|credential|cookie|passwd|password|private[_-]?key|secret)\b\s*[:=]\s*)(['\"]?)([^\s,'\";}{]+)(?:\2)"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})(?![A-Za-z0-9])"
)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])")


class CredentialRedactor:
    """Remove credential material before network transmission or persistence."""

    def __init__(self, config: LoadedConfig | None = None):
        configured_names = {"HARNESS_API_KEY"}
        if config is not None:
            configured = str(config.get("provider.api_key_env") or "")
            if configured:
                configured_names.add(configured)
            profiles = config.get("providers", {})
            if isinstance(profiles, dict):
                for profile in profiles.values():
                    if not isinstance(profile, dict):
                        continue
                    profile_name = profile.get("api_key_env")
                    if isinstance(profile_name, str) and profile_name:
                        configured_names.add(profile_name)
        configured_secrets = {
            os.environ[name] for name in configured_names if os.environ.get(name, "")
        }
        ambient_secrets = {
            value
            for name, value in os.environ.items()
            if _SENSITIVE_NAME.search(name) and len(value) >= 6
        }
        self._secrets = sorted(
            configured_secrets | ambient_secrets,
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        output = value
        for secret in self._secrets:
            output = output.replace(secret, REDACTED)
        output = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", output)
        output = _BEARER.sub(r"\1" + REDACTED, output)
        output = _KNOWN_TOKEN.sub(REDACTED, output)
        output = _JWT.sub(REDACTED, output)
        output = _ASSIGNMENT.sub(lambda match: match.group(1) + REDACTED, output)
        return output

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                key: REDACTED if isinstance(key, str) and _SENSITIVE_NAME.search(key) else self.value(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self.value(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.value(child) for child in value)
        return copy.deepcopy(value)
