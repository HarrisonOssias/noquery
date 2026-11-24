#!/usr/bin/env bash
# Starts the demo sidecar with the Claude planner on, runs one live ask() from Python, then stops it.
# Needs ANTHROPIC_API_KEY in the environment. Usage, from the repository root:
#   ANTHROPIC_API_KEY=... bash examples/sidecar/run-live-ask.sh ["your question"]
set -euo pipefail

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Set ANTHROPIC_API_KEY first." >&2
  exit 1
fi

cd "$(dirname "$0")/../.."
pnpm build > /dev/null

export NOQUERY_SERVICE_TOKEN="${NOQUERY_SERVICE_TOKEN:-$(openssl rand -hex 24)}"
export NOQUERY_TIMEZONE="${NOQUERY_TIMEZONE:-America/Toronto}"
export PORT="${PORT:-8417}"
export NOQUERY_URL="http://127.0.0.1:${PORT}"
unset NOQUERY_PLANNER

LOG="$(mktemp)"
(cd examples/sidecar && exec node ../../packages/server/dist/bin.js noquery.config.mjs > "$LOG" 2>&1) &
SIDECAR=$!
trap 'kill "$SIDECAR" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -s "$NOQUERY_URL/healthz" | grep -q '"planner":true'; then break; fi
  if ! kill -0 "$SIDECAR" 2>/dev/null; then cat "$LOG" >&2; exit 1; fi
  sleep 0.5
done

STATUS=0
PYTHONPATH=clients/python/src python3 examples/sidecar/live_ask.py "$@" || STATUS=$?

echo
echo "sidecar log:"
cat "$LOG"
exit "$STATUS"
