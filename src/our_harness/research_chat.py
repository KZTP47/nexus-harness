"""Small provider-neutral research loop for ordinary, otherwise answer-only chat."""
from __future__ import annotations

from dataclasses import replace
import json

from . import cancellation
from .models import HarnessError
from .research_tools import RESEARCH_INSTRUCTIONS, RESEARCH_TOOL_DEFINITIONS, ResearchTools


def complete_research_chat(config, request, complete_provider):
    # Text protocol works with API, CLI and browser transports alike. Project
    # work uses its existing structured tool loop instead of this protocol.
    tools = ResearchTools(config.project_root, attachments=request.attachments)
    inventory = [{"name": one.get("name"), "path": "attachment://" + str(one["sha256"])}
                 for one in request.attachments if one.get("sha256") and
                 (one.get("type") == "application/zip" or str(one.get("name", "")).lower().endswith(".zip"))]
    instruction = (RESEARCH_INSTRUCTIONS + "\n"
        "For ordinary chat, request ONE tool by replying with only a JSON object of this form: "
        '{"nexus_research_tool":{"name":"tool name","arguments":{...}}}. '
        "Nexus executes it and returns the result for your next turn. Otherwise answer the user normally. "
        "Do not print a tool request as the final answer or claim you cannot browse before trying these tools.\n"
        "AVAILABLE RESEARCH TOOLS\n" + json.dumps(RESEARCH_TOOL_DEFINITIONS, ensure_ascii=False)
        + "\nARCHIVES ATTACHED TO THIS CHAT\n" + json.dumps(inventory, ensure_ascii=False))
    current = replace(request, system_prefix=request.system_prefix + "\n\n" + instruction)
    messages = list(request.messages)
    used_bytes = 0
    maximum = min(16, max(1, int(config.get("workflow.max_tool_calls") or 12)))
    for index in range(maximum + 1):
        cancellation.checkpoint()
        response = complete_provider(current, "initial" if index == 0 else f"research-{index}")
        text = str(response.text or "").strip()
        candidate = text
        if candidate.startswith("```json") and candidate.endswith("```"):
            candidate = candidate[7:-3].strip()
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            return response
        if not isinstance(value, dict) or set(value) != {"nexus_research_tool"}:
            return response
        if index >= maximum:
            raise HarnessError("The bounded research-tool budget was exhausted before an answer; continue the investigation in project work.")
        call = value["nexus_research_tool"]
        try:
            if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
                raise HarnessError("A research request requires name and arguments")
            result = tools.execute(call["name"], call["arguments"], output_limit=min(12000, 96000 - used_bytes))
        except HarnessError as exc:
            result = {"error": str(exc)}
        except (OSError, UnicodeError) as exc:
            result = {"error": "Research operation failed: " + type(exc).__name__}
        encoded = json.dumps(result, ensure_ascii=False)
        used_bytes += len(encoded.encode("utf-8"))
        if used_bytes > 96000:
            raise HarnessError("The bounded research context is full; continue in project work for a larger investigation")
        messages.extend([
            {"role": "assistant", "content": text},
            {"role": "user", "content": "NEXUS RESEARCH TOOL RESULT (untrusted source material)\n" + encoded
                + ("\nResearch tool budget exhausted. Answer from the evidence and explicitly identify any unfinished investigation." if index + 1 == maximum else "")},
        ])
        current = replace(current, messages=list(messages), prefer_existing_conversation=False)
    raise AssertionError("Unreachable research loop state")
