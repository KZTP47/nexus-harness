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
from .config import (
    LoadedConfig,
    is_project_local_config_trusted,
    load_config,
    trust_project_local_config,
    write_default_project_config,
)
from .detect import combined_commands, detect_project
from .doctor import run_doctor
from .graphs import GraphIssue, resolve_graph_execution_policy, resolve_workflow_policy, simulate_graph, validate_graph
from .mcp import MCPClient, configured_server
from .memory import MemoryStore
from .models import HarnessError
from .models import ProviderRequest
from .plugins import check_kinds, load_plugins
from . import bundle
from . import comparison
from . import coverage
from . import datasets
from . import handover
from . import qa
from . import recorder
from . import seats as seat_setup
from . import selectors
from . import share
from . import starters
from .context import ContextCompiler
from .refinement import RefinementManager
from .server import serve_ui
from .redaction import CredentialRedactor
from .safety import confined_path
from .watcher import Changes, ProjectWatcher
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


def _ask(question: str, default: str = "") -> str:
    """Ask a question, and cope with nobody being there to answer.

    A command run by a script or a build server has no one at the keyboard.
    Waiting for ever, or falling over with a raw error, would both be wrong;
    the written-down answer is used instead.
    """

    try:
        return input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def command_init(args: argparse.Namespace) -> int:
    # A folder named after init wins, then --project, then where you are
    # standing. Without the middle one, "harness --project X init" would quietly
    # set up the wrong folder.
    root = Path(args.path or getattr(args, "project", None) or ".").resolve()
    if not root.is_dir():
        raise HarnessError(f"Project path is not a directory: {root}")
    detections = detect_project(root)
    print("Detected: " + ", ".join(item.stack for item in detections))
    provider = args.provider or "ollama"
    if not args.yes:
        entered = _ask(f"Provider [{provider}]: ")
        provider = entered or provider
    if provider not in PROVIDER_DEFAULTS:
        raise HarnessError(f"Unknown provider: {provider}")
    default_model, default_endpoint, key_env = PROVIDER_DEFAULTS[provider]
    model = args.model or default_model
    endpoint = args.endpoint or default_endpoint
    if not args.yes:
        model = _ask(f"Model [{model}]: ") or model
        endpoint = _ask(f"Endpoint [{endpoint}]: ") or endpoint
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
    # A plain name means a file in the project being worked on, the same as
    # every other command. A full path is taken as it is written.
    wanted = Path(args.file)
    if not wanted.is_absolute():
        wanted = (_config(args).project_root / wanted).resolve()
    try:
        graph = json.loads(wanted.read_text(encoding="utf-8"))
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


def command_bundle(args: argparse.Namespace) -> int:
    config = _config(args)
    if getattr(args, "read", ""):
        for name in ("part", "runs", "output"):
            given = getattr(args, name, None)
            if given not in (None, "", bundle.DEFAULT_RUNS):
                raise HarnessError(
                    f"--{name} makes a bundle, and --read only looks inside one. Use one or the other."
                )
        # A plain name means a file in the project being worked on, the same as
        # everywhere else. A full path is taken as it is written.
        wanted = Path(args.read)
        found = bundle.read_manifest(
            wanted if wanted.is_absolute() else (config.project_root / wanted).resolve()
        )
        if args.json:
            _print_json(found)
            return 0
        print(f"Made by {found.get('made_by', 'an unknown version')} on {found.get('made_at', 'an unknown day')}")
        print(f"It holds: {', '.join(found.get('parts', []))}")
        print(f"{_count(len(found.get('files', [])), 'file')} inside.")
        for line in found.get("left_out", []):
            print(f"  Left out: {line}")
        return 0
    result = bundle.build(
        config,
        parts=args.part or (),
        runs=args.runs,
        output=args.output or "",
        replace=bool(getattr(args, "replace", False)),
    )
    if args.json:
        _print_json(result.to_dict())
        return 0
    for line in bundle.describe(result):
        print(line)
    return 0


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


def command_seats(args: argparse.Namespace) -> int:
    """Find the assistants on this machine, and set them up."""

    root = Path(args.project).resolve() if getattr(args, "project", None) else Path.cwd()
    config, trouble = seat_setup.settings_to_work_from(root)
    if trouble:
        print(trouble)
        print("")
    found = seat_setup.look(config)
    if args.seats_command == "list":
        if args.json:
            _print_json(found.to_dict())
            return 0
        for line in seat_setup.summary(found):
            print(line)
        if found.ready:
            print("")
            print("Set them all up with: harness seats setup")
        return 0 if found.ready else 1

    wanted = [seat.kind for seat in found.ready]
    if args.only:
        asked = {str(item) for item in args.only}
        wanted = [kind for kind in wanted if kind in asked]
    if not wanted:
        raise HarnessError(
            "No assistant is ready on this machine. Run 'harness seats list' to see why."
        )
    done = seat_setup.set_up(config, wanted, trust=not args.no_trust)
    if args.json:
        _print_json(done.to_dict())
        return 0
    print(f"Wrote {done.settings_file}")
    print(f"Routes: {', '.join(done.routes)}")
    if done.kept:
        print(f"Your other settings were kept: {', '.join(done.kept)}")
    if done.replaced:
        print(f"Written over: {', '.join(done.replaced)}")
    print("Trusted." if done.trusted else done.note)
    if done.needs_your_say:
        # Written, shown, and left for a person to decide. The same choice the
        # panel offers, in a terminal.
        for line in done.risky_parts:
            print(f"  - {line}")
        print("")
        print("Read it, then trust it with: harness trust")
    print("")
    print("Give each agent one of these in the Workflow tab, or run: harness ui")
    return 0


def command_start(args: argparse.Namespace) -> int:
    """Set this project up and open the panel, in one go.

    Four commands got somebody from nothing to a working panel: init, doctor,
    qa init, ui. Each one is worth having on its own and none of them is worth
    knowing about on your first afternoon. This does all four, says plainly
    what it did and what is left, and then opens the panel.

    Nothing here is new: every step is a command that already existed, run in
    the order somebody would have run them.
    """

    root = Path(args.path or getattr(args, "project", None) or ".").resolve()
    if not root.is_dir():
        raise HarnessError(f"Project path is not a directory: {root}")
    print(f"Setting up {root.name}.")
    print("")

    # 1. Settings, if there are none yet.
    settings_file = root / ".harness" / "config.json"
    if settings_file.is_file():
        print(f"  Already set up: {settings_file.name} is there.")
    else:
        made = argparse.Namespace(
            path=str(root), project=str(root), config=None, provider="ollama",
            model=None, endpoint=None, api_key_env=None, yes=True, force=False,
        )
        try:
            command_init(made)
        except HarnessError as exc:
            # A project part way through being set up - a settings file of your
            # own but no shared one - is an ordinary state to be in, and this
            # is the command somebody runs to get out of it. Stopping here
            # would leave them exactly where they started.
            print(f"  Could not write the shared settings: {exc}")
            print("  Carrying on with what is here.")
        print("")

    config = _config(argparse.Namespace(project=str(root), config=None))

    # 2. Checks, if there are none yet, written from the commands this project
    #    already uses.
    suite_file = qa.suite_path(config)
    if suite_file.is_file():
        print(f"  Already written: {suite_file.name} holds your checks.")
    else:
        detections = detect_project(config.project_root)
        commands = {
            kind: list(config.get(f"project.{kind}_commands") or [])
            or combined_commands(detections, kind)
            for kind in ("test", "lint", "build")
        }
        suite = qa.starter_suite(commands["test"], commands["lint"], commands["build"])
        written = qa.write_suite(config, suite)
        print(f"  Wrote {_count(len(suite.cases), 'starter check')} to {written.name}.")

    # 3. What is missing, said plainly rather than left to be discovered.
    print("")
    report = run_doctor(config)
    for check in report["checks"]:
        mark = {"ok": "OK  ", "warn": "note", "error": "todo"}.get(str(check.get("level")), "    ")
        print(f"  {mark} {check.get('name')}: {check.get('message')}")
    left = [check for check in report["checks"] if str(check.get("level")) == "error"]
    print("")
    if left:
        print(f"{len(left)} thing(s) still to do. The panel will walk you through them.")
    else:
        print("Everything is ready.")

    if args.no_panel:
        print("")
        print("Open the panel yourself with: harness ui")
        return 0
    print("")
    print("Opening the panel. Press Control and C here to stop it.")
    return command_ui(argparse.Namespace(
        project=str(root), config=None, port=args.port, host="127.0.0.1",
        open_browser=not args.no_open_browser,
    ))


def command_carry(args: argparse.Namespace) -> int:
    """Pack this setup up to carry to another machine, or unpack one here."""

    from . import carry

    config = _config(args)
    if args.carry_command == "pack":
        packed = carry.write_to(config, args.output)
        print(f"Wrote {packed.file}")
        for line in packed.holds:
            print(f"  it holds {line}")
        for line in packed.left_out:
            print(f"  left out: {line}")
        print("")
        print("Copy that file to the other machine and run: harness carry unpack <file>")
        return 0

    where = confined_path(config.project_root, args.file, allow_missing=False)
    try:
        carried = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"{where.name} cannot be read: {exc}") from exc
    done = carry.unpack(config, carried, over_the_top=args.over_the_top)
    for line in done.written:
        print(f"  wrote {line}")
    for line in done.left_alone:
        print(f"  left alone: {line}")
    print("")
    print(done.note)
    return 0


def command_trust(args: argparse.Namespace) -> int:
    """Say that the local config file in this project is yours and may be used."""

    root = Path(args.project).resolve() if getattr(args, "project", None) else Path.cwd()
    local = root / ".harness" / "config.local.json"
    if not local.is_file():
        raise HarnessError(
            f"There is no {local}. Run 'harness init' first, or write the file yourself."
        )
    if args.show:
        trusted = is_project_local_config_trusted(root, local)
        print(f"{local}")
        print("This file is trusted." if trusted else "This file is not trusted yet.")
        return 0 if trusted else 1
    print(local.read_text(encoding="utf-8"))
    if not args.yes:
        answer = _ask("Trust this file and let it set provider routes and commands? [y/N] ", "n")
        if answer.lower() not in ("y", "yes"):
            print("Left as it was.")
            return 1
    store = trust_project_local_config(root, local)
    print(f"Trusted. Recorded in {store}")
    print("Edit the file again and this goes back to untrusted, on purpose.")
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


def _count(number: int, singular: str, plural: str = "") -> str:
    """Say "1 check" or "3 checks" rather than "1 check(s)"."""

    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _which_part(said: str) -> tuple[int, int]:
    """Read "2/4" as: the second part of four.

    Splitting a suite across machines is how a long run is made short. Getting
    the split wrong quietly runs a quarter of the checks and reports a pass, so
    anything that is not two plain numbers is refused here rather than guessed
    at.
    """

    said = str(said or "").strip()
    if not said:
        return (0, 0)
    parts = said.replace(" of ", "/").replace("-", "/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[0-9]+", item.strip()) for item in parts):
        raise HarnessError(
            "Write which part to run as two numbers, like --part 2/4 for the "
            "second part of four."
        )
    number, of = int(parts[0]), int(parts[1])
    if of < 1 or of > 100:
        raise HarnessError("Split the suite into between 1 and 100 parts.")
    if not 1 <= number <= of:
        raise HarnessError(f"There is no part {number} of {of}. Number the parts from 1 up to {of}.")
    return (number, of)


def _qa_write_report(config: LoadedConfig, result: qa.QaRunResult, args: argparse.Namespace) -> None:
    destination = getattr(args, "output", None)
    if not destination:
        return
    if re.split(r"[\\/]", str(destination))[0].lower() == ".git":
        raise HarnessError("A report may not be written inside the .git folder")
    path = confined_path(config.project_root, destination, allow_missing=True, allow_control=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        qa.render_report(result, args.format, CredentialRedactor(config)), encoding="utf-8"
    )
    print(f"Report written to {path}")


def command_qa(args: argparse.Namespace) -> int:
    config = _config(args)
    command = args.qa_command
    kinds = check_kinds(config)
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
        print(f"Wrote {_count(len(suite.cases), 'starter check')} to {written}")
        print("Edit that file, then run: harness qa run")
        return 0

    if command == "list":
        suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
        if args.json:
            _print_json(suite.to_dict())
            return 0
        print(f"Suite {suite.name} holds {_count(len(suite.cases), 'check')}.")
        for case in suite.cases:
            tags = f" [{', '.join(case.tags)}]" if case.tags else ""
            print(f"  {case.id} ({case.kind}){tags}: {case.title}")
        return 0

    if command == "run":
        suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
        runner = qa.QaRunner(config, extra_kinds=kinds, environment=getattr(args, 'environment', '') or '')
        result = runner.run(
            suite,
            tags=args.tag or (),
            ids=args.case or (),
            workers=args.workers,
            write_artifacts=not args.no_artifacts,
            part=_which_part(getattr(args, "part", "") or ""),
        )
        if not args.no_artifacts:
            qa.record_history(config, result)
        _qa_write_report(config, result, args)
        if args.format == "json" and not args.output:
            _print_json(result.to_dict())
        elif not args.output:
            print(qa.render_report(result, args.format, CredentialRedactor(config)))
        return 0 if result.passed else 1

    if command == "record":
        width, height = 1280, 800
        if getattr(args, "viewport", ""):
            parts = str(args.viewport).lower().replace("*", "x").split("x")
            if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
                raise HarnessError("Write the window size as WIDTHxHEIGHT, for example 1280x800")
            width, height = int(parts[0]), int(parts[1])
        selectors.check_url(config, args.url)
        print(f"Opening {args.url} in a browser window.")
        print("Do the thing you want to check, then press Done in the bar at the top.")
        taken = recorder.record(config, args.url, viewport=(width, height), seconds=args.seconds)
        case = taken.case(args.name or "recorded-workflow", args.title or "")
        for line in taken.skipped:
            print(f"  Left out: {line}")
        if args.json:
            _print_json(case)
            return 0
        print("")
        print(f"Wrote down {_count(len(taken.steps), 'step')}:")
        for number, step in enumerate(taken.steps, start=1):
            print(f"  {number}. {step.get('note') or step['do']}")
        if args.dry_run:
            print("")
            print("The check it would add:")
            print(json.dumps(case, indent=2))
            return 0
        try:
            suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
            cases = [item.to_dict() for item in suite.cases]
            name = suite.name
        except HarnessError:
            cases, name = [], "default"
        if any(item["id"] == case["id"] for item in cases):
            raise HarnessError(
                f"This suite already holds a check called {case['id']}. Use --name to give it another."
            )
        cases.append(case)
        written = qa.parse_suite({"schema_version": 1, "name": name, "cases": cases}, extra_kinds=kinds)
        path = qa.write_suite(config, written, getattr(args, "suite", None))
        print("")
        print(f"Added {case['id']} to {path}")
        print("Look at the steps, then run: harness qa run --case " + case["id"])
        return 0

    if command == "changed":
        if getattr(args, "before", "") or getattr(args, "after", ""):
            if not (args.before and args.after):
                raise HarnessError("Name both runs to compare, or neither to use the last two")
            before = comparison.read_report(comparison.kept_run_folder(config, args.before))
            after = comparison.read_report(comparison.kept_run_folder(config, args.after))
        else:
            before, after = comparison.last_two(config)
        found = comparison.compare(before, after, redactor=CredentialRedactor(config))
        if args.json:
            _print_json(found.to_dict())
            return 0 if not found.broke else 1
        print(f"Comparing {found.before_id} with {found.after_id}")
        for line in found.lines():
            print(line)
        return 1 if found.broke else 0

    if command == "share":
        path, page = share.write(
            config,
            str(getattr(args, "run", "") or ""),
            output=str(getattr(args, "output", "") or ""),
            with_pictures=not getattr(args, "no_pictures", False),
        )
        if args.json:
            print(share.as_json(path, page), end="")
            return 0
        for line in share.summary(path, page):
            print(line)
        return 0

    if command == "coverage":
        found = coverage.look(
            config,
            str(getattr(args, "url", "") or ""),
            max_pages=int(getattr(args, "max_pages", coverage.DEFAULT_MAX_PAGES)),
            stay_under=str(getattr(args, "stay_under", "") or ""),
            suite_path=getattr(args, "suite", None),
            extra_kinds=kinds,
        )
        added: list[str] = []
        if getattr(args, "write_missing", False) and found.missing:
            added = coverage.add_missing(
                config,
                [item.address for item in found.missing],
                extra_kinds=kinds,
                suite_path=getattr(args, "suite", None),
            )
        if args.json:
            shape = found.to_dict()
            shape["written"] = added
            _print_json(shape)
            return 0 if added or not found.missing else 1
        for line in found.lines(offer_help=not added):
            print(line)
        if added:
            print("")
            print(
                f"Wrote {len(added)} new check{'' if len(added) == 1 else 's'} for those pages: "
                + ", ".join(added)
            )
            print("Run them with: harness qa run")
            return 0
        return 1 if found.missing else 0

    if command == "starters":
        if args.json:
            _print_json(starters.listed())
            return 0
        print("Ready-made checks. Add one with: harness qa add <name>\n")
        for item in starters.STARTERS:
            print(f"  {item.key}")
            print(f"      {item.title}")
            print(f"      {item.what_it_does}")
            print(f"      To use it: {item.change_this}")
            print(f"      Needs: {item.needs}\n")
        return 0

    if command == "add":
        case = starters.build(args.starter, url=args.url or "", case_id=args.name or "")
        try:
            suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
            cases = [item.to_dict() for item in suite.cases]
            name = suite.name
        except HarnessError:
            cases, name = [], "default"
        if any(item["id"] == case["id"] for item in cases):
            raise HarnessError(
                f"This suite already holds a check called {case['id']}. Use --name to give it another."
            )
        cases.append(case)
        written = qa.parse_suite({"schema_version": 1, "name": name, "cases": cases}, extra_kinds=kinds)
        path = qa.write_suite(config, written, getattr(args, "suite", None))
        print(f"Added {case['id']} to {path}")
        print(f"To use it: {starters.BY_KEY[args.starter].change_this}")
        print("Then run: harness qa run --case " + case["id"])
        return 0

    if command == "remove":
        suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
        keeping = [item.to_dict() for item in suite.cases if item.id != args.case_id]
        if len(keeping) == len(suite.cases):
            known = ", ".join(item.id for item in suite.cases) or "none"
            raise HarnessError(f"There is no check called {args.case_id}. This suite holds: {known}")
        written = qa.parse_suite(
            {"schema_version": 1, "name": suite.name, "cases": keeping}, extra_kinds=kinds
        )
        path = qa.write_suite(config, written, getattr(args, "suite", None))
        print(f"Took {args.case_id} out of {path}")
        print(f"{_count(len(keeping), 'check')} left.")
        return 0

    if command == "fake":
        rows = starters.made_up_rows(args.rows, args.column, args.seed)
        if args.json:
            _print_json(rows)
            return 0
        if args.output:
            import csv
            import io

            page = io.StringIO()
            writer = csv.DictWriter(page, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            path = confined_path(config.project_root, args.output, allow_missing=True, allow_control=True)
            if path.exists() and not args.replace:
                raise HarnessError(
                    f"{args.output} is already there. Choose another name, or use --replace."
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(page.getvalue(), encoding="utf-8")
            print(f"Wrote {_count(len(rows), 'row')} to {path}")
            print('Use it in a check with "rows_file": "' + args.output + '"')
            return 0
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0

    if command == "explain":
        report = handover.read_run(config, getattr(args, "run", "") or "")
        case, evidence = handover.failure_from_run(report, getattr(args, "case", "") or "")
        print(f"Asking about: {case.get('title') or case.get('id')}")
        if args.dry_run:
            print("\n" + handover.failure_question(case, evidence))
            return 0
        answer = handover.explain_failure(config, case, evidence)
        print()
        print(answer.answer)
        print("\nThis is advice from a model. Nothing was changed.")
        return 0

    if command == "ci":
        relative = handover.write_build_file(
            config, args.service, suite=args.suite or "", python=args.python, replace=args.replace
        )
        print(f"Wrote {relative}")
        print("Look at it, change what you need, and commit it.")
        return 0

    if command == "pick":
        width, height = 1280, 800
        if getattr(args, "viewport", ""):
            parts = str(args.viewport).lower().replace("*", "x").split("x")
            if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
                raise HarnessError("Write the window size as WIDTHxHEIGHT, for example 1280x800")
            width, height = int(parts[0]), int(parts[1])
        selectors.check_url(config, args.url)
        print(f"Opening {args.url} in a browser window.")
        print("Click the thing you want to check. Press Escape to give up.")
        picked = selectors.pick(config, args.url, viewport=(width, height), seconds=args.seconds)
        if args.json:
            _print_json(picked.to_dict())
            return 0 if picked.offered else 1
        if picked.gave_up:
            print("\nNothing was picked.")
            return 1
        said = f' that says "{picked.text}"' if picked.text else ""
        print(f"\nYou picked a <{picked.tag or 'thing'}>{said}.")
        if not picked.offered:
            print("\nNothing on this page names it on its own.")
            print("Every name tried matched either nothing or several things:")
            for item in picked.thrown_away[:6]:
                print(f"  {item.selector} matches {_count(item.matches, 'thing')}")
            print("\nAsk whoever wrote the page to add data-testid=\"something\" to it.")
            return 1
        print(f"\n{_count(len(picked.offered), 'name')} you can use, best first:")
        for position, item in enumerate(picked.offered, start=1):
            print(f"  {position}. {selectors.describe(item)}")
        print("\nPaste this into the steps of a browser check:")
        print("  " + json.dumps(selectors.starter_step(picked.offered[0])))
        if picked.thrown_away:
            print(f"\nAlso tried, but not offered:")
            for item in picked.thrown_away[:5]:
                print(f"  {item.selector} matches {_count(item.matches, 'thing')}")
        return 0

    if command == "baseline":
        suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
        wanted = [case.id for case in suite.cases if case.kind == "visual"]
        if args.case:
            unknown = sorted(set(args.case) - set(wanted))
            if unknown:
                raise HarnessError(f"{unknown[0]} is not a screenshot check in this suite")
            wanted = [case_id for case_id in wanted if case_id in set(args.case)]
        if not wanted:
            print("This suite has no screenshot checks yet.")
            print('Add one with "kind": "visual" and a url, then run this again.')
            return 0
        runner = qa.QaRunner(
            config,
            extra_kinds=kinds,
            environment=getattr(args, "environment", "") or "",
            update_baselines=True,
        )
        result = runner.run(suite, ids=wanted, write_artifacts=False)
        for case in result.cases:
            note = case.attempts[-1].evidence if case.attempts else ""
            print(f"  {case.id}: {note or case.status}")
        if not result.passed:
            print("Some pictures could not be taken. The lines above say why.")
        print("Look at the saved pictures before you keep them. They are what every later run is judged against.")
        return 0 if result.passed else 1

    if command == "watch":
        suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
        runner = qa.QaRunner(config, extra_kinds=kinds, environment=getattr(args, 'environment', '') or '')
        tags = args.tag or ()
        chosen = runner.select(suite, tags=tags)
        watcher = ProjectWatcher(
            config,
            interval_seconds=args.interval,
            quiet_seconds=args.quiet,
            # Watching only part of a project is worth saying out loud. Said
            # quietly, it looks exactly like a project where nothing happens.
            on_partial=lambda note: print(f"\nOnly part of this project is watched. {note}"),
        )
        print(f"Watching {config.project_root} for changes.")
        if args.every:
            print(f"They also run every {args.every:g} seconds, whether anything changed or not.")
        print(f"{_count(len(chosen), 'check')} will run each time. Press Ctrl+C to stop.")

        # Every run counts, not only the first one, so a watch session that saw
        # a failure ends with a failing code and can be used as a gate.
        outcome = {"all_passed": True}

        def run_once(changes: Changes | None = None) -> bool:
            if changes is not None:
                print(f"\n{changes.describe() if changes else 'nothing changed, running on the timer'}")
            result = runner.run(suite, tags=tags, write_artifacts=not args.no_artifacts)
            if not args.no_artifacts:
                qa.record_history(config, result)
            counts = result.counts
            headline = "All checks passed" if result.passed else "Some checks failed"
            print(f"{headline}: {counts['passed']} passed, {counts['failed']} failed, "
                  f"{counts['flaky']} flaky, {counts['skipped']} skipped in {result.duration_ms} ms")
            for case in result.cases:
                if case.status == qa.STATUS_FAILED:
                    print(f"  {case.id}: {case.reasons[0] if case.reasons else 'failed'}")
            outcome["all_passed"] = outcome["all_passed"] and result.passed
            return result.passed

        if not args.skip_first:
            run_once()
        try:
            batches = watcher.watch(
                lambda changes: run_once(changes),
                max_batches=args.max_runs,
                repeat_seconds=args.every,
            )
        except KeyboardInterrupt:
            print("\nStopped watching.")
            return 0 if outcome["all_passed"] else 1
        print(f"\nStopped after {_count(batches, 'run')}.")
        if not outcome["all_passed"]:
            print("Some checks failed while watching.")
        return 0 if outcome["all_passed"] else 1

    if command == "env":
        action = args.env_command
        known = datasets.load_environments(config)
        if action == "list":
            if args.json:
                _print_json(known)
                return 0
            if not known:
                print("No settings are saved. Add some with: harness qa env set <name> KEY=value")
                return 0
            for name in sorted(known):
                print(f"  {name}")
                for key in sorted(known[name]):
                    print(f"      {key} = {known[name][key]}")
            return 0
        if action == "set":
            values = dict(known.get(args.name, {}))
            for pair in args.value:
                if "=" not in pair:
                    raise HarnessError(f"Write each value as KEY=value, not {pair}")
                key, _, item = pair.partition("=")
                values[key.strip()] = item
            known[args.name] = values
            path = datasets.save_environments(config, known)
            print(f"Saved {_count(len(values), 'value')} under {args.name} in {path}")
            print("Use them in a check as ${env.NAME}, and run: harness qa run --environment " + args.name)
            return 0
        if action == "delete":
            if args.name not in known:
                raise HarnessError(f"There are no settings named {args.name}")
            known.pop(args.name)
            datasets.save_environments(config, known)
            print(f"Removed the settings named {args.name}")
            return 0
        raise HarnessError("An env command is required")

    if command == "advise":
        try:
            suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
        except HarnessError:
            suite = None
        findings = qa.check_health(config, suite)
        if args.json:
            _print_json(findings)
            return 0
        if not findings:
            print("Nothing to report. Your checks look healthy, or there is not enough history yet.")
            print("Run them a few more times and ask again.")
            return 0
        print(f"{_count(len(findings), 'thing')} worth looking at:\n")
        for finding in findings:
            print(f"  {finding['id']} {finding['problem']}")
            print(f"    Why: {finding['why']}")
            print(f"    What to do: {finding['what_to_do']}\n")
        return 0

    if command == "flaky":
        report = qa.flaky_report(config)
        if args.json:
            _print_json(report)
            return 0
        if not report:
            print("No check looks unstable yet. Run them a few more times to build a history.")
            return 0
        print("Checks whose result keeps changing:")
        for entry in report:
            print(
                f"  {entry['id']}: failed {entry['failures']} of {entry['runs']} runs "
                f"(instability {entry['instability']}). {entry['why']}."
            )
        return 0

    if command == "generate":
        suite: qa.QaSuite | None
        try:
            suite = qa.load_suite(config, getattr(args, "suite", None), kinds)
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
        candidates = qa.parse_generated_cases(response.text, existing, kinds)
        path = qa.save_candidates(config, candidates, source=str(config.get("provider.model")))
        print(f"Saved {_count(len(candidates), 'proposed check')} to {path}")
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
            print("There are no proposed checks. Run 'harness qa generate' to ask the model for some.")
            return 0
        for item in candidates:
            case = item.get("case", {})
            print(f"  {case.get('id')} ({case.get('kind')}): {case.get('title')}")
            for warning in item.get("warnings", []):
                print(f"    warning: {warning}")
        return 0

    if command == "accept":
        suite, accepted = qa.accept_candidates(config, args.case_id, suite_override=getattr(args, "suite", None))
        print(f"Added {_count(len(accepted), 'check')}. The suite now holds {len(suite.cases)}.")
        return 0

    if command == "reject":
        rejected = qa.reject_candidates(config, args.case_id)
        print(f"Removed {_count(len(rejected), 'proposed check')}.")
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

    start = sub.add_parser(
        "start",
        help="Set this project up and open the panel, in one go. The one to run first.",
    )
    start.add_argument("path", nargs="?", default="")
    start.add_argument("--port", type=int, default=0, help="Port for the panel; 0 asks for a free one")
    start.add_argument("--no-panel", action="store_true", help="Set up, but do not open the panel")
    start.add_argument(
        "--no-open-browser", action="store_true", help="Start the panel without opening a browser"
    )
    start.set_defaults(handler=command_start)

    carry_it = sub.add_parser(
        "carry", help="Carry this setup to another machine, or unpack one here"
    )
    carry_sub = carry_it.add_subparsers(dest="carry_command", required=True)
    packing = carry_sub.add_parser("pack", help="Write the setup to one file")
    packing.add_argument("--output", default="harness-setup.json")
    packing.set_defaults(handler=command_carry)
    unpacking = carry_sub.add_parser("unpack", help="Write a carried setup into this project")
    unpacking.add_argument("file")
    unpacking.add_argument(
        "--over-the-top", action="store_true",
        help="Write over anything already here. Off by default, on purpose.",
    )
    unpacking.set_defaults(handler=command_carry)

    init = sub.add_parser("init", help="Scan a project and create .harness/config.json")
    init.add_argument("path", nargs="?", default="")
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
    bundle_command = sub.add_parser(
        "bundle", help="Zip the checks, recent runs, settings, and machine notes to send to somebody"
    )
    bundle_command.add_argument(
        "--part", action="append",
        help=f"Which part to include; may repeat. One of: {', '.join(bundle.PARTS)}, or all",
    )
    bundle_command.add_argument(
        "--runs", type=int, default=bundle.DEFAULT_RUNS, help="How many recent runs to include"
    )
    bundle_command.add_argument("--output", help="Where to write the zip")
    bundle_command.add_argument(
        "--replace", action="store_true", help="Write over a file that is already there"
    )
    bundle_command.add_argument("--read", help="Say what is inside a bundle instead of making one")
    bundle_command.add_argument("--json", action="store_true", help="Print the answer as JSON")
    bundle_command.set_defaults(handler=command_bundle)
    benchmark = sub.add_parser("benchmark", help="Run deterministic checks and optional provider-backed repair tasks")
    benchmark.add_argument("--seed", type=int, default=DEFAULT_SEED)
    benchmark.add_argument("--repetitions", type=int, choices=range(1, 11), default=1, metavar="1..10")
    benchmark.add_argument("--format", choices=["json", "markdown"], default="json")
    benchmark.add_argument("--output", help="Write the selected format to this file")
    benchmark.add_argument("--provider-profile", help="Run isolated agentic repair cases with this trusted local profile")
    benchmark.set_defaults(handler=command_benchmark)
    show = sub.add_parser("config", help="Print effective config and setting sources")
    show.set_defaults(handler=command_config_show)
    seats = sub.add_parser(
        "seats", help="Find the assistants you already pay for, and set them up"
    )
    seats_sub = seats.add_subparsers(dest="seats_command", required=True)
    seats_list = seats_sub.add_parser("list", help="Say which assistants this machine can use")
    seats_list.add_argument("--json", action="store_true")
    seats_list.set_defaults(handler=command_seats)
    seats_setup = seats_sub.add_parser(
        "setup", help="Write a route for each assistant that is ready, and trust it"
    )
    seats_setup.add_argument(
        "--only", action="append", help="Set up only this kind. May repeat."
    )
    seats_setup.add_argument(
        "--no-trust", action="store_true",
        help="Write the settings but do not trust them yet",
    )
    seats_setup.add_argument("--json", action="store_true")
    seats_setup.set_defaults(handler=command_seats)
    trust = sub.add_parser("trust", help="Say the local config file in this project is yours")
    trust.add_argument("--yes", action="store_true", help="Do not ask first")
    trust.add_argument("--show", action="store_true", help="Only say whether it is trusted")
    trust.set_defaults(handler=command_trust)
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
    qa_list = qa_sub.add_parser("list", help="Show every check in the suite")
    qa_list.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_list.add_argument("--json", action="store_true")
    qa_list.set_defaults(handler=command_qa)
    qa_run = qa_sub.add_parser("run", help="Run every check and report what happened")
    qa_run.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_run.add_argument("--tag", action="append", help="Only run checks with this tag; may repeat")
    qa_run.add_argument("--case", action="append", help="Only run this check id; may repeat")
    qa_run.add_argument("--workers", type=int, help="How many checks to run at the same time")
    qa_run.add_argument("--format", choices=qa.REPORT_FORMATS, default="markdown")
    qa_run.add_argument("--output", help="Write the report to this project-relative file")
    qa_run.add_argument("--no-artifacts", action="store_true", help="Do not keep evidence files")
    qa_run.add_argument("--environment", help="Use these saved settings for ${env.NAME} values")
    qa_run.add_argument(
        "--part",
        help=(
            "Run one part of the suite, written as 2/4. Split the checks across "
            "several machines and each one runs its own part"
        ),
    )
    qa_run.set_defaults(handler=command_qa)
    qa_record = qa_sub.add_parser(
        "record", help="Do a workflow by hand once, and get a check written from it"
    )
    qa_record.add_argument("--url", required=True, help="The page to open")
    qa_record.add_argument("--name", help="A name for the new check")
    qa_record.add_argument("--title", help="What the check is called in reports")
    qa_record.add_argument("--viewport", default="", help="Window size as WIDTHxHEIGHT")
    qa_record.add_argument("--seconds", type=float, help="How long to keep recording")
    qa_record.add_argument("--suite", help="Suite file to write instead of the configured one")
    qa_record.add_argument("--dry-run", action="store_true", help="Show the check without adding it")
    qa_record.add_argument("--json", action="store_true", help="Print the check as JSON")
    qa_record.set_defaults(handler=command_qa)
    qa_changed = qa_sub.add_parser(
        "changed", help="Say what moved since the run before: broken, fixed, new, gone, slower"
    )
    qa_changed.add_argument("--before", help="A kept run to compare from, by its folder name")
    qa_changed.add_argument("--after", help="A kept run to compare with, by its folder name")
    qa_changed.add_argument("--json", action="store_true")
    qa_changed.set_defaults(handler=command_qa)
    qa_coverage = qa_sub.add_parser(
        "coverage", help="Walk your site and say which pages have no check at all"
    )
    qa_coverage.add_argument("--url", required=True, help="The address to start walking from")
    qa_coverage.add_argument(
        "--max-pages", type=int, default=coverage.DEFAULT_MAX_PAGES,
        help="How many pages to open at most",
    )
    qa_coverage.add_argument(
        "--stay-under", default="", help="Do not follow links outside this address"
    )
    qa_coverage.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_coverage.add_argument(
        "--write-missing", action="store_true",
        help="Also write a plain 'the page opens' check for every page nobody looks at",
    )
    qa_coverage.add_argument("--json", action="store_true")
    qa_coverage.set_defaults(handler=command_qa)
    qa_share = qa_sub.add_parser(
        "share", help="Make one web page of a run, pictures and all, that you can send to anyone"
    )
    qa_share.add_argument("--run", default="", help="Which kept run, by its folder name")
    qa_share.add_argument("--output", default="", help="Where to write the page")
    qa_share.add_argument(
        "--no-pictures", action="store_true", help="Leave the screenshots out to keep the file small"
    )
    qa_share.add_argument("--json", action="store_true")
    qa_share.set_defaults(handler=command_qa)
    qa_starters = qa_sub.add_parser(
        "starters", help="Show the ready-made checks you can add in one line"
    )
    qa_starters.add_argument("--json", action="store_true")
    qa_starters.set_defaults(handler=command_qa)
    qa_add = qa_sub.add_parser("add", help="Add one ready-made check to your suite")
    qa_add.add_argument("starter", help="Which ready-made check; see: harness qa starters")
    qa_add.add_argument("--url", help="The address it should look at")
    qa_add.add_argument("--name", help="A name for the new check")
    qa_add.add_argument("--suite", help="Suite file to write instead of the configured one")
    qa_add.set_defaults(handler=command_qa)
    qa_remove = qa_sub.add_parser("remove", help="Take one check out of your suite")
    qa_remove.add_argument("case_id", help="Which check to take out")
    qa_remove.add_argument("--suite", help="Suite file to write instead of the configured one")
    qa_remove.set_defaults(handler=command_qa)
    qa_fake = qa_sub.add_parser(
        "fake", help="Make a table of made-up values to run a check against"
    )
    qa_fake.add_argument("--rows", type=int, default=10, help="How many rows")
    qa_fake.add_argument(
        "--column", action="append", default=[],
        help="A column name such as name, email, password; may repeat",
    )
    qa_fake.add_argument("--seed", type=int, default=1, help="The same seed gives the same table")
    qa_fake.add_argument("--output", help="Write it to this file instead of printing it")
    qa_fake.add_argument(
        "--replace", action="store_true", help="Write over a file that is already there"
    )
    qa_fake.add_argument("--json", action="store_true")
    qa_fake.set_defaults(handler=command_qa)
    qa_explain = qa_sub.add_parser(
        "explain", help="Ask your model why a check failed, in plain words"
    )
    qa_explain.add_argument("--case", help="Which failed check; the first one by default")
    qa_explain.add_argument("--run", help="A run report to read instead of the most recent")
    qa_explain.add_argument(
        "--dry-run", action="store_true", help="Show the question without asking anything"
    )
    qa_explain.set_defaults(handler=command_qa)
    qa_ci = qa_sub.add_parser(
        "ci", help="Write the file a build server needs to run these checks"
    )
    qa_ci.add_argument("service", choices=sorted(handover.SERVICES), help="Which build server")
    qa_ci.add_argument("--suite", help="Suite file the build server should run")
    qa_ci.add_argument("--python", default="3.11", help="Which Python version to ask for")
    qa_ci.add_argument("--replace", action="store_true", help="Write over a file that is already there")
    qa_ci.set_defaults(handler=command_qa)
    qa_pick = qa_sub.add_parser(
        "pick", help="Open a page, click something, and get a name a check can use"
    )
    qa_pick.add_argument("--url", required=True, help="The page to open, such as http://127.0.0.1:8765/")
    qa_pick.add_argument("--viewport", default="", help="Window size as WIDTHxHEIGHT, such as 1280x800")
    qa_pick.add_argument("--seconds", type=float, help="How long to wait for the click")
    qa_pick.add_argument("--json", action="store_true", help="Print the answer as JSON")
    qa_pick.set_defaults(handler=command_qa)
    qa_baseline = qa_sub.add_parser(
        "baseline", help="Save today's screenshots as the pictures later runs are judged against"
    )
    qa_baseline.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_baseline.add_argument("--case", action="append", help="Only save this check id; may repeat")
    qa_baseline.add_argument("--environment", help="Use these saved settings for ${env.NAME} values")
    qa_baseline.set_defaults(handler=command_qa)
    qa_watch = qa_sub.add_parser("watch", help="Run the checks again whenever a project file changes")
    qa_watch.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_watch.add_argument("--tag", action="append", help="Only run checks with this tag; may repeat")
    qa_watch.add_argument("--interval", type=float, default=1.0, help="Seconds between looks at the project")
    qa_watch.add_argument("--quiet", type=float, default=0.5, help="Seconds of stillness before running")
    qa_watch.add_argument("--every", type=float, help="Also run this often in seconds, even with no change")
    qa_watch.add_argument("--max-runs", type=int, help="Stop after this many runs")
    qa_watch.add_argument("--skip-first", action="store_true", help="Wait for a change before the first run")
    qa_watch.add_argument("--no-artifacts", action="store_true", help="Do not keep evidence files")
    qa_watch.add_argument("--environment", help="Use these saved settings for ${env.NAME} values")
    qa_watch.set_defaults(handler=command_qa)
    qa_advise = qa_sub.add_parser("advise", help="Say what to do about the checks, based on past runs")
    qa_advise.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_advise.add_argument("--json", action="store_true")
    qa_advise.set_defaults(handler=command_qa)
    qa_env = qa_sub.add_parser("env", help="Keep named settings that checks can use")
    qa_env_sub = qa_env.add_subparsers(dest="env_command", required=True)
    qa_env_list = qa_env_sub.add_parser("list", help="Show every saved set of settings")
    qa_env_list.add_argument("--json", action="store_true")
    qa_env_list.set_defaults(handler=command_qa)
    qa_env_set = qa_env_sub.add_parser("set", help="Add or change one set of settings")
    qa_env_set.add_argument("name")
    qa_env_set.add_argument("value", nargs="+", help="One or more KEY=value pairs")
    qa_env_set.set_defaults(handler=command_qa)
    qa_env_delete = qa_env_sub.add_parser("delete", help="Remove one set of settings")
    qa_env_delete.add_argument("name")
    qa_env_delete.set_defaults(handler=command_qa)
    qa_flaky = qa_sub.add_parser("flaky", help="Name the checks whose result keeps changing")
    qa_flaky.add_argument("--json", action="store_true")
    qa_flaky.set_defaults(handler=command_qa)
    qa_generate = qa_sub.add_parser("generate", help="Ask the model to propose new checks for review")
    qa_generate.add_argument("--suite", help="Suite file to read instead of the configured one")
    qa_generate.add_argument("--focus", help="What the new checks should cover")
    qa_generate.add_argument("--limit", type=int, default=8)
    qa_generate.set_defaults(handler=command_qa)
    qa_candidates = qa_sub.add_parser("candidates", help="Show proposed checks waiting for a decision")
    qa_candidates.add_argument("--json", action="store_true")
    qa_candidates.set_defaults(handler=command_qa)
    qa_accept = qa_sub.add_parser("accept", help="Move proposed checks into the suite")
    qa_accept.add_argument("case_id", nargs="+")
    qa_accept.add_argument("--suite", help="Suite file to write instead of the configured one")
    qa_accept.set_defaults(handler=command_qa)
    qa_reject = qa_sub.add_parser("reject", help="Throw away proposed checks")
    qa_reject.add_argument("case_id", nargs="+")
    qa_reject.set_defaults(handler=command_qa)

    ask = sub.add_parser("ask", help="Answer a project question from indexed evidence")
    ask.add_argument("question")
    ask.set_defaults(handler=command_ask)
    brief = sub.add_parser("brief", help="Print project health, stack, recent runs, and standards order")
    brief.set_defaults(handler=command_brief)
    return root


def _say_anything_in_any_language() -> None:
    """Let this tool print any word a project might hold.

    On Windows, when the output goes to a file or a build server rather than a
    window, Python writes it in an old code page that has no Japanese, no
    arrows, no em dash. A check whose name held one of those stopped the whole
    command with a stack trace and left an empty report behind. Nothing about
    that is the person's fault, and none of it is worth an error.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # An output that will not be told is still an output. Whatever it
            # can write, it writes.
            pass


def main(argv: list[str] | None = None) -> int:
    _say_anything_in_any_language()
    try:
        args = parser().parse_args(argv)
        return int(args.handler(args))
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except (OSError, UnicodeError) as exc:
        # Somewhere to write, and something in the way of writing it. A
        # sentence is more use than a stack trace.
        print(f"error: could not write the output: {exc}", file=sys.stderr)
        return 2
