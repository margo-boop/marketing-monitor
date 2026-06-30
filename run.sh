#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"

# Проверяем стандартные места Python на macOS
for candidate in /usr/local/bin/python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
  if [ -x "$candidate" ]; then
    PY="$candidate"
    break
  fi
done

cd "$ROOT"
exec "$PY" app.py
