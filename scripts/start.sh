#!/bin/zsh
set -eu
cd "${0:A:h:h}"
if [ ! -f .env ]; then
  .venv/bin/python scripts/setup.py
fi
exec .venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8766 --workers 1 --no-access-log
