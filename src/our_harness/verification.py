from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _words(command: list[str]) -> list[str]:
    return [Path(part).name.lower().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat") for part in command]


def _module(words: list[str], name: str) -> bool:
    return any(words[index:index + 2] == ["-m", name] for index in range(len(words) - 1))


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _json_field(value: object, dotted: str) -> object:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _explicit_json_contract(
    command: list[str], output: str, contracts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    contract = next((item for item in contracts if item.get("command") == command), None)
    if contract is None:
        return None
    try:
        report = json.loads(output)
    except (json.JSONDecodeError, UnicodeError):
        return None
    total = _integer(_json_field(report, str(contract["total_field"])))
    failed = _integer(_json_field(report, str(contract["failed_field"])))
    if total is None or failed is None or total <= 0 or failed != 0:
        return None
    return {"framework": "configured-json", "executed": total, "source": "trusted json-stdout contract"}


def _known_json_report(words: list[str], output: str) -> dict[str, Any] | None:
    try:
        report = json.loads(output)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if any(word in {"jest", "vitest"} for word in words) and isinstance(report, dict):
        total = _integer(report.get("numTotalTests"))
        passed = _integer(report.get("numPassedTests"))
        failed = _integer(report.get("numFailedTests"))
        if total and passed and failed == 0:
            return {"framework": "jest/vitest", "executed": total, "source": "JSON report"}
    return None


def _go_json_proof(words: list[str], output: str) -> dict[str, Any] | None:
    if not (words and words[0] == "go" and "test" in words):
        return None
    passed: set[tuple[str, str]] = set()
    failed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("Test"), str):
            continue
        identity = (str(event.get("Package") or ""), event["Test"])
        if event.get("Action") == "pass":
            passed.add(identity)
        elif event.get("Action") == "fail":
            failed = True
    if passed and not failed:
        return {"framework": "go", "executed": len(passed), "source": "go test JSON events"}
    return None


def _text_proof(command: list[str], output: str) -> dict[str, Any] | None:
    words = _words(command)
    lower = output.lower()
    joined = " ".join(str(one).replace("\\", "/").lower() for one in command)

    if _module(words, "unittest") or (words and words[0] == "unittest"):
        match = re.search(r"(?mi)^ran\s+(\d+)\s+tests?\s+in\s+", output)
        count = int(match.group(1)) if match else 0
        if count > 0 and not re.search(r"(?mi)^failed\s*\(", output):
            return {"framework": "unittest", "executed": count, "source": "Ran N tests summary"}

    if _module(words, "pytest") or any(word.startswith("pytest") for word in words):
        counts = [int(value) for value in re.findall(r"(?i)(\d+)\s+passed\b", output)]
        if counts and max(counts) > 0 and not re.search(r"(?i)\d+\s+(?:failed|error)s?\b", output):
            return {"framework": "pytest", "executed": max(counts), "source": "pytest passed summary"}

    js_wrapper = bool(words and words[0] in {"npm", "pnpm", "yarn", "bun", "npx"})
    vitest = "vitest" in joined or (js_wrapper and "vitest" in lower)
    if vitest:
        match = re.search(r"(?mi)^\s*tests?\s+(\d+)\s+passed\b", output)
        if match and int(match.group(1)) > 0 and not re.search(r"(?mi)^\s*tests?\s+.*\b\d+\s+failed\b", output):
            return {"framework": "vitest", "executed": int(match.group(1)), "source": "Vitest Tests summary"}

    jest = "jest" in joined or (js_wrapper and "test suites:" in lower)
    if jest:
        match = re.search(r"(?mi)^\s*tests:\s*(?:\d+\s+skipped,\s*)?(\d+)\s+passed,\s*(\d+)\s+total\b", output)
        if match and int(match.group(1)) > 0 and int(match.group(2)) > 0 and not re.search(r"(?mi)^\s*tests:\s*.*\b\d+\s+failed\b", output):
            return {"framework": "jest", "executed": int(match.group(2)), "source": "Jest Tests summary"}

    playwright = "playwright" in joined or bool(js_wrapper and re.search(r"(?mi)^running\s+\d+\s+tests?\s+using\b", output))
    if playwright:
        collected = re.search(r"(?mi)^running\s+(\d+)\s+tests?\s+using\b", output)
        passed = re.search(r"(?mi)^\s*(\d+)\s+passed\s+\(", output)
        if collected and passed and int(collected.group(1)) > 0 and int(passed.group(1)) > 0:
            return {"framework": "playwright", "executed": int(passed.group(1)), "source": "Playwright run and passed summaries"}

    go = _go_json_proof(words, output)
    if go:
        return go

    if words and words[0] == "cargo" and "test" in words:
        matches = re.findall(r"(?mi)^test result:\s+ok\.\s+(\d+)\s+passed;\s+(\d+)\s+failed;", output)
        executed = sum(int(passed) for passed, failed in matches if int(failed) == 0)
        if matches and executed > 0 and all(int(failed) == 0 for _passed, failed in matches):
            return {"framework": "cargo", "executed": executed, "source": "Cargo test result summary"}

    if words and words[0] == "dotnet" and "test" in words:
        match = re.search(r"(?i)passed!\s*-\s*failed:\s*(\d+),\s*passed:\s*(\d+),\s*skipped:\s*\d+,\s*total:\s*(\d+)", output)
        if match and int(match.group(1)) == 0 and int(match.group(2)) > 0 and int(match.group(3)) > 0:
            return {"framework": "dotnet", "executed": int(match.group(3)), "source": ".NET test summary"}
        total = re.search(r"(?mi)^\s*total tests:\s*(\d+)\s*$", output)
        passed = re.search(r"(?mi)^\s*passed:\s*(\d+)\s*$", output)
        failed = re.search(r"(?mi)^\s*failed:\s*(\d+)\s*$", output)
        if total and passed and int(total.group(1)) > 0 and int(passed.group(1)) > 0 and (not failed or int(failed.group(1)) == 0):
            return {"framework": "dotnet", "executed": int(total.group(1)), "source": ".NET VSTest summary"}

    if words and words[0] in {"mvn", "mvnw"} and "test" in words:
        matches = re.findall(r"(?i)tests run:\s*(\d+),\s*failures:\s*(\d+),\s*errors:\s*(\d+),\s*skipped:\s*\d+", output)
        good = [int(total) for total, failures, errors in matches if int(failures) == 0 and int(errors) == 0]
        if matches and sum(good) > 0 and len(good) == len(matches):
            return {"framework": "maven", "executed": sum(good), "source": "Surefire test summary"}

    if words and (words[0].startswith("gradlew") or words[0] == "gradle") and "test" in words:
        match = re.search(r"(?i)(\d+)\s+tests?\s+completed,\s*(\d+)\s+failed", output)
        if match and int(match.group(1)) > 0 and int(match.group(2)) == 0:
            return {"framework": "gradle", "executed": int(match.group(1)), "source": "Gradle test summary"}

    return _known_json_report(words, output)


def analyze_verification(
    commands: list[list[str]],
    results: list[dict[str, Any]],
    *,
    test_indexes: set[int] | None = None,
    evidence_contracts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Require positive, runner-specific proof that at least one test executed."""

    tested = test_indexes if test_indexes is not None else set(range(len(commands)))
    contracts = evidence_contracts or []
    problems: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result.get("output_truncated"):
            problems.append({"index": index, "reason": "command output was truncated; verification evidence is incomplete"})
            continue
        if result.get("timed_out") or int(result.get("exit_code", -1)) != 0:
            problems.append({"index": index, "reason": "command failed or timed out"})
            continue
        if index not in tested:
            continue
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        framework_output = f"{stdout}\n{stderr}".strip()
        proof = _text_proof(commands[index], framework_output) or _explicit_json_contract(
            commands[index], stdout, contracts,
        )
        if proof is None:
            problems.append({
                "index": index,
                "reason": (
                    "test command exited zero without positive framework evidence that one or more tests executed; "
                    "use a supported runner/report or configure project.test_evidence_contracts in trusted settings"
                ),
            })
        else:
            evidence.append({"index": index, **proof})
    return {
        "passed": bool(commands) and not problems,
        "no_commands": not commands,
        "no_test_evidence": any("test command" in item["reason"] for item in problems),
        "verification_problems": problems,
        "verification_evidence": evidence,
    }
