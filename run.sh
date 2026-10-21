#!/usr/bin/env bash
# Wortgut launcher — portable: works from any directory.
#
#   ./run.sh              start the app on 127.0.0.1:8000
#   ./run.sh --reload     start with auto-reload (development)
#
# Environment overrides are read from .env if present (see .env.example).

set -euo pipefail

# Resolve this script's directory so the app runs the same from anywhere.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: 'uv' is not installed. See https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# Keep the environment in sync without needing a separate step.
# Try the local cache first: the app must start with the network off, and the
# model is a direct-URL dependency that uv would otherwise re-check online on
# every launch. If the cache is incomplete (first run, changed lockfile), fall
# back to a normal online sync.
uv sync --quiet --offline 2>/dev/null || uv sync --quiet

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# --no-sync: the sync above already ran, and `uv run` would otherwise re-check
# the direct-URL model online, which fails when the network is off.
if [[ "${1:-}" == "--reload" ]]; then
  exec uv run --no-sync uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
fi

exec uv run --no-sync uvicorn app.main:app --host "$HOST" --port "$PORT"
