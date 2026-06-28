#!/usr/bin/env bash
# Propagate the canonical protocol module into both runtimes.
# The wire format must stay byte-identical on both ends, so protocol/protocol.py
# is the single source of truth and is copied (not imported) into each package.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/protocol/protocol.py"

cp "$SRC" "$ROOT/plugin/py_modules/protocol.py"
cp "$SRC" "$ROOT/host/deckontrol_host/protocol.py"
echo "synced protocol.py -> plugin/py_modules and host/deckontrol_host"
