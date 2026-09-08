"""Versioned, non-secret observations of a completed browser relay.

These are historical durations, never cached configuration or completion proof.
Unknown versions and arbitrary provider payloads cannot become timing metadata.
"""

FIELDS = (
    "queue_ms", "prepare_ms", "submit_ms", "first_reply_ms", "reply_wait_ms",
    "capture_tail_ms", "browser_total_ms",
)


def frozen(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or str(value.get("timing_version")) != "1":
        return {}
    result = {"timing_version": 1}
    for name in FIELDS:
        number = value.get(name)
        if isinstance(number, bool):
            continue
        if isinstance(number, str) and number.isascii() and number.isdecimal():
            number = int(number) if len(number) <= 8 else None
        if isinstance(number, int) and 0 <= number <= 86_400_000:
            result[name] = number
    return result if len(result) > 1 else {}


def from_response(response: object) -> dict[str, int]:
    raw = getattr(response, "raw", None)
    return frozen(raw.get("relay_timing")) if isinstance(raw, dict) else {}
