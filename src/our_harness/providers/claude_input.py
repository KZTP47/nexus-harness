"""Native Claude image messages, adapted from t3code's ClaudeAdapter.

Source: apps/server/src/provider/Layers/ClaudeAdapter.ts,
buildUserMessage/buildClaudeImageContentBlock/buildUserMessageEffect, commit
eb115063634c416c6362cc407f8572cb0c136ddf. See docs/T3CODE_ADOPTION.md.
Images are actual content blocks; a plaintext filename is not visual input.
"""

from __future__ import annotations

import base64
import json

from ..models import HarnessError, ProviderRequest
from .image_inputs import read_image_inputs


def build_user_message(request: ProviderRequest, prompt: str) -> str:
    content: list[dict] = []
    for mime_type, raw in read_image_inputs(request.attachments):
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": mime_type,
            "data": base64.b64encode(raw).decode("ascii"),
        }})
    # As in t3code, images precede the final text block. This also preserves
    # Claude's last-text-block semantics without substituting a file-read tool.
    content.append({"type": "text", "text": prompt})
    return json.dumps({
        "type": "user", "session_id": "", "parent_tool_use_id": None,
        "message": {"role": "user", "content": content},
    }, ensure_ascii=False) + "\n"


def terminal_result(stdout: str) -> str:
    """Only a terminal result proves a streamed Claude turn finished."""
    terminal = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarnessError("Claude returned a malformed image-input response stream") from exc
        if not isinstance(event, dict):
            raise HarnessError("Claude returned a malformed image-input response event")
        if event.get("type") == "error":
            raise HarnessError("Claude reported a failed image-input turn")
        if event.get("type") == "result":
            if terminal is not None:
                raise HarnessError("Claude returned more than one terminal result")
            terminal = event
    if terminal is None:
        raise HarnessError("Claude stopped without a terminal image-input result")
    return json.dumps(terminal, ensure_ascii=False)
