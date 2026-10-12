#!/bin/bash
# Runs tests/e2e.mjs: a throwaway game server (worlds rose and mint, admin "boss" with code 999)
# and headless Chrome, both removed afterwards.
#
#   tests/e2e.sh [screenshot dir]
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

printf 'rose Розовая долина\nmint Мятный остров\n' > "$TMP/worlds"
echo "boss 999" > "$TMP/admin"
python3 "$HERE/router/server.py" --port "$GAME_PORT" --page "$HERE/index.html" \
    --worlds "$TMP/worlds" --data "$TMP/data" --admin "$TMP/admin" 2> "$TMP/server.log" &
SERVER=$!
# Without these flags Chrome freezes background windows, and the second player would stand still.
"$CHROME" --headless=new --remote-debugging-port="$DEVTOOLS_PORT" --window-size=1000,640 \
    --disable-background-timer-throttling --disable-renderer-backgrounding \
    --disable-backgrounding-occluded-windows --user-data-dir="$TMP/chrome" --no-first-run \
    about:blank > /dev/null 2>&1 &
BROWSER=$!
sleep 3
node "$HERE/tests/e2e.mjs" "$DEVTOOLS_PORT" "http://127.0.0.1:$GAME_PORT/" "${1:-}"
