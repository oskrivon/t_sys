#!/bin/bash
# Engine watchdog — checks heartbeat file and restarts if stale.
# Install: crontab -e → */5 * * * * /root/trading/scripts/watchdog_engine.sh

HEARTBEAT="/tmp/engine_heartbeat"
MAX_AGE=600  # 10 minutes — if no heartbeat, restart
LOGFILE="/root/trading/data/logs/watchdog.log"
ENGINE_CMD="cd /root/trading && python scripts/run_engine.py"

now=$(date +%s)

if [ ! -f "$HEARTBEAT" ]; then
    echo "$(date -Iseconds) heartbeat file missing — starting engine" >> "$LOGFILE"
    # Check if engine is running at all
    if pgrep -f "run_engine.py" > /dev/null; then
        echo "$(date -Iseconds) engine PID alive but no heartbeat — killing" >> "$LOGFILE"
        pkill -9 -f "run_engine.py"
        sleep 2
    fi
    nohup bash -c "$ENGINE_CMD" >> /dev/null 2>&1 &
    exit 0
fi

last_beat=$(cat "$HEARTBEAT" | cut -d. -f1)
age=$((now - last_beat))

if [ "$age" -gt "$MAX_AGE" ]; then
    echo "$(date -Iseconds) heartbeat stale (${age}s) — restarting engine" >> "$LOGFILE"
    pkill -9 -f "run_engine.py"
    sleep 2
    nohup bash -c "$ENGINE_CMD" >> /dev/null 2>&1 &
fi
