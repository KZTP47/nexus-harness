"""Resolve simple package test scripts into existing contained runner profiles.

This is deliberately not a shell. Compound scripts and lifecycle hooks are
rejected rather than silently omitted or executed outside containment.
"""
import json
import re
import shlex
from pathlib import Path

from .models import HarnessError


def resolve_package_command(root: Path, command: list[str]) -> list[str]:
    if not command or Path(command[0]).stem.lower() not in {"npm", "pnpm", "yarn"}:
        return command
    args = command[1:]
    if args and args[0] in {"run", "run-script"}:
        args = args[1:]
    if not args or args[0].startswith("-"):
        raise HarnessError("Use a package script name or an explicit supported test runner command")
    name, tail = args[0], args[1:]
    if tail[:1] == ["--"]:
        tail = tail[1:]
    try:
        package = root / "package.json"
        if package.is_symlink() or package.stat().st_size > 1_000_000:
            raise ValueError("package.json must be a bounded ordinary file")
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        script = scripts.get(name)
        if not isinstance(script, str) or not script.strip():
            raise ValueError("the selected script is missing")
        if scripts.get("pre" + name) or scripts.get("post" + name):
            raise ValueError("lifecycle hooks require an explicit contained runner command")
        if re.search(r"[&|;<>`$%\r\n]", script):
            raise ValueError("shell operators and environment expansion require an explicit contained runner command")
        words = shlex.split(script, posix=True)
        if words[:1] == ["npx"]:
            words = words[1:]
        if words[:1] == ["playwright"]:
            words = ["node", "node_modules/@playwright/test/cli.js", *words[1:]]
        if not words or words[0] not in {"node", "nodejs", "python", "python3", "py"}:
            raise ValueError("this package script does not use a supported contained Node, Python, or Playwright runner")
        return [*words, *tail]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise HarnessError("Package test command could not run in the protected runtime: " + str(exc)) from exc
