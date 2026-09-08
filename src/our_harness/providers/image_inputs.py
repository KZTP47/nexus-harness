"""Bounded original image bytes shared by native CLI input adapters.

Adapted from t3code ClaudeAdapter.buildUserMessageEffect and
CodexAdapter.resolveAttachment at eb115063634c416c6362cc407f8572cb0c136ddf.
Nexus additionally binds the content digest when attachment metadata provides it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any

from ..models import HarnessError

SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 4_000_000
MAX_TOTAL_IMAGE_BYTES = 8_000_000


def read_image_inputs(attachments: list[dict[str, Any]]) -> list[tuple[str, bytes]]:
    images = []
    total = 0
    for attachment in attachments:
        if not isinstance(attachment, dict) or not str(attachment.get("type") or "").startswith("image/"):
            continue
        mime_type = str(attachment["type"])
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise HarnessError(f"The native provider cannot inspect the attached image type {mime_type}")
        encoded = attachment.get("data")
        try:
            if isinstance(encoded, str) and encoded:
                if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                    raise HarnessError("The image input exceeds the attachment size limit")
                raw = base64.b64decode(encoded, validate=True)
            else:
                path = Path(str(attachment.get("path") or ""))
                with path.open("rb") as stream:
                    raw = stream.read(MAX_IMAGE_BYTES + 1)
        except (OSError, ValueError, binascii.Error) as exc:
            raise HarnessError("The image input is missing or invalid; attach it again") from exc
        total += len(raw)
        if not raw or len(raw) > MAX_IMAGE_BYTES or total > MAX_TOTAL_IMAGE_BYTES:
            raise HarnessError("The image input exceeds the attachment size limit")
        expected = str(attachment.get("sha256") or "")
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise HarnessError("The image changed after it was attached; attach it again")
        images.append((mime_type, raw))
    return images
