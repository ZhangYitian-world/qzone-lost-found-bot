#!/bin/zsh
set -eu
cd "${0:A:h:h}"
umask 077
data/bridge/node_modules/.bin/tsx scripts/bridge-server.mjs &
bridge_pid=$!
trap 'kill "$bridge_pid" 2>/dev/null || true' EXIT INT TERM
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8766 --workers 1 --no-access-log
