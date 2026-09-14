"""Optional request timing observations; never retry or execution authority."""
import hashlib
import json
import time


def notify(callback, *, timeout_seconds=None):
    if callback is not None:
        try:
            callback({"started_ms": int(time.time() * 1000), "timeout_seconds": timeout_seconds})
        except Exception:
            # Optional display evidence must not fail an admitted request.
            pass


def record(task, route_binding, observation):
    material = {"contract": "provider-wait/v1", "route": route_binding,
                "timeout_seconds": observation.get("timeout_seconds")}
    return {"schema_version": 1, "effect_id": task.get("provider_effect_id"),
            "contract_fingerprint_sha256": hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(),
            "started_ms": observation["started_ms"], "timeout_seconds": observation.get("timeout_seconds")}


def current(task, route_binding):
    held = task.get("provider_wait")
    if not isinstance(held, dict) or held.get("schema_version") != 1 \
            or task.get("state") != "running" or task.get("provider_effect_state") != "dispatched" \
            or not task.get("provider_effect_id") or held.get("effect_id") != task["provider_effect_id"] \
            or type(held.get("started_ms")) is not int or held["started_ms"] <= 0:
        return False
    return held.get("contract_fingerprint_sha256") == record(task, route_binding, held)["contract_fingerprint_sha256"]
