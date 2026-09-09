"""Explicit judge replies for deterministic workflow integration fixtures."""
import json


def judge_reply(context):
    if not context.startswith("INDEPENDENT WHOLE-GOAL CLOSEOUT JUDGE"):
        return None
    packet, _ = json.JSONDecoder().raw_decode(context[context.index("{"):])
    assert packet["scope"]["original_prompt"]
    assert packet["verification"]["status"] == "passed"
    refs = ["task:" + task["id"] for task in packet["contributions"]]
    return {"text": json.dumps({
        "action": "complete", "summary": "Independent fixture judge checked the whole request",
        "evidence": ["review-packet:" + packet["fingerprint"]], "risk": "low", "changes": [],
        "needs_files": [], "tool_calls": [], "tasks": [], "handoff_agent_id": "", "questions": [],
        "review_verdict": "approve", "review_findings": ["The submitted fixture satisfies every criterion."],
        "criteria_evidence": [{"criterion": criterion, "evidence_refs": refs}
            for criterion in packet["scope"]["acceptance_criteria"]],
    })}
