#!/usr/bin/env bash
# Starts a throwaway coordinator (fresh SQLite database and staging folder),
# runs the end-to-end test against it, then cleans up.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
PORT="${PORT:-8765}"

export DJANGO_DEBUG=1
export DJANGO_SECRET_KEY="e2e-only-$(date +%s)"
export DATABASE_URL="sqlite:///$WORK/db.sqlite3"
export STAGING_DIR="$WORK/staging"
export AUTH_THROTTLE_RATE="300/minute"
export AUTH_USER_THROTTLE_RATE="100/minute"
export LOG_LEVEL=WARNING

cleanup() {
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT/coordinator"
python3 manage.py migrate --noinput >/dev/null
python3 manage.py runserver "127.0.0.1:$PORT" --noreload >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.2
done

cd "$ROOT"
if ! BASE_URL="http://127.0.0.1:$PORT" node scripts/e2e.mjs; then
  echo "--- server log ---"
  tail -50 "$WORK/server.log"
  exit 1
fi
