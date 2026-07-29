#!/bin/sh
set -eu

term_child() {
    if [ -n "${child_pid:-}" ] && kill -0 "$child_pid" 2>/dev/null; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
}

trap 'term_child; exit 0' INT TERM

echo "Starting iCSee PTZ web supervisor"
while true; do
    python -u /app/app.py &
    child_pid=$!
    echo "Web service started with PID $child_pid"

    set +e
    wait "$child_pid"
    status=$?
    set -e
    child_pid=""

    echo "Web service exited with code $status; restarting in 1 second"
    sleep 1
done
