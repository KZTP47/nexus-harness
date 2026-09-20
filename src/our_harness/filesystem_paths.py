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


def plain_path(path: Path) -> Path:
    """Spell a path the way other programs parse it, without the long prefix.

    Bundled third-party tools reject the extended Win32 spelling, so drop it
    where a path leaves the engine for another process. Device and volume
    paths have no plain form and are returned unchanged.
    """
    if os.name != "nt":
        return path
    text = str(path)
    folded = text.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        return Path("\\\\" + text[8:])
    remainder = text[4:]
    if folded.startswith("\\\\?\\") and _is_drive_path(remainder):
        return Path(remainder)
    return path


def _is_drive_path(value: str) -> bool:
    return (len(value) > 2 and value[0].isascii() and value[0].isalpha()
            and value[1] == ":" and value[2] == "\\")
