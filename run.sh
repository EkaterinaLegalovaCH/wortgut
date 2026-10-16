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
uv sync --quiet

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

if [[ "${1:-}" == "--reload" ]]; then
  exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
fi

exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT"
