#!/usr/bin/env bash
PORT=${1:-3344}

echo "Starting Token Telemetry on http://127.0.0.1:$PORT..."

# Automatically open browser across operating systems if not disabled
if [[ "$*" != *"--no-browser"* ]]; then
  (
    sleep 1
    if command -v open >/dev/null 2>&1; then
      open "http://127.0.0.1:$PORT"
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "http://127.0.0.1:$PORT"
    fi
  ) &
fi

python3 "$(dirname "$0")/server.py" "$@"
