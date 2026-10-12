#!/bin/bash
# Runs tests/e2e.mjs: a throwaway game server and headless Chrome, both removed afterwards.
#
#   tests/e2e.sh [screenshot.png]
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
GAME_PORT=8093
DEVTOOLS_PORT=9333
TMP="$(mktemp -d)"
cleanup() {
    kill "${SERVER:-}" "${BROWSER:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

echo pw > "$TMP/password"
python3 "$HERE/router/server.py" --port "$GAME_PORT" --page "$HERE/index.html" \
    --password "$TMP/password" --world "$TMP/world.json" 2> "$TMP/server.log" &
SERVER=$!
# Without these flags Chrome freezes background windows, and the second player would stand still.
"$CHROME" --headless=new --remote-debugging-port="$DEVTOOLS_PORT" --window-size=1000,640 \
    --disable-background-timer-throttling --disable-renderer-backgrounding \
    --disable-backgrounding-occluded-windows --user-data-dir="$TMP/chrome" --no-first-run \
    about:blank > /dev/null 2>&1 &
BROWSER=$!
sleep 3
node "$HERE/tests/e2e.mjs" "$DEVTOOLS_PORT" "http://127.0.0.1:$GAME_PORT/" "${1:-}"
