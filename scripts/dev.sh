#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "$0")/.." && pwd)"
backend_pid=""
frontend_pid=""

shutdown() {
  trap - EXIT INT TERM
  [ -n "$frontend_pid" ] && kill "$frontend_pid" 2>/dev/null || true
  [ -n "$backend_pid" ] && kill "$backend_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}

trap shutdown EXIT INT TERM

cd "$root_dir"
python3 app.py --port 4173 &
backend_pid="$!"

cd "$root_dir/frontend"
npm run dev &
frontend_pid="$!"

wait "$backend_pid" "$frontend_pid"
