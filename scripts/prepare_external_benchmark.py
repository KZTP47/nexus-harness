from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from our_harness.external_benchmark import (  # noqa: E402
    ExternalBenchmarkError,
    build_evaluation_plan,
    read_instance_ids,
    select_verified_mini,
    validate_predictions,
)


def _write_new(path: Path, text: str) -> None:
    if path.exists():
        raise ExternalBenchmarkError(f"Refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        try:
            os.link(temporary, path)
        except OSError:
            with path.open("x", encoding="utf-8") as destination:
                destination.write(text)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze or validate an external SWE-bench evaluation plan")
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select-mini", help="Select the fixed 50-task Verified smoke subset")
    select.add_argument("--all-instance-ids", required=True, type=Path)
    select.add_argument("--output", required=True, type=Path)

    plan = subparsers.add_parser("plan", help="Build a non-executing official-evaluator plan")
    plan.add_argument("--suite", required=True)
    plan.add_argument("--predictions", required=True, type=Path)
    plan.add_argument("--fairness-lock", required=True, type=Path)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--max-workers", type=int, default=4)
    plan.add_argument("--instance-ids", type=Path)
    plan.add_argument("--all-instance-ids", type=Path)
    plan.add_argument("--output", type=Path)

    validate = subparsers.add_parser("validate-predictions", help="Check official JSONL prediction fields and coverage")
    validate.add_argument("--predictions", required=True, type=Path)
    validate.add_argument("--instance-ids", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "select-mini":
            selected = select_verified_mini(read_instance_ids(args.all_instance_ids))
            _write_new(args.output, "".join(f"{item}\n" for item in selected))
            print(json.dumps({"output": str(args.output.resolve()), "instances": len(selected)}, sort_keys=True))
            return 0
        if args.command == "validate-predictions":
            expected = read_instance_ids(args.instance_ids) if args.instance_ids else None
            print(json.dumps(validate_predictions(args.predictions, expected), indent=2, sort_keys=True))
            return 0
        result = build_evaluation_plan(
            suite_id=args.suite,
            predictions_path=args.predictions,
            fairness_lock_path=args.fairness_lock,
            run_id=args.run_id,
            max_workers=args.max_workers,
            instance_ids_path=args.instance_ids,
            all_instance_ids_path=args.all_instance_ids,
        )
        rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            _write_new(args.output, rendered)
        else:
            print(rendered, end="")
        return 0
    except ExternalBenchmarkError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
