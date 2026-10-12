#!/bin/sh
# Keeps the game server alive: a crashed server is back within 30 seconds.
# The log lives on the flash drive, so it is cut before it grows large.
PATH=/opt/bin:/opt/sbin:/opt/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
cd /opt/heartcraft
while true; do
    pgrep -f "/opt/heartcraft/server.py" >/dev/null 2>&1 || \
        python3 /opt/heartcraft/server.py --port 8092 >> /opt/heartcraft/server.log 2>&1 &
    if [ -f /opt/heartcraft/server.log ] && [ "$(wc -c < /opt/heartcraft/server.log)" -gt 1000000 ]; then
        : > /opt/heartcraft/server.log
    fi
    sleep 30
done
