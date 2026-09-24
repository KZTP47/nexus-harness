"""Native tool permission profiles for engine-owned project copies."""
from pathlib import Path

from ..models import HarnessError
from ..goal_workspaces import _direct

CONTRACT = "nexus-native-agent/v1"
# Characters a Claude Code permission rule cannot carry exactly: separators,
# wildcards, quotes and whitespace inside an argument, and the characters
# cmd.exe expands or escapes when Claude runs through claude.cmd (%VAR%, ^, !).
# A command containing one is never turned into a (possibly different) rule;
# it stays advisory.
_UNRULABLE = set(",()\n\r*?\"'`%^!")
_RULE_TOOLS = ("Bash", "PowerShell")
_PROGRAM_SUFFIXES = ("", ".cmd", ".exe", ".bat", ".ps1")


def denied_commands(request):
    context = request.workspace_context
    held = getattr(context, "denied_commands", ()) if context is not None else ()
    return [tuple(str(part) for part in one) for one in held or () if one]


def _enforceable(argv):
    return bool(argv) and all(
        str(part) and not (_UNRULABLE & set(str(part))) and not any(ch.isspace() for ch in str(part))
        for part in argv)


def _program_spellings(program):
    """The spellings of one program a shell would run: its given name, its
    basename, and the basename with the usual Windows launcher suffixes."""
    text = str(program).replace("\\", "/")
    base = text.rsplit("/", 1)[-1]
    stem = base
    for suffix in _PROGRAM_SUFFIXES[1:]:
        if stem.casefold().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    spellings = [text, base] + [stem + suffix for suffix in _PROGRAM_SUFFIXES]
    # A drive-qualified path cannot be written in a rule (its colon would read
    # as the rule's own separator); its basename spellings cover it.
    return list(dict.fromkeys(one for one in spellings if one and ":" not in one))


def claude_deny_rules(commands):
    """Claude Code --disallowedTools rules for the user's denied commands.

    Returns (rules, unenforceable). Each enforceable denied argv becomes an
    exact ``Bash(<command>)`` and ``PowerShell(<command>)`` rule, matching the
    user's exact denial as Nexus's own check does (never a prefix, which would
    block every ``git`` command for a denied ``git``), for every spelling Nexus can derive: the program's own name, its basename and
    launcher variants (``npm``, ``npm.cmd``, ``npm.exe``), with the arguments as
    given and normalised (``./dist`` vs ``dist``). A command whose arguments
    contain whitespace, quotes or wildcards cannot be written as an exact rule;
    it is returned as unenforceable (advisory, and recorded), never widened.
    """
    from ..goal_access import normalized_argv
    rules, unenforceable = [], []
    for argv in commands:
        argv = [str(part) for part in argv]
        if not _enforceable(argv):
            unenforceable.append(list(argv))
            continue
        rests = list(dict.fromkeys([" ".join(argv[1:]), " ".join(normalized_argv(argv)[1:])]))
        for program in _program_spellings(argv[0]):
            for rest in rests:
                spelling = (program + " " + rest).strip()
                for tool in _RULE_TOOLS:
                    rule = tool + "(" + spelling + ")"
                    if rule not in rules:
                        rules.append(rule)
    return rules, unenforceable


def denial_instructions(request, *, enforced=False):
    commands = denied_commands(request)
    if not commands:
        return ""
    listed = "; ".join(" ".join(one) for one in commands)
    return ("\n\nUSER-DENIED COMMANDS\nThe user explicitly denied these commands: " + listed + ". "
            "You must not run them, in any spelling. "
            + ("Your CLI also enforces this denial as a permission rule. " if enforced else
               "This CLI cannot enforce the denial itself. ")
            + "Everything else in your granted access remains available.")


def workspace(request):
    if not request.native_execution:
        return None
    if request.native_execution not in {"inspect", "work"} or request.workspace_context is None:
        raise HarnessError("Native execution requires an engine-owned workspace and permission profile")
    context = request.workspace_context
    root = _direct(Path(request.working_directory))
    source = _direct(Path(context.project_path))
    if context.execution_mode == "facilitator":
        if not request.working_directory or not Path(request.working_directory).is_absolute() \
                or root != source or root != _direct(Path(context.execution_path)) or not root.is_dir():
            raise HarnessError("Facilitator native execution must use the selected project folder")
        return root
    if not request.working_directory or not Path(request.working_directory).is_absolute() \
            or root != _direct(Path(context.execution_path)) or not root.is_dir() \
            or root == source or source in root.parents or root in source.parents:
        raise HarnessError("Native execution must use an independent agent copy outside the real project")
    return root


def instructions(request, *, denials_enforced=False):
    root = workspace(request)
    if root is None:
        return ""
    return _instructions(request, root) + denial_instructions(request, enforced=denials_enforced)


def _instructions(request, root):
    writable = request.native_execution == "work"
    if request.workspace_context.execution_mode == "facilitator":
        return (
            "NATIVE AGENT EXECUTION\nYour working directory is the selected project: " + str(root) + ". "
            + ("Use native commands and edits under the granted access. Saved files are immediately visible. "
               "Return changes=[] for files already edited. " if writable else
               "Use native inspection tools. Propose permitted edits through changes and request commands through Nexus tool_calls. ")
            + "Report command failures and test results accurately; they do not hide saved work. "
            "Respect the user's selected roles and permissions. Return the requested structured action."
        )
    return (
        "NATIVE AGENT EXECUTION\nYour working directory is your own project copy: " + str(root) + ". "
        "Use your native file, search, web and skill tools to investigate the task. "
        + ("Use native commands, scripts and edits in this copy. Nexus collects the actual changed files; "
           "return changes=[] for files already edited with native tools. " if writable else
           "This turn permits native inspection. Propose edits in the changes field and request commands "
           "through Nexus tools under the current user access setting. ")
        + "Nexus context tools also refer to this copy. Tool permission denials are real constraints: report them "
        "or ask a concrete question; do not bypass them. Follow the USER-SELECTED COLLABORATION workspace permissions when present; otherwise edit only your own copy. "
        "Nexus reviews and verifies candidates before publishing to the original project. "
        "Keep temporary downloads and dependencies separate from deliverables. "
        "A native test result is useful evidence, but Nexus performs its own final verification. "
        "Always return the requested structured final action. Use Nexus tool_calls for team communication."
    )
