"""Native I/O spelling, separate from persisted paths and ownership identity."""
from __future__ import annotations

import os
from pathlib import Path


def filesystem_path(path: Path) -> Path:
    """Use Windows Unicode file APIs without requiring a registry policy change.

    This does not validate or grant access. Callers still confine paths before
    using them; keep this spelling at the I/O boundary, out of saved identities.
    """
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if not absolute.startswith("\\\\?\\"):
        absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    return Path(absolute)
