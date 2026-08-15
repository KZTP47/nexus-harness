from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .memory import MemoryStore
from .models import HarnessError


REFINEMENT_KINDS = {"prompt", "memory", "skill", "subagent"}


@dataclass(frozen=True)
class RefinementPlan:
    kind: str
    name: str
    baseline_id: str | None
    body: str
    evidence: list[str]
    expected_outcome: str
    sha256: str


class RefinementManager:
    """Versioned supplemental state. The base system policy is not stored here."""

    def __init__(self, memory: MemoryStore):
        self.memory = memory

    def current(self, kind: str, name: str) -> dict[str, Any] | None:
        row = self.memory.connection.execute(
            "SELECT * FROM prompt_versions WHERE kind=? AND name=? AND active=1 ORDER BY created_at DESC LIMIT 1",
            (kind, name),
        ).fetchone()
        return dict(row) if row else None

    def plan(self, kind: str, name: str, body: str, evidence: list[str], expected_outcome: str) -> RefinementPlan:
        if kind not in REFINEMENT_KINDS:
            raise HarnessError(f"Unknown refinement kind: {kind}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise HarnessError("Refinement name must contain letters, digits, hyphens, or underscores")
        if not body.strip() or len(body) > 32_000:
            raise HarnessError("Refinement body must contain 1 to 32000 characters")
        if not evidence or not expected_outcome.strip():
            raise HarnessError("Refinement requires evidence and an expected outcome")
        safe_body = self.memory.redact_text(body)
        safe_evidence = self.memory.redact_value(evidence)
        safe_outcome = self.memory.redact_text(expected_outcome)
        if not isinstance(safe_evidence, list) or not all(isinstance(item, str) for item in safe_evidence):
            raise HarnessError("Refinement evidence must be text")
        current = self.current(kind, name)
        digest = hashlib.sha256(safe_body.encode()).hexdigest()
        return RefinementPlan(kind, name, current["id"] if current else None, safe_body, safe_evidence, safe_outcome, digest)

    @staticmethod
    def _validated_verification(verification: list[dict[str, Any]], require_pass: bool = True) -> list[dict[str, Any]]:
        if not isinstance(verification, list) or not verification:
            raise HarnessError("Refinement requires verification records")
        normalized: list[dict[str, Any]] = []
        for item in verification:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise HarnessError("Every refinement verification record needs a name")
            if not isinstance(item.get("passed"), bool):
                raise HarnessError("Every refinement verification record needs a boolean passed value")
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                raise HarnessError("Every refinement verification record needs concrete evidence")
            normalized.append({**item, "name": item["name"].strip(), "evidence": item["evidence"].strip()})
        if require_pass and any(item["passed"] is not True for item in normalized):
            raise HarnessError("All refinement verification records must pass")
        return normalized

    def _insert_version(
        self,
        plan: RefinementPlan,
        verification: list[dict[str, Any]],
        review_verdict: str,
    ) -> str:
        current = self.current(plan.kind, plan.name)
        current_id = current["id"] if current else None
        if current_id != plan.baseline_id:
            raise HarnessError("Refinement baseline changed; make a new plan")
        version_id = uuid.uuid4().hex
        metadata = {
            "evidence": plan.evidence,
            "expected_outcome": plan.expected_outcome,
            "verification": verification,
            "verification_sha256": hashlib.sha256(
                json.dumps(verification, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "review_verdict": review_verdict,
            "sha256": plan.sha256,
        }
        self.memory.connection.execute("UPDATE prompt_versions SET active=0 WHERE kind=? AND name=?", (plan.kind, plan.name))
        self.memory.connection.execute(
            "INSERT INTO prompt_versions(id,kind,name,body,parent_id,active,created_at,metadata_json) VALUES(?,?,?,?,?,1,?,?)",
            (version_id, plan.kind, plan.name, plan.body, plan.baseline_id, int(time.time()), json.dumps(metadata, sort_keys=True)),
        )
        return version_id

    def apply(self, plan: RefinementPlan, verification: list[dict[str, Any]], review_verdict: str) -> str:
        if review_verdict.upper() != "PASS":
            raise HarnessError("Refinement requires a PASS review verdict")
        checked = self._validated_verification(verification)
        with self.memory.connection:
            return self._insert_version(plan, checked, "PASS")

    def rollback(
        self,
        kind: str,
        name: str,
        target_id: str,
        verification: list[dict[str, Any]],
        review_verdict: str,
    ) -> str:
        target = self.memory.connection.execute(
            "SELECT * FROM prompt_versions WHERE id=? AND kind=? AND name=?", (target_id, kind, name)
        ).fetchone()
        if not target:
            raise HarnessError(f"Refinement version not found: {target_id}")
        current = self.current(kind, name)
        plan = self.plan(kind, name, target["body"], [f"rollback:{target_id}"], "Restore a prior reviewed version")
        if current and plan.baseline_id != current["id"]:
            raise HarnessError("Refinement changed during rollback")
        return self.apply(plan, verification, review_verdict)

    def overview(self, limit: int = 16) -> str:
        rows = self.memory.connection.execute(
            "SELECT kind,name,id,body,metadata_json FROM prompt_versions WHERE active=1 ORDER BY kind,name LIMIT ?", (limit,)
        ).fetchall()
        blocks = []
        for row in rows:
            body = row["body"][:1200]
            blocks.append(f"[{row['kind']}:{row['name']} version={row['id']}]\n{body}")
        return "\n\n".join(blocks)

    def stage_candidate(self, plan: RefinementPlan) -> str:
        candidate_id = uuid.uuid4().hex
        with self.memory.connection:
            self.memory.connection.execute(
                "INSERT INTO refinement_candidates(id,kind,name,body,baseline_id,evidence_json,expected_outcome,status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
                (candidate_id, plan.kind, plan.name, plan.body, plan.baseline_id, json.dumps(plan.evidence), plan.expected_outcome, int(time.time())),
            )
        return candidate_id

    @staticmethod
    def _candidate_dict(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        raw_verification = value.pop("verification_json", None)
        value["verification"] = json.loads(raw_verification) if raw_verification else []
        return value

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.memory.connection.execute(
            "SELECT * FROM refinement_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not row:
            raise HarnessError(f"Refinement candidate not found: {candidate_id}")
        return self._candidate_dict(row)

    def candidates(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            rows = self.memory.connection.execute(
                "SELECT * FROM refinement_candidates WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = self.memory.connection.execute(
                "SELECT * FROM refinement_candidates ORDER BY created_at DESC"
            ).fetchall()
        return [self._candidate_dict(row) for row in rows]

    def review_candidate(
        self,
        candidate_id: str,
        verification: list[dict[str, Any]],
        review_verdict: str,
        reason: str = "",
    ) -> dict[str, Any]:
        verdict = review_verdict.upper()
        if verdict not in {"PASS", "BLOCK"}:
            raise HarnessError("Candidate review verdict must be PASS or BLOCK")
        if not reason.strip():
            raise HarnessError("Candidate review requires a reason")
        checked = self._validated_verification(verification, require_pass=verdict == "PASS")
        checked = self.memory.redact_value(checked)
        reason = self.memory.redact_text(reason.strip())
        status = "reviewed" if verdict == "PASS" else "rejected"
        candidate = self.candidate(candidate_id)
        if candidate["status"] != "pending":
            raise HarnessError(f"Refinement candidate is not pending: {candidate['status']}")
        binding_payload = {
            key: candidate[key]
            for key in ("id", "kind", "name", "body", "baseline_id", "evidence", "expected_outcome")
        }
        binding_payload.update({"verification": checked, "review_verdict": verdict, "reason": reason})
        review_binding = hashlib.sha256(
            json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.memory.connection:
            cursor = self.memory.connection.execute(
                "UPDATE refinement_candidates SET status=?,verification_json=?,review_verdict=?,decision_reason=?,decided_at=?,review_binding_sha256=? "
                "WHERE id=? AND status='pending'",
                (status, json.dumps(checked, sort_keys=True), verdict, reason, int(time.time()), review_binding, candidate_id),
            )
            if cursor.rowcount != 1:
                existing = self.candidate(candidate_id)
                raise HarnessError(f"Refinement candidate is not pending: {existing['status']}")
        return self.candidate(candidate_id)

    def promote_candidate(self, candidate_id: str) -> str:
        candidate = self.candidate(candidate_id)
        if candidate["status"] != "reviewed" or candidate.get("review_verdict") != "PASS":
            raise HarnessError("Refinement candidate requires a passing review before promotion")
        checked = self._validated_verification(candidate["verification"])
        binding_payload = {
            key: candidate[key]
            for key in ("id", "kind", "name", "body", "baseline_id", "evidence", "expected_outcome")
        }
        binding_payload.update(
            {
                "verification": checked,
                "review_verdict": candidate["review_verdict"],
                "reason": candidate["decision_reason"],
            }
        )
        expected_binding = hashlib.sha256(
            json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if candidate.get("review_binding_sha256") != expected_binding:
            raise HarnessError("Refinement candidate review evidence no longer matches the candidate")
        plan = RefinementPlan(
            candidate["kind"],
            candidate["name"],
            candidate["baseline_id"],
            candidate["body"],
            candidate["evidence"],
            candidate["expected_outcome"],
            hashlib.sha256(candidate["body"].encode()).hexdigest(),
        )
        with self.memory.connection:
            version_id = self._insert_version(plan, checked, "PASS")
            cursor = self.memory.connection.execute(
                "UPDATE refinement_candidates SET status='promoted',promoted_version_id=?,decided_at=? "
                "WHERE id=? AND status='reviewed'",
                (version_id, int(time.time()), candidate_id),
            )
            if cursor.rowcount != 1:
                raise HarnessError("Refinement candidate changed during promotion")
        return version_id

    def reject_candidate(self, candidate_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise HarnessError("Candidate rejection requires a reason")
        reason = self.memory.redact_text(reason.strip())
        with self.memory.connection:
            cursor = self.memory.connection.execute(
                "UPDATE refinement_candidates SET status='rejected',review_verdict='BLOCK',decision_reason=?,decided_at=? "
                "WHERE id=? AND status IN ('pending','reviewed')",
                (reason, int(time.time()), candidate_id),
            )
            if cursor.rowcount != 1:
                existing = self.candidate(candidate_id)
                raise HarnessError(f"Refinement candidate cannot be rejected from status: {existing['status']}")
        return self.candidate(candidate_id)
