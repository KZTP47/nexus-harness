from __future__ import annotations

import hashlib
import json
import re
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable


MINI_SELECTION_SALT = "our-harness-swebench-verified-mini-v1"
REQUIRED_PREDICTION_FIELDS = {"instance_id", "model_name_or_path", "model_patch"}
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class ExternalBenchmarkError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def external_benchmark_manifest() -> dict[str, Any]:
    raw = files("our_harness.templates").joinpath("external_benchmark_manifest.json").read_bytes()
    return json.loads(raw)


def _suite(suite_id: str) -> dict[str, Any]:
    manifest = external_benchmark_manifest()
    for suite in manifest["suites"]:
        if suite["id"] == suite_id:
            return suite
    choices = ", ".join(item["id"] for item in manifest["suites"])
    raise ExternalBenchmarkError(f"Unknown suite {suite_id!r}; choose one of: {choices}")


def read_instance_ids(path: str | Path) -> list[str]:
    source = Path(path)
    try:
        values = [line.strip() for line in source.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        raise ExternalBenchmarkError(f"Cannot read instance ID file {source}: {exc}") from exc
    values = [value for value in values if value and not value.startswith("#")]
    if not values:
        raise ExternalBenchmarkError("Instance ID file is empty")
    if len(values) != len(set(values)):
        raise ExternalBenchmarkError("Instance ID file contains duplicates")
    if any(any(character.isspace() for character in value) for value in values):
        raise ExternalBenchmarkError("Instance IDs must not contain whitespace")
    return values


def select_verified_mini(instance_ids: Iterable[str], count: int = 50) -> list[str]:
    values = sorted(set(instance_ids))
    if len(values) < count:
        raise ExternalBenchmarkError(f"Need at least {count} unique instance IDs; received {len(values)}")
    return sorted(
        values,
        key=lambda value: (_sha256(f"{MINI_SELECTION_SALT}\0{value}".encode("utf-8")), value),
    )[:count]


def load_fairness_lock(path: str | Path) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalBenchmarkError(f"Cannot read fairness lock {source}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ExternalBenchmarkError("Fairness lock must be an object with schema_version 1")
    for field in ("model", "dataset", "evaluator", "budgets", "tool_policy", "harnesses"):
        if field not in value:
            raise ExternalBenchmarkError(f"Fairness lock is missing {field!r}")
    if not isinstance(value["model"], dict) or not value["model"].get("name"):
        raise ExternalBenchmarkError("Fairness lock model.name must be a non-empty string")
    if any("replace" in str(value["model"].get(field, "")).casefold() for field in ("provider", "name")):
        raise ExternalBenchmarkError("Fairness lock still contains model placeholders")
    for owner in ("dataset", "evaluator"):
        item = value[owner]
        if not isinstance(item, dict) or not isinstance(item.get("revision"), str) or not item["revision"].strip():
            raise ExternalBenchmarkError(f"Fairness lock {owner}.revision must be a non-empty string")
        if "replace" in item["revision"].casefold():
            raise ExternalBenchmarkError(f"Fairness lock still contains a {owner} revision placeholder")
    if not isinstance(value["budgets"], dict):
        raise ExternalBenchmarkError("Fairness lock budgets must be an object")
    required_budgets = {"max_turns", "wall_time_seconds", "max_cost_usd"}
    missing_budgets = required_budgets - set(value["budgets"])
    if missing_budgets:
        raise ExternalBenchmarkError(f"Fairness lock budgets are missing: {', '.join(sorted(missing_budgets))}")
    if not isinstance(value["budgets"]["max_turns"], int) or value["budgets"]["max_turns"] < 1:
        raise ExternalBenchmarkError("Fairness lock budgets.max_turns must be a positive integer")
    if not isinstance(value["budgets"]["wall_time_seconds"], int) or value["budgets"]["wall_time_seconds"] < 1:
        raise ExternalBenchmarkError("Fairness lock budgets.wall_time_seconds must be a positive integer")
    max_cost = value["budgets"]["max_cost_usd"]
    if max_cost is not None and (not isinstance(max_cost, (int, float)) or isinstance(max_cost, bool) or max_cost < 0):
        raise ExternalBenchmarkError("Fairness lock budgets.max_cost_usd must be null or a non-negative number")
    if not isinstance(value["tool_policy"], list) or not all(isinstance(item, str) and item for item in value["tool_policy"]):
        raise ExternalBenchmarkError("Fairness lock tool_policy must be a list of non-empty strings")
    harnesses = value["harnesses"]
    if not isinstance(harnesses, list):
        raise ExternalBenchmarkError("Fairness lock harnesses must be a list")
    revisions: dict[str, str] = {}
    for item in harnesses:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("revision"), str):
            raise ExternalBenchmarkError("Each fairness-lock harness needs string id and revision fields")
        if not item["revision"].strip():
            raise ExternalBenchmarkError(f"Harness {item['id']!r} has an empty revision")
        if "replace" in item["revision"].casefold():
            raise ExternalBenchmarkError(f"Harness {item['id']!r} still has a revision placeholder")
        if item["id"] in revisions:
            raise ExternalBenchmarkError(f"Fairness lock repeats harness {item['id']!r}")
        revisions[item["id"]] = item["revision"]
    required_harnesses = {item["id"] for item in external_benchmark_manifest()["comparison_harnesses"]}
    missing_harnesses = required_harnesses - set(revisions)
    if missing_harnesses:
        raise ExternalBenchmarkError(f"Fairness lock is missing harnesses: {', '.join(sorted(missing_harnesses))}")
    return value, _sha256(raw)


def validate_predictions(path: str | Path, expected_instance_ids: Iterable[str] | None = None) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ExternalBenchmarkError(f"Cannot read predictions {source}: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExternalBenchmarkError(f"Predictions line {line_number} is not JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ExternalBenchmarkError(f"Predictions line {line_number} must be an object")
        missing = REQUIRED_PREDICTION_FIELDS - set(record)
        if missing:
            raise ExternalBenchmarkError(f"Predictions line {line_number} is missing: {', '.join(sorted(missing))}")
        for field in REQUIRED_PREDICTION_FIELDS:
            if not isinstance(record[field], str):
                raise ExternalBenchmarkError(f"Predictions line {line_number} field {field!r} must be a string")
        if not record["instance_id"] or not record["model_name_or_path"]:
            raise ExternalBenchmarkError(f"Predictions line {line_number} has an empty identity field")
        records.append(record)
    if not records:
        raise ExternalBenchmarkError("Predictions file contains no records")
    instance_ids = [record["instance_id"] for record in records]
    if len(instance_ids) != len(set(instance_ids)):
        raise ExternalBenchmarkError("Predictions contain duplicate instance IDs")
    expected = set(expected_instance_ids or [])
    if expected and set(instance_ids) != expected:
        missing = sorted(expected - set(instance_ids))
        extra = sorted(set(instance_ids) - expected)
        raise ExternalBenchmarkError(
            f"Predictions do not match the frozen instance set; missing={missing[:5]}, extra={extra[:5]}"
        )
    model_names = sorted({record["model_name_or_path"] for record in records})
    if len(model_names) != 1:
        raise ExternalBenchmarkError("Predictions must use one model_name_or_path label per file")
    return {
        "path": str(source.resolve()),
        "sha256": _sha256(raw),
        "records": len(records),
        "model_name_or_path": model_names[0],
        "empty_patches": sum(1 for record in records if not record["model_patch"].strip()),
    }


def build_evaluation_plan(
    *,
    suite_id: str,
    predictions_path: str | Path,
    fairness_lock_path: str | Path,
    run_id: str,
    max_workers: int = 4,
    instance_ids_path: str | Path | None = None,
    all_instance_ids_path: str | Path | None = None,
) -> dict[str, Any]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ExternalBenchmarkError("run_id must contain only letters, digits, dot, underscore, or hyphen (1-80 chars)")
    if not 1 <= max_workers <= 64:
        raise ExternalBenchmarkError("max_workers must be between 1 and 64")
    suite = _suite(suite_id)
    fairness_lock, fairness_lock_sha256 = load_fairness_lock(fairness_lock_path)
    instance_ids: list[str] = []
    all_instance_ids: list[str] = []
    if suite["selection"] == "frozen_instance_ids":
        if instance_ids_path is None or all_instance_ids_path is None:
            raise ExternalBenchmarkError(f"Suite {suite_id!r} requires --instance-ids and --all-instance-ids")
        instance_ids = read_instance_ids(instance_ids_path)
        all_instance_ids = read_instance_ids(all_instance_ids_path)
        if len(all_instance_ids) != suite["source_task_count"]:
            raise ExternalBenchmarkError(
                f"Suite {suite_id!r} requires all {suite['source_task_count']} source instance IDs"
            )
        if len(instance_ids) != suite["task_count"]:
            raise ExternalBenchmarkError(
                f"Suite {suite_id!r} requires exactly {suite['task_count']} frozen instance IDs"
            )
        if instance_ids != select_verified_mini(all_instance_ids, suite["task_count"]):
            raise ExternalBenchmarkError("Frozen mini instance IDs do not match the manifest selection rule")
    elif instance_ids_path is not None or all_instance_ids_path is not None:
        raise ExternalBenchmarkError(f"Suite {suite_id!r} does not accept instance-ID inputs")

    predictions = Path(predictions_path)
    prediction_record: dict[str, Any] = {"path": str(predictions.resolve()), "status": "pending"}
    if predictions.is_file():
        prediction_record = {"status": "validated", **validate_predictions(predictions, instance_ids or None)}
        if prediction_record["model_name_or_path"] != fairness_lock["model"]["name"]:
            raise ExternalBenchmarkError(
                "Predictions model_name_or_path does not match the frozen fairness-lock model name"
            )

    argv = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        suite["dataset_name"],
        "--split",
        suite["split"],
        "--predictions_path",
        str(predictions.resolve()),
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]
    if instance_ids:
        argv.extend(["--instance_ids", *instance_ids])
    manifest_raw = files("our_harness.templates").joinpath("external_benchmark_manifest.json").read_bytes()
    return {
        "schema_version": 1,
        "manifest_sha256": _sha256(manifest_raw),
        "suite": suite,
        "run_id": run_id,
        "fairness_lock": fairness_lock,
        "fairness_lock_sha256": fairness_lock_sha256,
        "instance_ids": instance_ids,
        "instance_ids_sha256": _sha256(_canonical(instance_ids)) if instance_ids else None,
        "source_instance_ids_sha256": _sha256(_canonical(all_instance_ids)) if all_instance_ids else None,
        "predictions": prediction_record,
        "official_evaluator_argv": argv,
        "execution_note": "Run the argv directly without a shell after the source, fairness lock, and instance set are frozen.",
    }
