"""Read-only team status inquiries; never scheduling or objective authority."""
import re
import time


def is_inquiry(payload):
    if not isinstance(payload, dict) or payload.get("attachments"):
        return False
    words = re.sub(r"\s+", " ", str(payload.get("text") or "").lower()).strip(" ?!.\t\r\n")
    # Full matches exclude mixed questions plus work instructions.
    return bool(re.fullmatch(
        r"(?:status(?: update)?|any (?:update|progress)(?: yet)?|"
        r"what(?:'s| is) (?:the )?(?:status|progress)|"
        r"(?:what|why)(?: the (?:hell|fuck))? is (?:it |this )?taking (?:so )?long|"
        r"why is (?:it|this) (?:so )?(?:slow|fucking slow)|"
        r"are you (?:still )?(?:working|running|stuck)|how is it going)", words))


def describe(goal):
    from .provider_wait import current
    agents = {one["id"]: one.get("name") or one["id"] for one in goal.get("agents", [])}
    bindings = {one["id"]: one.get("route_binding") for one in goal.get("agents", [])}
    parts = ["Team status: " + str(goal.get("status", "unknown")) + "."]
    for task in goal.get("tasks", []):
        name = agents.get(task.get("assigned_agent_id"), "Agent")
        state = task.get("state", "unknown")
        wait = task.get("provider_wait") or {}
        if state == "running" and task.get("provider_effect_state") == "dispatched":
            words = f"{name}: waiting for a provider reply"
            if current(task, bindings.get(task.get("assigned_agent_id"))):
                elapsed = max(0, int(time.time() - wait["started_ms"] / 1000))
                words += f" ({elapsed}s elapsed"
                if wait.get("timeout_seconds") is not None:
                    words += f", request limit {wait['timeout_seconds']:g}s"
                words += ")"
            parts.append(words + ". No final reply has arrived.")
        else:
            parts.append(f"{name}: {state}." + (" " + str(task["last_error"]) if state in {"failed", "blocked"} and task.get("last_error") else ""))
    if goal.get("status") in {"paused", "waiting_for_user", "failed"} and goal.get("note"):
        parts.append(str(goal["note"]))
    parts.append("Checking status does not restart or change the work.")
    return " ".join(parts)
