#!/usr/bin/env bash
# Fresh-clone smoke test. Regression check for bug B-1 ("./run.sh removes the
# spaCy model") and proof that the README setup works from nothing.
#
#   scripts/smoke_fresh_clone.sh
#
# What it does:
#   1. clones the COMMITTED state of this repo into a temp directory
#   2. follows the README: uv sync, fetch_data.sh, both imports
#   3. starts the app THROUGH ./run.sh (never raw uvicorn), checks /ready, pastes
#      a German text and reads it; stops it
#   4. starts it through ./run.sh a SECOND time and repeats the checks
#      (this is the step that failed before the fix: the re-sync deleted the model)
#   5. starts it a THIRD time inside a network namespace with only loopback
#      (skipped with a notice if 'unshare -rn' is unavailable)
#
# Touches: only the temp directory. Your own data/ and database are not used.
# Downloads ~470 MB unless SMOKE_DATA_CACHE points at a directory that already
# holds the files fetch_data.sh produces (then they are copied instead).
# Needs: git, uv, curl, tar. Offline phase also needs util-linux 'unshare'.

set -euo pipefail

PORT="${PORT:-8766}"
URL="http://127.0.0.1:$PORT"
TEXT=$'Der alte Müller ging jeden Morgen zur Mühle. Dort traf er seine Freunde.\n\nAm Abend war er müde, aber glücklich.'

wait_ready() {
  for _ in $(seq 90); do
    if curl -s -m 2 "$URL/health" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "FAIL: app did not come up; log follows" >&2
  cat "$1" >&2
  return 1
}

check_app() { # $1 = label, $2 = log file
  local ready code
  ready="$(curl -s "$URL/ready")"
  echo "[$1] /ready: $ready"
  [[ "$ready" == *'"spacy_model_loaded":true'* ]] || { echo "FAIL: spaCy model not loaded" >&2; return 1; }
  [[ "$ready" == *'"ready":true'* ]] || { echo "FAIL: not ready" >&2; return 1; }

  local location
  location="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -X POST "$URL/texts" \
    --data-urlencode "title=Smoke $1" --data-urlencode "text=$TEXT")"
  echo "[$1] POST /texts: $location"
  [[ "$location" == 303* ]] || { echo "FAIL: paste did not redirect" >&2; return 1; }

  code="$(curl -s -o /dev/null -w '%{http_code}' "${location#303 }")"
  echo "[$1] GET reader: $code"
  [[ "$code" == 200 ]] || { echo "FAIL: reader did not return 200" >&2; return 1; }
}

run_through_run_sh() { # $1 = label
  local log="$WORK/run-$1.log" pid
  ./run.sh >"$log" 2>&1 &
  pid=$!
  wait_ready "$log"
  check_app "$1" "$log"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  # run.sh ends in 'exec uv run uvicorn'; make sure nothing is left on the port.
  pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  sleep 1
}

if [[ "${1:-}" == "--offline-phase" ]]; then
  WORK="$2"
  cd "$WORK/wortgut"
  ip link set lo up
  if curl -s -m 5 -o /dev/null https://example.com; then
    echo "FAIL: network is reachable inside the offline phase" >&2
    exit 1
  fi
  echo "[offline] outbound request to example.com fails, as intended"
  export PORT
  run_through_run_sh offline
  exit 0
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
export PORT
echo "== work dir: $WORK"

echo "== clone of committed state: $(git -C "$SRC" rev-parse --short HEAD)"
git clone -q "$SRC" "$WORK/wortgut"
cd "$WORK/wortgut"

echo "== README step 1: uv sync"
uv sync
echo "== README step 2: fetch_data.sh"
if [[ -n "${SMOKE_DATA_CACHE:-}" ]]; then
  mkdir -p data && cp -r "$SMOKE_DATA_CACHE"/. data/
fi
./scripts/fetch_data.sh
echo "== README step 3: imports"
uv run python scripts/import_freq.py
uv run python scripts/import_dict.py

echo "== run 1 through ./run.sh"
run_through_run_sh first
echo "== run 2 through ./run.sh (model must survive the re-sync)"
run_through_run_sh second
.venv/bin/python -c "import de_core_news_sm; print('[second] model importable:', de_core_news_sm.__version__)"

echo "== run 3 through ./run.sh with the network off"
if unshare -rn true 2>/dev/null; then
  unshare -rn bash "$SRC/scripts/smoke_fresh_clone.sh" --offline-phase "$WORK"
else
  echo "SKIPPED: 'unshare -rn' unavailable here, offline phase not run"
fi

echo "== SMOKE TEST PASSED ($WORK)"
