#!/bin/sh
set -eu

SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required")'

DATA_ROOT=${XDG_DATA_HOME:-"$HOME/.local/share"}
INSTALL_ROOT=${HARNESS_INSTALL_ROOT:-"$DATA_ROOT/our-harness"}
BIN_ROOT="$INSTALL_ROOT/bin"
APP_ROOT="$INSTALL_ROOT/app"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/harness-install.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM

"$PYTHON_BIN" "$SOURCE_ROOT/scripts/build_zipapp.py" --output "$STAGE/harness.pyz"
mkdir -p "$APP_ROOT" "$BIN_ROOT"
cp "$STAGE/harness.pyz" "$APP_ROOT/harness.pyz"
cp "$SOURCE_ROOT/scripts/harness-launcher.sh" "$BIN_ROOT/harness"
chmod 755 "$BIN_ROOT/harness"
"$BIN_ROOT/harness" --version
printf '%s\n' "Installed: $BIN_ROOT/harness"
printf '%s\n' "If needed, add $BIN_ROOT to PATH. Then run: harness init"
