"""Portable programming-agent harness."""

import os as _os
import sys as _sys

# Python's embedded Windows runtime may run without the machine-wide long-path
# policy. Normalize import roots before importing engine modules or dependencies.
# This is native I/O spelling, not a different installation or access grant.
if _os.name == "nt":
    def _native_import_path(value):
        if not value or not _os.path.isabs(value) or value.startswith("\\\\?\\"):
            return value
        absolute = _os.path.abspath(value)
        return "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute

    __path__[:] = [_native_import_path(value) for value in __path__]
    _sys.path[:] = [_native_import_path(value) for value in _sys.path]
    del _native_import_path

__version__ = "0.2.30"

#: The name people see: on the panel, in the desktop window, and in anything
#: the harness writes. Named once here so the parts cannot drift apart.
PRODUCT_NAME = "Nexus Harness"
