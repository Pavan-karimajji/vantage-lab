#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PYCMD=""
if command -v python3 >/dev/null 2>&1; then PYCMD=python3
elif command -v python >/dev/null 2>&1; then PYCMD=python
else echo "ERROR: Python was not found in PATH." >&2; exit 1
fi

exec "$PYCMD" "$(dirname "$0")/scripts/build.py" "$@"
