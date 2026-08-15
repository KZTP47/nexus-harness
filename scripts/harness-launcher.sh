#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APPLICATION="$SCRIPT_DIR/../app/harness.pyz"
if [ ! -f "$APPLICATION" ]; then
    printf '%s\n' "Harness application was not found beside the launcher: $APPLICATION" >&2
    exit 1
fi
if command -v python3 >/dev/null 2>&1; then
    PYTHON_COMMAND=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON_COMMAND=python
else
    printf '%s\n' 'Python 3.11 or newer is required on PATH.' >&2
    exit 1
fi
exec "$PYTHON_COMMAND" "$APPLICATION" "$@"
