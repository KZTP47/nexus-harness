"""Read-only delivery observations. No scheduler, retry or permission authority."""
from __future__ import annotations
from typing import Any


def task_delivery(goal: dict[str, Any], task: dict[str, Any]) -> dict[str, str]:
    state = task.get('state')
    if goal.get('status') == 'complete' and state == 'complete':
        return {'stage': 'verified', 'label': 'Project result verified'}
    if task.get('outcome_unknown'):
        return {'stage': 'unknown', 'label': 'Delivery outcome unknown; existing recovery applies'}
    if state == 'running':
        return {'stage': 'dispatched', 'label': 'Agent working; conversation remains active'}
    if state in {'complete', 'pending_apply', 'waiting_review'}:
        return {'stage': 'response_recorded', 'label': 'Response recorded; project verification remains separate'}
    if state in {'failed', 'blocked'}:
        return {'stage': 'needs_attention', 'label': 'Task needs attention; other eligible work keeps its existing schedule'}
    if state == 'cancelled':
        return {'stage': 'cancelled', 'label': 'Cancelled'}
    return {'stage': 'queued', 'label': 'Queued for an eligible agent turn'}
