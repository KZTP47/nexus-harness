from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .audit import audit_distribution, audit_installed_distribution
from .benchmark import DEFAULT_SEED, render_markdown, run_benchmark
from .checkpoints import CheckpointManager
from .changes import FileTransaction
from .config import LoadedConfig, load_config, write_default_project_config
from .detect import combined_commands, detect_project
from .doctor import run_doctor
from .graphs import GraphIssue, resolve_graph_execution_policy, resolve_workflow_policy, simulate_graph, validate_graph
from .mcp import MCPClient, configured_server
from .memory import MemoryStore
from .models import HarnessError
from .models import ProviderRequest
from .plugins import load_plugins
from . import qa
from .context import ContextCompiler
from .refinement import RefinementManager
from .server import serve_ui
from .safety import confined_path
from .workflow import HarnessApplication
from .resident import ResidentClient, start_daemon


PROVIDER_DEFAULTS = {
    "ollama": ("qwen2.5-coder:7b", "http://127.0.0.1:11434", ""),
    "openai": ("gpt-5", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("claude-sonnet-4-5", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openai-compatible": ("local-model", "http://127.0.0.1:8000/v1", ""),
    "local": ("local-model", "", ""),
}


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _config(args: argparse.Namespace) -> LoadedConfig:
    start = Path(args.project).resolve() if getattr(args, "project", None) else Path.cwd()
    explicit = Path(args.config).resolve() if getattr(args, "config", None) else None
    overrides: dict[str, Any] = {}
    if getattr(args, "provider", None):
        overrides.setdefault("provider", {})["name"] = args.provider
    if getattr(args, "model", None):
        overrides.setdefault("provider", {})["model"] = args.model
    return load_config(start, explicit, overrides or None)


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.path or ".").resolve()
    if not root.is_dir():
        raise HarnessError(f"Project path is not a directory: {root}")
    detections = detect_project(root)
    print("Detected: " + ", ".join(item.stack for item in detections))
    provider = args.provider or "ollama"
    if not args.yes:
        entered = input(f"Provider [{provider}]: ").strip()
        provider = entered or provider
    if provider not in PROVIDER_DEFAULTS:
        raise HarnessError(f"Unknown provider: {provider}")
    default_model, default_endpoint, key_env = PROVIDER_DEFAULTS[provider]
    model = args.model or default_model
    endpoint = args.endpoint or default_endpoint
    if not args.yes:
        model = input(f"Model [{model}]: ").strip() or model
        endpoint = input(f"Endpoint [{endpoint}]: ").strip() or endpoint
    path = write_default_project_config(
        root,
        provider,
        model,
        endpoint,
        key_env,
        combined_commands(detections, "test"),
        combined_commands(detections, "lint"),
        combined_commands(detections, "build"),
    )
    print(f"Created {path}")
    if key_env:
        print(f"Set {key_env} before running a task. Store it in your shell or secret manager, not in config.json.")
    print("Next: harness doctor")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    result = run_doctor(_config(args))
    if args.json:
        _print_json(result)
    else:
        for check in result["checks"]:
            print(f"{check['level'].upper():4} {check['name']}: {check['message']}")
    return int(result["exit_code"])


def _event_printer(event: dict[str, Any]) -> None:
    payload = event.get("payload", {})
    detail = payload.get("error") or payload.get("summary") or event.get("kind")
    print(f"[{event.get('node')}] {detail}", file=sys.stderr)


def command_run(args: argparse.Namespace) -> int:
    config = _config(args)
    task = " ".join(args.task).strip()
    if args.detach:
        start_daemon(config)
        job = ResidentClient(config.project_root).request(
            "POST", "/v1/jobs", {"task": task, "dry_run": args.dry_run}
        )
        _print_json(job) if args.json else print(f"Queued job: {job['id']}")
        return 0
    with HarnessApplication(config, None if args.json else _event_printer) as app:
        result = app.run_task(task, dry_run=args.dry_run)
    if args.json:
        _print_json(result)
    else:
        print(f"Run {result['state']}: {result['run_id']}")
        if result.get("transactions"):
            print("Transactions: " + ", ".join(result["transactions"]))
    return 0


def command_daemon(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.daemon_command == "start":
        result = start_daemon(config, args.port)
    else:
        client = ResidentClient(config.project_root)
        if args.daemon_command == "status":
            result = client.request("GET", "/v1/health")
        elif args.daemon_command == "stop":
            result = client.request("POST", "/v1/shutdown", {})
        else:
            raise HarnessError("Daemon command is required")
    _print_json(result) if args.json else print(json.dumps(result, sort_keys=True))
    return 0


def command_jobs(args: argparse.Namespace) -> int:
    config = _config(args)
    client = ResidentClient(config.project_root)
    action = args.jobs_command
    if action == "list":
        result = client.request("GET", "/v1/jobs")
    elif action == "status":
        result = client.request("GET", f"/v1/jobs/{args.job_id}")
    elif action == "cancel":
        result = client.request("POST", f"/v1/jobs/{args.job_id}/cancel", {})
    elif action == "resume":
        result = client.request("POST", f"/v1/jobs/{args.job_id}/resume", {})
    elif action == "message":
        result = client.request(
            "POST", f"/v1/jobs/{args.job_id}/messages",
            {"target": args.target, "message": args.message},
        )
    elif action == "receipts":
        result = client.request("GET", f"/v1/jobs/{args.job_id}/messages")
    elif action == "attach":
        cursor = max(0, args.after)
        while True:
            page = client.request("GET", f"/v1/jobs/{args.job_id}/events?after={cursor}")
            for event in page["events"]:
                cursor = int(event["sequence"])
                if args.json:
                    print(json.dumps(event, sort_keys=True, ensure_ascii=False))
                else:
                    print(f"{cursor:>6} {event['kind']}: {json.dumps(event['payload'], ensure_ascii=False)}")
            job = client.request("GET", f"/v1/jobs/{args.job_id}")
            if job["state"] in {"complete", "failed", "cancelled", "resume_ready", "uncertain"}:
                return 0 if job["state"] == "complete" else 1
            if not args.follow:
                return 0
            time.sleep(0.25)
    else:
        raise HarnessError("Jobs command is required")
    _print_json(result)
    return 0


def command_test(args: argparse.Namespace) -> int:
    with HarnessApplication(_config(args)) as app:
        result = app.test(include_lint=args.lint, include_build=args.build)
    if args.json:
        _print_json(result)
    else:
        if result["no_commands"]:
            print("No test command was detected. Set project.test_commands in .harness/config.json.")
        for item in result["results"]:
            status = "PASS" if item["exit_code"] == 0 and not item["timed_out"] else "FAIL"
            print(f"{status} {' '.join(item['argv'])} ({item['duration_ms']} ms)")
            if status == "FAIL":
                print(item["stderr"] or item["stdout"])
    return 0 if result["passed"] else 1


def command_index(args: argparse.Namespace) -> int:
    with HarnessApplication(_config(args)) as app:
        result = app.index()
    _print_json(result) if args.json else print(f"Indexed {result['files']} files; updated {result['updated']}; skipped {result['skipped']}.")
    return 0


def command_memory(args: argparse.Namespace) -> int:
    config = _config(args)
    with MemoryStore(config) as memory:
        if args.memory_command == "search":
            hits = memory.search_episodes(args.query, args.limit) + memory.search_documents(args.query, args.limit)
            _print_json([{"source": hit.source, "key": hit.key, "score": hit.score, "text": hit.text[:4000], "metadata": hit.metadata} for hit in hits])
        elif args.memory_command == "add":
            if not memory.enabled:
                raise HarnessError("Memory is disabled; enable memory before adding a retained episode")
            key = memory.add_episode(args.namespace, args.title, args.body, {"manual": True}, trust=args.trust)
            print(key)
        elif args.memory_command in {"status", "curate"}:
            _print_json(memory.stats(int(config.get("memory.retention_days"))))
        else:
            raise HarnessError("Memory command is required")
    return 0


def _verification_records(config: LoadedConfig, args: argparse.Namespace) -> list[dict[str, Any]]:
    raw = getattr(args, "verification_json", None)
    filename = getattr(args, "verification_file", None)
    if filename:
        path = confined_path(config.project_root, filename, allow_missing=False)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HarnessError(f"Cannot read verification file: {exc}") from exc
    if not raw:
        raise HarnessError("Verification records are required")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Verification records are not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise HarnessError("Verification records must be a JSON array")
    return value


def command_refine(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.get("memory.enabled"):
        raise HarnessError("Refinement is unavailable while memory is disabled because reviewed versions cannot be retained")
    with MemoryStore(config) as memory:
        manager = RefinementManager(memory)
        if args.refine_command == "list":
            print(manager.overview() or "No active supplemental state.")
        elif args.refine_command == "candidates":
            _print_json(manager.candidates(None if args.all else args.status))
        elif args.refine_command == "review":
            _print_json(
                manager.review_candidate(
                    args.candidate_id,
                    _verification_records(config, args),
                    args.verdict,
                    args.reason,
                )
            )
        elif args.refine_command == "promote":
            print(manager.promote_candidate(args.candidate_id))
        elif args.refine_command == "reject":
            _print_json(manager.reject_candidate(args.candidate_id, args.reason))
        elif args.refine_command == "rollback":
            print(
                manager.rollback(
                    args.kind,
                    args.name,
                    args.version,
                    _verification_records(config, args),
                    args.verdict,
                )
            )
        else:
            raise HarnessError("Refine command is required")
    return 0


def command_mcp(args: argparse.Namespace) -> int:
    config = _config(args)
    server = configured_server(config, args.server)
    with MCPClient(
        server,
        timeout=int(config.get("mcp.timeout_seconds")),
        max_response_bytes=int(config.get("mcp.max_response_bytes")),
    ) as client:
        if args.mcp_command == "list":
            _print_json(client.list_tools())
        elif args.mcp_command == "call":
            try:
                arguments = json.loads(args.arguments)
            except json.JSONDecodeError as exc:
                raise HarnessError(f"MCP arguments are not valid JSON: {exc}") from exc
            if not isinstance(arguments, dict):
                raise HarnessError("MCP arguments must be a JSON object")
            _print_json(client.call_tool(args.tool, arguments))
        else:
            raise HarnessError("MCP command is required")
    return 0


def command_graph(args: argparse.Namespace) -> int:
    try:
        graph = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Cannot read graph: {exc}") from exc
    if args.graph_command == "validate":
        issues = validate_graph(graph)
        if not issues:
            config = _config(args)
            policy = resolve_workflow_policy(config, load_plugins(config).workflow_nodes)
            try:
                resolve_graph_execution_policy(config, graph, policy)
            except HarnessError as exc:
                issues.append(GraphIssue("execution", str(exc)))
        _print_json({"valid": not issues, "issues": [issue.__dict__ for issue in issues]})
        return 0 if not issues else 1
    result = simulate_graph(graph, {"test_failures_remaining": args.failures, "temperature": 0.2})
    _print_json(result)
    return 0 if result["complete"] else 1


def command_audit(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[2]
    result = audit_distribution(root) if (root / "src" / "our_harness").is_dir() else audit_installed_distribution()
    _print_json(result)
    return 0 if result["passed"] else 1


def command_benchmark(args: argparse.Namespace) -> int:
    result = run_benchmark(args.seed, args.provider_profile, repetitions=args.repetitions)
    rendered = (
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.format == "json"
        else render_markdown(result)
    )
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(output)
    else:
        print(rendered, end="")
    return 0 if result["case_summary"]["failed"] == 0 else 1


def command_config_show(args: argparse.Namespace) -> int:
    config = _config(args)
    redacted = json.loads(json.dumps(config.data))
    _print_json({"project_root": str(config.project_root), "config": redacted, "provenance": config.provenance})
    return 0


def command_checkpoint(args: argparse.Namespace) -> int:
    manager = CheckpointManager(_config(args))
    if args.checkpoint_command == "create":
        _print_json(manager.create(args.note))
    elif args.checkpoint_command == "list":
        _print_json(manager.list())
    elif args.checkpoint_command == "restore":
        _print_json(manager.restore_file(args.checkpoint_id, args.path))
    return 0


def command_recovery(args: argparse.Namespace) -> int:
    config = _config(args)
    transactions = FileTransaction(
        config.project_root,
        int(config.get("execution.max_changed_files")),
        int(config.get("execution.max_changed_bytes")),
    )
    if args.recovery_command == "list":
        _print_json(transactions.reconcile())
    else:
        _print_json(transactions.recover(args.transaction_id, args.recovery_command))
    return 0


def _run_decision(args: argparse.Namespace) -> dict[str, Any]:
    try:
        decision = json.loads(args.decision_json)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Run decision is not valid JSON: {exc}") from exc
    if not isinstance(decision, dict):
        raise HarnessError("Run decision must be a JSON object")
    return decision


def _checkpoint_json(checkpoint: Any) -> dict[str, Any]:
    return {
        **checkpoint.payload(),
        "version": checkpoint.version,
        "updated_at_ms": checkpoint.updated_at_ms,
    }


def command_runs(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.runs_command in {"list", "show"}:
        with MemoryStore(config) as memory:
            if args.runs_command == "show":
                checkpoint = memory.load_run_checkpoint(args.run_id)
                if checkpoint is None:
                    raise HarnessError(f"Run has no resumable checkpoint: {args.run_id}")
                _print_json(_checkpoint_json(checkpoint))
            else:
                _print_json(
                    [
                        {
                            "run_id": item.run_id,
                            "task": item.task,
                            "current_node": item.current_node,
                            "remaining_deadline_seconds": item.remaining_deadline_seconds,
                            "pending_approval": item.pending_approval,
                            "sequence": item.sequence,
                            "version": item.version,
                            "updated_at_ms": item.updated_at_ms,
                        }
                        for item in memory.list_run_checkpoints()
                    ]
                )
        return 0
    with HarnessApplication(config, None if getattr(args, "json", False) else _event_printer) as app:
        if args.runs_command == "resume":
            result = app.resume_task(args.run_id)
            if args.json:
                _print_json(result)
            else:
                print(f"Run {result['state']}: {result['run_id']}")
            return 0
        if args.runs_command == "cancel":
            _print_json(app.cancel_run(args.run_id, _run_decision(args)))
            return 0
        if args.runs_command in {"approve", "reject"}:
            checkpoint = app.decide_run_approval(
                args.run_id,
                args.runs_command == "approve",
                _run_decision(args),
            )
            _print_json(_checkpoint_json(checkpoint))
            return 0
    raise HarnessError("Runs command is required")


def command_ask(args: argparse.Namespace) -> int:
    config = _config(args)
    with HarnessApplication(config) as app:
        app.index()
        compiled = ContextCompiler(config, app.memory).compile(args.question, [item.to_dict() for item in detect_project(config.project_root)])
        request_value = ProviderRequest(
            compiled.prefix,
            compiled.dynamic,
            [{"role": "user", "content": "Answer the project question. Cite each factual claim with the supplied [document:path] or [episode:id] label. If the evidence is absent, say so.\n\nQUESTION\n" + args.question}],
            str(config.get("provider.model")),
            0.0,
            int(config.get("provider.max_output_tokens")),
        )
        response = app.provider.complete(request_value)
    print(response.text)
    return 0


def _qa_write_report(config: LoadedConfig, result: qa.QaRunResult, args: argparse.Namespace) -> None:
    destination = getattr(args, "output", None)
    if not destination:
        return
    if re.split(r"[\\/]", str(destination))[0].lower() == ".git":
        raise HarnessError("A report may not be written inside the .git folder")
    path = confined_path(config.project_root, destination, allow_missing=True, allow_control=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(qa.render_report(result, args.format), encoding="utf-8")
    print(f"Report written to {path}")


def command_qa(args: argparse.Namespace) -> int:
    config = _config(args)
    command = args.qa_command
    if command == "init":
        detections = detect_project(config.project_root)
        combined = {
            kind: list(config.get(f"project.{kind}_commands") or []) or combined_commands(detections, kind)
            for kind in ("test", "lint", "build")
        }
        suite = qa.starter_suite(combined["test"], combined["lint"], combined["build"])
        path = qa.suite_path(config)
        if path.exists() and not args.force:
            raise HarnessError(f"A suite already exists at {path}. Use --force to replace it.")
        written = qa.write_suite(config, suite)
        print(f"Wrote {len(suite.cases)} starter case(s) to {written}")
        print("Edit that file, then run: harness qa run")
        return 0

    if command == "list":
        suite = qa.load_suite(config, getattr(args, "suite", None))
        if args.json:
            _print_json(suite.to_dict())
            return 0
        print(f"Suite {suite.name} holds {len(suite.cases)} case(s).")
        for case in suite.cases:
            tags = f" [{', '.join(case.tags)}]" if case.tags else ""
            print(f"  {case.id} ({case.kind}){tags}: {case.title}")
        return 0

    if command == "run":
        suite = qa.load_suite(config, getattr(args, "suite", None))
        runner = qa.QaRunner(config)
        result = runner.run(
            suite,
            tags=args.tag or (),
            ids=args.case or (),
            workers=args.workers,
            write_artifacts=not args.no_artifacts,
        )
        if not args.no_artifacts:
            qa.record_history(config, result)
        _qa_write_report(config, result, args)
        if args.format == "json" and not args.output:
            _print_json(result.to_dict())
        elif not args.output:
            print(qa.render_report(result, args.format))
        return 0 if result.passed else 1

    if command == "flaky":
        report = qa.flaky_report(config)
        if args.json:
            _print_json(report)
            return 0
        if not report:
            print("No case looks unstable yet. Run the suite a few more times to build a history.")
            return 0
        print("Cases whose result keeps changing:")
        for entry in report:
            print(
                f"  {entry['id']}: failed {entry['failures']} of {entry['runs']} runs "
                f"(instability {entry['instability']}). {entry['why']}."
            )
        return 0

    if command == "generate":
        suite: qa.QaSuite | None
        try:
            suite = qa.load_suite(config, getattr(args, "suite", None))
        except HarnessError:
            suite = None
        detections = [item.to_dict() for item in detect_project(config.project_root)]
        prompt = qa.generation_prompt(suite, detections, args.focus or "", args.limit)
        with HarnessApplication(config) as app:
            response = app.provider.complete(
                ProviderRequest(
                    qa.GENERATION_INSTRUCTIONS,
                    prompt,
                    [{"role": "user", "content": prompt}],
                    str(config.get("provider.model")),
                    0.0,
                    int(config.get("provider.max_output_tokens")),
                )
            )
        existing = [case.id for case in suite.cases] if suite else []
        candidates = qa.parse_generated_cases(response.text, existing)
        path = qa.save_candidates(config, candidates, source=str(config.get("provider.model")))
        print(f"Saved {len(candidates)} proposed case(s) to {path}")
        for item in candidates:
            case = item["case"]
            print(f"  {case['id']} ({case['kind']}): {case['title']}")
            for warning in item["warnings"]:
                print(f"    warning: {warning}")
        print("Nothing runs until you accept it: harness qa accept <id>")
        return 0

    if command == "candidates":
        candidates = qa.load_candidates(config)
        if args.json:
            _print_json(candidates)
            return 0
        if not candidates:
            print("There are no proposed cases. Run 'harness qa generate' to ask the model for some.")
            return 0
        for item in candidates:
            case = item.get("case", {})
            print(f"  {case.get('id')} ({case.get('kind')}): {case.get('title')}")
            for warning in item.get("warnings", []):
                print(f"    warning: {warning}")
        return 0

    if command == "accept":
        suite, accepted = qa.accept_candidates(config, args.case_id, suite_override=getattr(args, "suite", None))
        print(f"Added {len(accepted)} case(s). The suite now holds {len(suite.cases)}.")
        return 0

    if command == "reject":
        rejected = qa.reject_candidates(config, args.case_id)
        print(f"Removed {len(rejected)} proposed case(s).")
        return 0

    raise HarnessError("QA command is required")


def command_ui(args: argparse.Namespace) -> int:
    config = _config(args)
    overrides: dict[str, Any] = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.host is not None:
        overrides["host"] = args.host
    if args.open_browser is not None:
        overrides["open_browser"] = args.open_browser
    serve_ui(config, **overrides)
    return 0


def command_brief(args: argparse.Namespace) -> int:
    config = _config(args)
    doctor = run_doctor(config)
    detections = detect_project(config.project_root)
    with MemoryStore(config) as memory:
        runs = [dict(row) for row in memory.connection.execute("SELECT id,task,state,updated_at FROM runs ORDER BY updated_at DESC LIMIT 5")]
    _print_json({
        "project": str(config.project_root),
        "doctor": {"exit_code": doctor["exit_code"], "checks": doctor["checks"]},
        "detections": [item.to_dict() for item in detections],
        "recent_runs": runs,
        "standards_read_order": config.get("project.standards_files"),
    })
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="harness", description="Local programming-agent harness")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--project", help="Project root or a path inside it")
    root.add_argument("--config", help="Extra config file loaded after project config")
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Scan a project and create .harness/config.json")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--provider", choices=sorted(PROVIDER_DEFAULTS), default="ollama")
    init.add_argument("--model")
    init.add_argument("--endpoint")
    init.add_argument("--yes", action="store_true", help="Accept defaults without questions")
    init.set_defaults(handler=command_init)

    run = sub.add_parser("run", help="Plan, edit, test, review, and repair a coding task")
    run.add_argument("task", nargs="+", help="Task text")
    run.add_argument("--provider", choices=sorted(PROVIDER_DEFAULTS))
    run.add_argument("--model")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--detach", action="store_true", help="Queue the task in the workspace resident daemon")
    run.add_argument("--json", action="store_true")
    run.set_defaults(handler=command_run)

    test = sub.add_parser("test", help="Run detected or configured project checks")
    test.add_argument("--lint", action="store_true")
    test.add_argument("--build", action="store_true")
    test.add_argument("--json", action="store_true")
    test.set_defaults(handler=command_test)

    doctor = sub.add_parser("doctor", help="Check config, provider, tools, and local prerequisites")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    index = sub.add_parser("index", help="Update the workspace text and dependency index")
    index.add_argument("--json", action="store_true")
    index.set_defaults(handler=command_index)

    ui = sub.add_parser("ui", help="Open the local control panel")
    ui.add_argument("--port", type=int, help="Port to listen on; 0 asks the system for a free one")
    ui.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), help="Loopback address to bind")
    ui.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=None, help="Open a browser window on start")
    ui.set_defaults(handler=command_ui)

    memory = sub.add_parser("memory", help="Search or add local memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    search = memory_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.set_defaults(handler=command_memory)
    add = memory_sub.add_parser("add")
    add.add_argument("title")
    add.add_argument("body")
    add.add_argument("--namespace", default="manual")
    add.add_argument("--trust", type=float, default=0.7)
    add.set_defaults(handler=command_memory)
    status = memory_sub.add_parser("status")
    status.set_defaults(handler=command_memory)
    curate = memory_sub.add_parser("curate", help="Print a read-only retention plan")
    curate.set_defaults(handler=command_memory)

    refine = sub.add_parser("refine", help="Review and manage supplemental state")
    refine_sub = refine.add_subparsers(dest="refine_command", required=True)
    refine_list = refine_sub.add_parser("list")
    refine_list.set_defaults(handler=command_refine)
    refine_candidates = refine_sub.add_parser("candidates")
    refine_candidates.add_argument("--status", default="pending", choices=["pending", "reviewed", "promoted", "rejected"])
    refine_candidates.add_argument("--all", action="store_true")
    refine_candidates.set_defaults(handler=command_refine)
    review = refine_sub.add_parser("review")
    review.add_argument("candidate_id")
    review.add_argument("--verdict", required=True, choices=["PASS", "BLOCK"])
    review.add_argument("--reason", required=True)
    review_verification = review.add_mutually_exclusive_group(required=True)
    review_verification.add_argument("--verification-json")
    review_verification.add_argument("--verification-file", help="Project-relative JSON file")
    review.set_defaults(handler=command_refine)
    promote = refine_sub.add_parser("promote")
    promote.add_argument("candidate_id")
    promote.set_defaults(handler=command_refine)
    reject = refine_sub.add_parser("reject")
    reject.add_argument("candidate_id")
    reject.add_argument("reason")
    reject.set_defaults(handler=command_refine)
    rollback = refine_sub.add_parser("rollback")
    rollback.add_argument("kind", choices=["prompt", "memory", "skill", "subagent"])
    rollback.add_argument("name")
    rollback.add_argument("version")
    rollback.add_argument("--verdict", required=True, choices=["PASS", "BLOCK"])
    rollback_verification = rollback.add_mutually_exclusive_group(required=True)
    rollback_verification.add_argument("--verification-json")
    rollback_verification.add_argument("--verification-file", help="Project-relative JSON file")
    rollback.set_defaults(handler=command_refine)

    mcp = sub.add_parser("mcp", help="List or call configured MCP tools")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_list = mcp_sub.add_parser("list")
    mcp_list.add_argument("server")
    mcp_list.set_defaults(handler=command_mcp)
    mcp_call = mcp_sub.add_parser("call")
    mcp_call.add_argument("server")
    mcp_call.add_argument("tool")
    mcp_call.add_argument("--arguments", default="{}", help="JSON object")
    mcp_call.set_defaults(handler=command_mcp)

    graph_parser = sub.add_parser("graph", help="Validate or simulate a workflow graph")
    graph_sub = graph_parser.add_subparsers(dest="graph_command", required=True)
    graph_validate = graph_sub.add_parser("validate")
    graph_validate.add_argument("file")
    graph_validate.set_defaults(handler=command_graph)
    graph_simulate = graph_sub.add_parser("simulate")
    graph_simulate.add_argument("file")
    graph_simulate.add_argument("--failures", type=int, default=1)
    graph_simulate.set_defaults(handler=command_graph)

    audit = sub.add_parser("audit", help="Check the installed source for fixed paths and project bindings")
    audit.set_defaults(handler=command_audit)
    benchmark = sub.add_parser("benchmark", help="Run deterministic checks and optional provider-backed repair tasks")
    benchmark.add_argument("--seed", type=int, default=DEFAULT_SEED)
    benchmark.add_argument("--repetitions", type=int, choices=range(1, 11), default=1, metavar="1..10")
    benchmark.add_argument("--format", choices=["json", "markdown"], default="json")
    benchmark.add_argument("--output", help="Write the selected format to this file")
    benchmark.add_argument("--provider-profile", help="Run isolated agentic repair cases with this trusted local profile")
    benchmark.set_defaults(handler=command_benchmark)
    show = sub.add_parser("config", help="Print effective config and setting sources")
    show.set_defaults(handler=command_config_show)
    checkpoint = sub.add_parser("checkpoint", help="Create, list, or restore project safety snapshots")
    checkpoint_sub = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_create = checkpoint_sub.add_parser("create")
    checkpoint_create.add_argument("--note", default="")
    checkpoint_create.set_defaults(handler=command_checkpoint)
    checkpoint_list = checkpoint_sub.add_parser("list")
    checkpoint_list.set_defaults(handler=command_checkpoint)
    checkpoint_restore = checkpoint_sub.add_parser("restore")
    checkpoint_restore.add_argument("checkpoint_id")
    checkpoint_restore.add_argument("path")
    checkpoint_restore.set_defaults(handler=command_checkpoint)
    recovery = sub.add_parser("recovery", help="Inspect or resolve interrupted file transactions")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_list = recovery_sub.add_parser("list")
    recovery_list.set_defaults(handler=command_recovery)
    for action in ("rollback", "finalize"):
        recovery_action = recovery_sub.add_parser(action)
        recovery_action.add_argument("transaction_id")
        recovery_action.set_defaults(handler=command_recovery)
    runs = sub.add_parser("runs", help="Inspect, decide, resume, or cancel durable runs")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_sub.add_parser("list")
    runs_list.set_defaults(handler=command_runs)
    runs_show = runs_sub.add_parser("show")
    runs_show.add_argument("run_id")
    runs_show.set_defaults(handler=command_runs)
    runs_resume = runs_sub.add_parser("resume")
    runs_resume.add_argument("run_id")
    runs_resume.add_argument("--json", action="store_true")
    runs_resume.set_defaults(handler=command_runs)
    for action in ("cancel", "approve", "reject"):
        runs_action = runs_sub.add_parser(action)
        runs_action.add_argument("run_id")
        runs_action.add_argument(
            "--decision-json",
            "--decision",
            dest="decision_json",
            required=True,
            help="Explicit JSON object recording the decision",
        )
        runs_action.set_defaults(handler=command_runs)
    daemon = sub.add_parser("daemon", help="Manage the workspace resident process")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_start = daemon_sub.add_parser("start")
    daemon_start.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an available port")
    daemon_start.add_argument("--json", action="store_true")
    daemon_start.set_defaults(handler=command_daemon)
    daemon_status = daemon_sub.add_parser("status")
    daemon_status.add_argument("--json", action="store_true")
    daemon_status.set_defaults(handler=command_daemon)
    daemon_stop = daemon_sub.add_parser("stop")
    daemon_stop.add_argument("--json", action="store_true")
    daemon_stop.set_defaults(handler=command_daemon)
    jobs = sub.add_parser("jobs", help="Inspect and control resident jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_list = jobs_sub.add_parser("list")
    jobs_list.set_defaults(handler=command_jobs)
    jobs_status = jobs_sub.add_parser("status")
    jobs_status.add_argument("job_id")
    jobs_status.set_defaults(handler=command_jobs)
    jobs_attach = jobs_sub.add_parser("attach")
    jobs_attach.add_argument("job_id")
    jobs_attach.add_argument("--after", type=int, default=0)
    jobs_attach.add_argument("--follow", action=argparse.BooleanOptionalAction, default=True)
    jobs_attach.add_argument("--json", action="store_true")
    jobs_attach.set_defaults(handler=command_jobs)
    for resident_action in ("cancel", "resume"):
        jobs_action = jobs_sub.add_parser(resident_action)
        jobs_action.add_argument("job_id")
        jobs_action.set_defaults(handler=command_jobs)
    jobs_message = jobs_sub.add_parser("message")
    jobs_message.add_argument("job_id")
    jobs_message.add_argument("target")
    jobs_message.add_argument("message")
    jobs_message.set_defaults(handler=command_jobs)
    jobs_receipts = jobs_sub.add_parser("receipts")
    jobs_receipts.add_argument("job_id")
    jobs_receipts.set_defaults(handler=command_jobs)
    qa_parser = sub.add_parser("qa", help="Write, run, and report plain-language project checks")
    qa_sub = qa_parser.add_subparsers(dest="qa_command", required=True)
    qa_init = qa_sub.add_parser("init", help="Write a starter suite from the detected project commands")
    qa_init.add_argument("--force", action="store_true", help="Replace an existing suite")
    qa_init.set_defaults(handler=command_qa)
    qa_list = qa_sub.add_parser("list", help="Show every case in the suite")
    qa_list.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_list.add_argument("--json", action="store_true")
    qa_list.set_defaults(handler=command_qa)
    qa_run = qa_sub.add_parser("run", help="Run the suite and report what happened")
    qa_run.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_run.add_argument("--tag", action="append", help="Only run cases with this tag; may repeat")
    qa_run.add_argument("--case", action="append", help="Only run this case id; may repeat")
    qa_run.add_argument("--workers", type=int, help="How many cases to run at the same time")
    qa_run.add_argument("--format", choices=qa.REPORT_FORMATS, default="markdown")
    qa_run.add_argument("--output", help="Write the report to this project-relative file")
    qa_run.add_argument("--no-artifacts", action="store_true", help="Do not keep evidence files")
    qa_run.set_defaults(handler=command_qa)
    qa_flaky = qa_sub.add_parser("flaky", help="Name the cases whose result keeps changing")
    qa_flaky.add_argument("--json", action="store_true")
    qa_flaky.set_defaults(handler=command_qa)
    qa_generate = qa_sub.add_parser("generate", help="Ask the model to propose new cases for review")
    qa_generate.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_generate.add_argument("--focus", help="What the new cases should cover")
    qa_generate.add_argument("--limit", type=int, default=8)
    qa_generate.set_defaults(handler=command_qa)
    qa_candidates = qa_sub.add_parser("candidates", help="Show proposed cases waiting for a decision")
    qa_candidates.add_argument("--json", action="store_true")
    qa_candidates.set_defaults(handler=command_qa)
    qa_accept = qa_sub.add_parser("accept", help="Move proposed cases into the suite")
    qa_accept.add_argument("case_id", nargs="+")
    qa_accept.add_argument("--suite", help="Suite file to write instead of the configured one")
    qa_accept.set_defaults(handler=command_qa)
    qa_reject = qa_sub.add_parser("reject", help="Throw away proposed cases")
    qa_reject.add_argument("case_id", nargs="+")
    qa_reject.set_defaults(handler=command_qa)

    ask = sub.add_parser("ask", help="Answer a project question from indexed evidence")
    ask.add_argument("question")
    ask.set_defaults(handler=command_ask)
    brief = sub.add_parser("brief", help="Print project health, stack, recent runs, and standards order")
    brief.set_defaults(handler=command_brief)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.handler(args))
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
