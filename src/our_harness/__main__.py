import sys

if sys.version_info < (3, 11):
    found = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(f"Nexus Harness requires Python 3.11 or newer; this interpreter is {found}.")

from .cli import main

raise SystemExit(main())
