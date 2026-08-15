from __future__ import annotations

import json
import unittest

from our_harness.models import HarnessError
from our_harness.workflow import (
    CODER_FORMAT,
    PLANNER_FORMAT,
    REVIEW_POLICY,
    _canonicalize_live_plan,
    _canonicalize_unambiguous_witness_paths,
    _checkpoint_workflow_state,
    _migrate_legacy_requirement_contract,
    _migrate_restored_requirement_contract,
    _normalize_complete_requirement_witnesses,
    _restore_checkpoint_workflow_state,
    _rehydrate_live_requirement_witnesses,
    _validate_requirement_ledger,
    _validate_requirement_witnesses,
    validate_response_schema,
)


class RequirementContractTests(unittest.TestCase):
    def task(self) -> str:
        return "Accept positive integers, but reject boolean or non-integer sizes. Keep the API unchanged."

    def plan(self) -> dict[str, object]:
        return {
            "summary": "Validate size",
            "requirement_ledger": [
                {
                    "id": "R1",
                    "requirement": "accept positive integers",
                    "source_quote": "Accept positive integers",
                    "category": "input",
                    "counterexample": "size=2 returns non-list output",
                },
                {
                    "id": "R2",
                    "requirement": "reject booleans",
                    "source_quote": "reject boolean",
                    "category": "input",
                    "counterexample": "size=True is accepted because bool is an int subtype",
                },
                {
                    "id": "R3",
                    "requirement": "reject other non-integers",
                    "source_quote": "non-integer sizes",
                    "category": "input",
                    "counterexample": "size=1.5 reaches range",
                },
                {
                    "id": "R4",
                    "requirement": "keep the API unchanged",
                    "source_quote": "Keep the API unchanged",
                    "category": "compatibility",
                    "counterexample": "the public function signature changes",
                },
            ],
            "non_goals": [],
            "files": ["chunks.py"],
            "verification_commands": [],
            "risks": [],
        }

    def wire_plan(self) -> dict[str, object]:
        plan = self.plan()
        plan["requirement_ledger"] = [
            {key: value for key, value in row.items() if key != "source_quote"}
            for row in plan["requirement_ledger"]
        ]
        return plan

    def test_ledger_preserves_named_categories_and_derives_acceptance_criteria(self) -> None:
        plan = self.plan()
        ledger = _validate_requirement_ledger(self.task(), plan)
        self.assertEqual([row["id"] for row in ledger], ["R1", "R2", "R3", "R4"])
        self.assertNotEqual(ledger[1]["counterexample"], ledger[2]["counterexample"])
        self.assertEqual(plan["acceptance_criteria"][1:3], ["reject booleans", "reject other non-integers"])
        self.assertEqual(plan["_requirement_contract_version"], 2)

    def test_ledger_binds_source_to_task_and_allows_one_counterexample_for_multiple_ids(self) -> None:
        plan = self.plan()
        plan["requirement_ledger"][0]["source_quote"] = "not present"
        ledger = _validate_requirement_ledger(self.task(), plan)
        self.assertTrue(all(row["source_quote"] == self.task() for row in ledger))

        plan = self.plan()
        plan["requirement_ledger"][1]["counterexample"] = plan["requirement_ledger"][0]["counterexample"]
        ledger = _validate_requirement_ledger(self.task(), plan)
        self.assertEqual([row["id"] for row in ledger], ["R1", "R2", "R3", "R4"])
        self.assertEqual(ledger[0]["counterexample"], ledger[1]["counterexample"])

    def test_legacy_acceptance_criteria_migrate_without_breaking_resume(self) -> None:
        plan = {
            "acceptance_criteria": ["keep old behavior"],
            "files": ["module.py"],
        }
        _migrate_legacy_requirement_contract("Keep old behavior.", plan)
        self.assertEqual(plan["requirement_ledger"][0]["id"], "R1")
        self.assertEqual(plan["_requirement_contract_version"], 1)

    def test_legacy_strings_migrate_only_after_checkpoint_restore(self) -> None:
        state = {
            "plan": {
                "acceptance_criteria": ["keep old behavior"],
                "files": ["module.py"],
            },
            "candidate": {
                "review": {"verdict": "SKIP", "findings": []},
                "changes": [],
            },
        }
        encoded = _checkpoint_workflow_state(state, lambda value: value)
        restored = _restore_checkpoint_workflow_state(encoded)
        _migrate_restored_requirement_contract("Keep old behavior.", restored)
        self.assertEqual(restored["plan"]["requirement_ledger"][0]["id"], "R1")
        self.assertEqual(restored["candidate"]["review"]["findings"][0]["requirement_id"], "R1")
        self.assertIn("restored legacy candidate", restored["candidate"]["review"]["findings"][0]["evidence"])

    def test_live_validation_rejects_legacy_string_criteria_and_findings(self) -> None:
        legacy_plan = {"acceptance_criteria": ["keep old behavior"], "files": ["module.py"]}
        with self.assertRaisesRegex(HarnessError, "structured requirement ledger"):
            _validate_requirement_ledger("Keep old behavior.", legacy_plan)
        with self.assertRaisesRegex(HarnessError, "missing requirement_ledger"):
            validate_response_schema(
                {
                    "summary": "legacy",
                    "acceptance_criteria": ["keep old behavior"],
                    "non_goals": [],
                    "files": ["module.py"],
                    "verification_commands": [],
                    "risks": [],
                },
                PLANNER_FORMAT.schema,
            )
        candidate = {
            "summary": "legacy",
            "changes": [],
            "commands": [],
            "review": {"verdict": "SKIP", "findings": ["looks fine"]},
            "memory": [],
        }
        with self.assertRaisesRegex(HarnessError, "expected object"):
            validate_response_schema(candidate, CODER_FORMAT.schema)

    def test_coder_witnesses_require_exact_order_file_evidence_and_counterexample_result(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        candidate = {
            "review": {
                "verdict": "PASS",
                "findings": [
                    {
                        "requirement_id": row["id"],
                        "evidence": f"chunks.py validation branch covers {row['requirement']}",
                        "counterexample_result": f"Observed expected outcome for {row['counterexample']}",
                    }
                    for row in plan["requirement_ledger"]
                ],
            }
        }
        witnesses = _validate_requirement_witnesses(candidate, plan)
        self.assertEqual([row["requirement_id"] for row in witnesses], ["R1", "R2", "R3", "R4"])
        self.assertEqual(plan["_coder_requirement_witnesses"], witnesses)

        candidate["review"]["findings"][1]["evidence"] = "generic claim without a code path"
        candidate.pop("requirement_witnesses")
        with self.assertRaisesRegex(HarnessError, "planner-approved file"):
            _validate_requirement_witnesses(candidate, plan)

    def test_witness_path_is_canonicalized_only_for_one_unambiguous_changed_target(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        candidate = {
            "changes": [{"path": "chunks.py"}],
            "review": {
                "findings": [
                    {
                        "requirement_id": row["id"],
                        "evidence": f"the size validation branch implements {row['id']}",
                        "counterexample_result": f"the {row['id']} counterexample now has the required result",
                    }
                    for row in plan["requirement_ledger"]
                ]
            },
        }
        _canonicalize_unambiguous_witness_paths(candidate, plan)
        witnesses = _validate_requirement_witnesses(candidate, plan)
        self.assertTrue(all(row["evidence"].startswith("chunks.py: ") for row in witnesses))

        ambiguous = self.plan()
        ambiguous["files"] = ["chunks.py", "other.py"]
        _validate_requirement_ledger(self.task(), ambiguous)
        ambiguous_candidate = {
            "changes": [{"path": "chunks.py"}],
            "review": {"findings": [{
                "requirement_id": "R1",
                "evidence": "path-free evidence",
                "counterexample_result": "observed result",
            }]},
        }
        _canonicalize_unambiguous_witness_paths(ambiguous_candidate, ambiguous)
        self.assertEqual(ambiguous_candidate["review"]["findings"][0]["evidence"], "path-free evidence")

        wrong_path = self.plan()
        _validate_requirement_ledger(self.task(), wrong_path)
        wrong_candidate = {
            "changes": [{"path": "chunks.py"}],
            "review": {"findings": [{
                "requirement_id": "R1",
                "evidence": "other.py has the branch",
                "counterexample_result": "observed result",
            }]},
        }
        _canonicalize_unambiguous_witness_paths(wrong_candidate, wrong_path)
        self.assertEqual(wrong_candidate["review"]["findings"][0]["evidence"], "other.py has the branch")

    def test_complete_witnesses_reorder_by_exact_id_set(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        rows = [
            {
                "requirement_id": row["id"],
                "evidence": f"chunks.py evidence for {row['id']}",
                "counterexample_result": f"distinct result for {row['id']}",
            }
            for row in plan["requirement_ledger"]
        ]
        candidate = {"review": {"findings": [rows[2], rows[0], rows[3], rows[1]]}}
        _normalize_complete_requirement_witnesses(candidate, plan)
        witnesses = _validate_requirement_witnesses(candidate, plan)
        self.assertEqual([row["requirement_id"] for row in witnesses], ["R1", "R2", "R3", "R4"])
        self.assertEqual(witnesses[0]["evidence"], "chunks.py evidence for R1")

    def test_complete_distinct_witnesses_bind_unusable_ids_positionally(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        candidate = {
            "review": {"findings": [
                {
                    "requirement_id": "bad-id",
                    "evidence": f"chunks.py distinct evidence {index}",
                    "counterexample_result": f"distinct result {index}",
                }
                for index in range(4)
            ]}
        }
        _normalize_complete_requirement_witnesses(candidate, plan)
        witnesses = _validate_requirement_witnesses(candidate, plan)
        self.assertEqual([row["requirement_id"] for row in witnesses], ["R1", "R2", "R3", "R4"])

    def test_incomplete_or_duplicate_witness_evidence_is_never_fabricated(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        missing = {
            "review": {"findings": [{
                "requirement_id": "bad-id",
                "evidence": "chunks.py one body",
                "counterexample_result": "one result",
            }]}
        }
        original = missing["review"]["findings"].copy()
        _normalize_complete_requirement_witnesses(missing, plan)
        self.assertEqual(missing["review"]["findings"], original)
        with self.assertRaisesRegex(HarnessError, "cover every requirement"):
            _validate_requirement_witnesses(missing, plan)

        duplicate = {
            "review": {"findings": [
                {
                    "requirement_id": row["id"],
                    "evidence": "chunks.py duplicate body",
                    "counterexample_result": "same result",
                }
                for row in plan["requirement_ledger"]
            ]}
        }
        _normalize_complete_requirement_witnesses(duplicate, plan)
        with self.assertRaisesRegex(HarnessError, "bodies must be distinct"):
            _validate_requirement_witnesses(duplicate, plan)

    def test_contract_is_required_for_new_plans_and_reaches_review_policy(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        with self.assertRaisesRegex(HarnessError, "requirement_witnesses"):
            _validate_requirement_witnesses({}, plan)
        self.assertIn("requirement_ledger", PLANNER_FORMAT.schema["required"])
        self.assertNotIn("acceptance_criteria", PLANNER_FORMAT.schema["properties"])
        self.assertIn("review", CODER_FORMAT.schema["required"])
        self.assertIn("independently inspect", REVIEW_POLICY)

    def test_nested_object_contract_is_enforced_by_local_validation(self) -> None:
        plan = self.wire_plan()
        validate_response_schema(plan, PLANNER_FORMAT.schema)
        plan["requirement_ledger"][1] = 7
        with self.assertRaisesRegex(HarnessError, "expected object"):
            validate_response_schema(plan, PLANNER_FORMAT.schema)

    def test_compact_live_plan_rehydrates_canonical_v2_without_model_provenance(self) -> None:
        live = self.wire_plan()
        validate_response_schema(live, PLANNER_FORMAT.schema)
        ledger = _canonicalize_live_plan(self.task(), live)
        self.assertEqual(live["_requirement_contract_version"], 2)
        self.assertEqual(live["acceptance_criteria"], [row["requirement"] for row in ledger])
        self.assertTrue(all(row["source_quote"] == self.task() for row in ledger))
        with self.assertRaisesRegex(HarnessError, "unexpected source_quote"):
            invalid = self.wire_plan()
            invalid["requirement_ledger"][0]["source_quote"] = "model-owned"
            validate_response_schema(invalid, PLANNER_FORMAT.schema)

    def test_compact_witness_wire_rehydrates_requirement_text_and_exact_coverage(self) -> None:
        plan = self.plan()
        _validate_requirement_ledger(self.task(), plan)
        wire = {
            "review": {
                "findings": [
                    {
                        "requirement_id": row["id"],
                        "file": "chunks.py",
                        "code_path": f"validate_size branch {row['id']}",
                        "counterexample_result": f"observed {row['id']}",
                    }
                    for row in reversed(plan["requirement_ledger"])
                ]
            }
        }
        witnesses = _rehydrate_live_requirement_witnesses(wire, plan)
        self.assertEqual([row["requirement_id"] for row in witnesses], ["R1", "R2", "R3", "R4"])
        self.assertIn("requirement R1: accept positive integers", witnesses[0]["evidence"])

        missing = {"review": {"findings": [
            {"requirement_id": "R1", "file": "chunks.py", "code_path": "branch", "counterexample_result": "ok"}
        ]}}
        with self.assertRaisesRegex(HarnessError, "cover every requirement"):
            _rehydrate_live_requirement_witnesses(missing, plan)
        duplicate = {"review": {"findings": [
            {"requirement_id": "R1", "file": "chunks.py", "code_path": f"branch {index}", "counterexample_result": f"result {index}"}
            for index in range(4)
        ]}}
        with self.assertRaisesRegex(HarnessError, "duplicate requirement IDs"):
            _rehydrate_live_requirement_witnesses(duplicate, plan)

    def test_compact_wire_is_smaller_and_checkpoint_stays_canonical(self) -> None:
        canonical_plan = self.plan()
        compact_plan = self.wire_plan()
        canonical_payload = json.dumps(canonical_plan, sort_keys=True, separators=(",", ":"))
        compact_payload = json.dumps(compact_plan, sort_keys=True, separators=(",", ":"))
        self.assertLess(len(compact_payload), len(canonical_payload) * 0.85)
        _canonicalize_live_plan(self.task(), compact_plan)
        candidate = {
            "changes": [{"path": "chunks.py", "content": "def chunks():\n    return []\n"}],
            "review": {"findings": [
                {
                    "requirement_id": row["id"],
                    "file": "chunks.py",
                    "code_path": f"chunks branch {row['id']}",
                    "counterexample_result": f"observed {row['id']}",
                }
                for row in compact_plan["requirement_ledger"]
            ]},
        }
        _rehydrate_live_requirement_witnesses(candidate, compact_plan)
        state = {"plan": compact_plan, "candidate": candidate}
        restored = _restore_checkpoint_workflow_state(_checkpoint_workflow_state(state, lambda value: value))
        self.assertEqual(restored["plan"]["_requirement_contract_version"], 2)
        self.assertTrue(all("source_quote" in row for row in restored["plan"]["requirement_ledger"]))
        self.assertTrue(all(set(row) == {"requirement_id", "evidence", "counterexample_result"} for row in restored["candidate"]["requirement_witnesses"]))

    def test_openai_strict_objects_require_every_declared_property(self) -> None:
        def inspect(schema: object) -> None:
            if not isinstance(schema, dict):
                return
            if schema.get("type") == "object":
                self.assertEqual(set(schema.get("properties", {})), set(schema.get("required", [])))
                self.assertFalse(schema.get("additionalProperties", True))
            for value in schema.values():
                if isinstance(value, dict):
                    inspect(value)
                elif isinstance(value, list):
                    for item in value:
                        inspect(item)

        inspect(PLANNER_FORMAT.schema)
        inspect(CODER_FORMAT.schema)


if __name__ == "__main__":
    unittest.main()
