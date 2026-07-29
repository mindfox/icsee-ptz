#!/bin/sh
set -eu

POLL_INTERVAL=${POLL_INTERVAL:-60}

case "$POLL_INTERVAL" in
    ''|*[!0-9]*)
        echo "POLL_INTERVAL must be a positive integer" >&2
        exit 2
        ;;
esac

if [ "$POLL_INTERVAL" -lt 1 ]; then
    echo "POLL_INTERVAL must be at least 1 second" >&2
    exit 2
fi

# The normal container process stays alive and launches one isolated polling
# cycle at a time. A failed test or temporary GitHub/network error is logged,
# then the next cycle still runs.
if [ "${WORKER_ONCE:-0}" != "1" ]; then
    echo "Starting icsee lab worker; polling every ${POLL_INTERVAL}s"
    while true; do
        if WORKER_ONCE=1 "$0"; then
            :
        else
            status=$?
            echo "Worker cycle failed with exit code $status; retrying after ${POLL_INTERVAL}s" >&2
        fi
        sleep "$POLL_INTERVAL"
    done
fi

required_vars="GITHUB_REPOSITORY GITHUB_TOKEN SOURCE_BRANCH RESULTS_BRANCH TEST_SCRIPT CAMERA_HOST CAMERA_PORT CAMERA_USERNAME CAMERA_PASSWORD"
for name in $required_vars; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "Missing required environment variable: $name" >&2
        exit 2
    fi
done

SOURCE_DIR=/workspace/source
RESULTS_DIR=/workspace/results
RAW_LOG=/tmp/icsee-test-raw.log
SAFE_LOG=/tmp/icsee-test.log
ASKPASS=/tmp/git-askpass.sh

cat > "$ASKPASS" <<'EOF'
#!/bin/sh
case "$1" in
    *Username*) printf '%s\n' 'x-access-token' ;;
    *Password*) printf '%s\n' "$GITHUB_TOKEN" ;;
    *) printf '\n' ;;
esac
EOF
chmod 700 "$ASKPASS"
export GIT_ASKPASS="$ASKPASS"
export GIT_TERMINAL_PROMPT=0

REPO_URL="https://github.com/${GITHUB_REPOSITORY}.git"

if [ ! -d "$SOURCE_DIR/.git" ]; then
    rm -rf "$SOURCE_DIR"
    git clone --branch "$SOURCE_BRANCH" --single-branch "$REPO_URL" "$SOURCE_DIR"
else
    git -C "$SOURCE_DIR" remote set-url origin "$REPO_URL"
    git -C "$SOURCE_DIR" fetch origin "$SOURCE_BRANCH"
    git -C "$SOURCE_DIR" checkout -B "$SOURCE_BRANCH" "origin/$SOURCE_BRANCH"
fi

SOURCE_SHA=$(git -C "$SOURCE_DIR" rev-parse HEAD)

if [ ! -d "$RESULTS_DIR/.git" ]; then
    rm -rf "$RESULTS_DIR"
    git clone --no-checkout "$REPO_URL" "$RESULTS_DIR"
fi

git -C "$RESULTS_DIR" remote set-url origin "$REPO_URL"
git -C "$RESULTS_DIR" fetch origin "+refs/heads/*:refs/remotes/origin/*"

if git -C "$RESULTS_DIR" show-ref --verify --quiet "refs/remotes/origin/$RESULTS_BRANCH"; then
    git -C "$RESULTS_DIR" checkout -B "$RESULTS_BRANCH" "origin/$RESULTS_BRANCH"
else
    git -C "$RESULTS_DIR" checkout --orphan "$RESULTS_BRANCH"
    git -C "$RESULTS_DIR" rm -rf . >/dev/null 2>&1 || true
fi

LAST_TESTED=""
if [ -f "$RESULTS_DIR/last-tested-sha.txt" ]; then
    LAST_TESTED=$(cat "$RESULTS_DIR/last-tested-sha.txt")
fi

if [ "$SOURCE_SHA" = "$LAST_TESTED" ]; then
    echo "No new source commit to test: $SOURCE_SHA"
    exit 0
fi

cd "$SOURCE_DIR"
set +e
python "$TEST_SCRIPT" >"$RAW_LOG" 2>&1
TEST_EXIT_CODE=$?
set -e

python - "$RAW_LOG" "$SAFE_LOG" <<'PY'
import os
import sys

source, destination = sys.argv[1:]
text = open(source, "r", encoding="utf-8", errors="replace").read()
for name in (
    "GITHUB_TOKEN",
    "CAMERA_PASSWORD",
    "CAMERA_USERNAME",
    "CAMERA_HOST",
):
    value = os.environ.get(name)
    if value:
        text = text.replace(value, f"<{name}_REDACTED>")
open(destination, "w", encoding="utf-8").write(text)
PY

mkdir -p "$RESULTS_DIR/results"
cp "$SAFE_LOG" "$RESULTS_DIR/results/latest.log"
printf '%s\n' "$SOURCE_SHA" > "$RESULTS_DIR/last-tested-sha.txt"

python - "$RESULTS_DIR/results/latest.json" "$SOURCE_SHA" "$TEST_EXIT_CODE" <<'PY'
import datetime
import json
import sys

path, source_sha, exit_code = sys.argv[1:]
data = {
    "tested_commit": source_sha,
    "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "exit_code": int(exit_code),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
PY

git -C "$RESULTS_DIR" config user.name "icsee-lab-worker"
git -C "$RESULTS_DIR" config user.email "icsee-lab-worker@users.noreply.github.com"
git -C "$RESULTS_DIR" add last-tested-sha.txt results/latest.log results/latest.json
git -C "$RESULTS_DIR" commit -m "Test $SOURCE_SHA (exit $TEST_EXIT_CODE)"
git -C "$RESULTS_DIR" push -u origin "$RESULTS_BRANCH"

echo "Tested $SOURCE_SHA; exit code $TEST_EXIT_CODE; results pushed to $RESULTS_BRANCH"
exit "$TEST_EXIT_CODE"
