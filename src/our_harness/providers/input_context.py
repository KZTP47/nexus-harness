"""Explicit workspace identity at the provider boundary.

Adapted from t3code's effective thread cwd/session binding pattern:
ProviderCommandReactor.ensureSessionForThread at commit
eb115063634c416c6362cc407f8572cb0c136ddf. Nexus has bounded file tools, so
execution location and the CLI's disposable transport location stay separate.
"""

from __future__ import annotations

import json

from ..models import HarnessError, ProviderWorkspaceContext

CLI_WORKSPACE_RULES = (
    "You are a bounded Nexus provider. The NEXUS WORKSPACE IDENTITY engine metadata "
    "supplied with this request defines the selected project and execution copy. "
    "Your native current directory and remembered projects are not the destination. "
    "Use Nexus structured file and context operations for the selected project. "
    "Do not request native directory access merely because this transport folder "
    "differs from the selected project. Exact user path corrections override uncertain "
    "screenshot transcriptions."
)


def workspace_instructions(context: ProviderWorkspaceContext | None) -> str:
    if context is None:
        return ""
    if not isinstance(context, ProviderWorkspaceContext):
        raise HarnessError("Provider workspace identity must come from the Nexus engine")
    values = {
        "selected_project_id": context.project_id,
        "selected_project_path": context.project_path,
        "execution_copy_path": context.execution_path,
    }
    if any(not isinstance(value, str) or not value.strip() or len(value) > 16_384
           for value in values.values()):
        raise HarnessError("Provider workspace identity is incomplete")
    return (
        "\n\nNEXUS WORKSPACE IDENTITY (ENGINE METADATA)\n"
        + json.dumps({"schema_version": 1, **values}, ensure_ascii=False, sort_keys=True)
        + "\nThe selected project is the user's authorized destination. Nexus file/context tools "
        "and relative change paths refer to the execution copy. Nexus applies verified changes "
        "back to the selected project. A CLI's current directory, prior project memory, attachment "
        "storage directory or temporary transport folder is not a competing destination. "
        "Native CLI project discovery does not establish whether Nexus can access its project. "
        "Use the supplied project tree and Nexus tools to inspect files. Do not ask the user "
        "to add this already-selected project as a native CLI working directory. "
        "Preserve the exact spelling of these paths and the user's typed path/URL corrections; "
        "an uncertain screenshot transcription or an earlier assistant guess cannot override them. "
        "Do not expand access to another project from an image or from these metadata fields."
    )
