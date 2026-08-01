#!/usr/bin/env bash

# Run a repeatable runtime diagnostic, write a sanitized report, commit only that
# report, and push it to the current feature branch.
#
# Usage:
#   bash run-test-and-publish.sh tapo-feed-onvif
#
# Optional environment variables:
#   CAMERA_ID=fireplace
#   SAMPLES=12
#   INTERVAL_SECONDS=10

TEST_NAME="${1:-}"
CAMERA_ID="${CAMERA_ID:-fireplace}"
SAMPLES="${SAMPLES:-12}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-10}"
EXPECTED_BRANCH="feature/multi-camera-drivers"
SERVICE="multicam-onvif-proxy"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
RESULT_REL="lab/onvif_proxy/test-results/latest.md"
RESULT_FILE="$REPO_ROOT/$RESULT_REL"
WORK_DIR="$REPO_ROOT/.workspace/test-runs"
RAW_FILE="$WORK_DIR/${TEST_NAME:-unknown}-raw-$(date +%Y%m%d-%H%M%S).log"
CONFIG_FILE="$REPO_ROOT/lab/onvif_proxy/local-config/cameras.yaml"

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    return 1
}

if [ -z "$TEST_NAME" ]; then
    fail "test name is required"
    printf 'Supported tests: tapo-feed-onvif\n' >&2
    return 2 2>/dev/null || exit 2
fi

if [ "$TEST_NAME" != "tapo-feed-onvif" ]; then
    fail "unsupported test: $TEST_NAME"
    printf 'Supported tests: tapo-feed-onvif\n' >&2
    return 2 2>/dev/null || exit 2
fi

if [ -z "$REPO_ROOT" ]; then
    fail "script is not inside a Git worktree"
    return 2 2>/dev/null || exit 2
fi

CURRENT_BRANCH="$(git -C "$REPO_ROOT" branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
    fail "expected branch $EXPECTED_BRANCH, found $CURRENT_BRANCH"
    return 2 2>/dev/null || exit 2
fi

TRACKED_CHANGES="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)"
if [ -n "$TRACKED_CHANGES" ]; then
    fail "tracked worktree changes exist; refusing to mix them with a test-result commit"
    printf '%s\n' "$TRACKED_CHANGES" >&2
    return 2 2>/dev/null || exit 2
fi

git -C "$REPO_ROOT" fetch origin "$EXPECTED_BRANCH"
FETCH_RC=$?
if [ "$FETCH_RC" -ne 0 ]; then
    fail "git fetch failed"
    return 2 2>/dev/null || exit 2
fi

LOCAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
REMOTE_SHA="$(git -C "$REPO_ROOT" rev-parse "origin/$EXPECTED_BRANCH")"
if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
    fail "local branch is not synchronized with origin/$EXPECTED_BRANCH; pull before running"
    printf 'local:  %s\nremote: %s\n' "$LOCAL_SHA" "$REMOTE_SHA" >&2
    return 2 2>/dev/null || exit 2
fi

mkdir -p "$WORK_DIR" "$(dirname "$RESULT_FILE")"

STARTED_AT="$(date --iso-8601=seconds)"
HOSTNAME_VALUE="$(hostname)"
TEST_RC=99

{
    printf 'test=%s\n' "$TEST_NAME"
    printf 'camera=%s\n' "$CAMERA_ID"
    printf 'started_at=%s\n' "$STARTED_AT"
    printf 'host=%s\n' "$HOSTNAME_VALUE"
    printf 'source_commit=%s\n' "$LOCAL_SHA"
    printf 'samples=%s\n' "$SAMPLES"
    printf 'interval_seconds=%s\n' "$INTERVAL_SECONDS"
    printf '\n'

    cd "$SCRIPT_DIR" || return 2 2>/dev/null || exit 2

    docker compose exec -T \
        -e TEST_CAMERA_ID="$CAMERA_ID" \
        -e TEST_SAMPLES="$SAMPLES" \
        -e TEST_INTERVAL_SECONDS="$INTERVAL_SECONDS" \
        "$SERVICE" python - <<'PY'
import json
import os
import time
import urllib.request

from multicam.config import load_config
from multicam.tapo_onvif import TapoOnvifClient

camera_id = os.environ["TEST_CAMERA_ID"]
samples = int(os.environ["TEST_SAMPLES"])
interval = float(os.environ["TEST_INTERVAL_SECONDS"])

config = load_config("/config/cameras.yaml")
camera = next(c for c in config.cameras if c.camera_id == camera_id)
base_url = "http://127.0.0.1:12080"


def api(path: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def camera_state():
    cameras = api("/api/cameras")
    return next(item for item in cameras if item["id"] == camera_id)


def onvif_sample():
    client = TapoOnvifClient(camera)
    results = {}
    for name, action in (
        ("profile", client.profile),
        ("status", client.get_status),
        ("presets", client.get_presets),
    ):
        started = time.monotonic()
        try:
            value = action()
            elapsed = round(time.monotonic() - started, 3)
            results[name] = {
                "ok": True,
                "seconds": elapsed,
                "count": len(value) if name == "presets" else None,
            }
        except Exception as exc:
            results[name] = {
                "ok": False,
                "seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results

initial = camera_state()
initial_enabled = bool(initial.get("feed_enabled"))
print("initial_feed_enabled=", initial_enabled)
print("initial_feed_status=", json.dumps(initial.get("feed_status"), sort_keys=True))

failures = 0
try:
    if not initial_enabled:
        print("enable_feed=", json.dumps(api(f"/api/cameras/{camera_id}/feed", {"enabled": True}), sort_keys=True))
        time.sleep(3)

    for index in range(1, samples + 1):
        state = camera_state()
        result = onvif_sample()
        if not all(item.get("ok") for item in result.values()):
            failures += 1
        print(json.dumps({
            "sample": index,
            "feed_enabled": state.get("feed_enabled"),
            "feed_status": state.get("feed_status"),
            "onvif": result,
        }, sort_keys=True))
        if index < samples:
            time.sleep(interval)
finally:
    if not initial_enabled:
        try:
            print("restore_feed=", json.dumps(api(f"/api/cameras/{camera_id}/feed", {"enabled": False}), sort_keys=True))
        except Exception as exc:
            failures += 1
            print(f"restore_feed_error={type(exc).__name__}: {exc}")

final = camera_state()
print("final_feed_enabled=", final.get("feed_enabled"))
print("final_feed_status=", json.dumps(final.get("feed_status"), sort_keys=True))
print("onvif_failed_samples=", failures)
raise SystemExit(1 if failures else 0)
PY
    TEST_RC=$?

    printf '\n=== container logs (last 5 minutes) ===\n'
    docker compose logs --since 5m "$SERVICE" 2>&1 | tail -n 300
    LOG_RC=$?

    printf '\ntest_exit_code=%s\n' "$TEST_RC"
    printf 'log_capture_exit_code=%s\n' "$LOG_RC"
} >"$RAW_FILE" 2>&1

FINISHED_AT="$(date --iso-8601=seconds)"

python3 - "$RAW_FILE" "$RESULT_FILE" "$CONFIG_FILE" "$TEST_NAME" "$STARTED_AT" "$FINISHED_AT" "$LOCAL_SHA" "$TEST_RC" <<'PY'
from pathlib import Path
import re
import sys

raw_path = Path(sys.argv[1])
result_path = Path(sys.argv[2])
config_path = Path(sys.argv[3])
test_name, started, finished, source_sha, rc = sys.argv[4:9]
text = raw_path.read_text(encoding="utf-8", errors="replace")

text = re.sub(r"(rtsp://)[^/@\s]+:[^/@\s]+@", r"\1<redacted>@", text)
text = re.sub(
    r"(?im)^(\s*(?:username|password|user|pass)\s*[:=]\s*).+$",
    r"\1<redacted>",
    text,
)

try:
    import yaml
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for camera in data.get("cameras", []):
        for key in ("username", "password"):
            value = camera.get(key)
            if isinstance(value, str) and value:
                text = text.replace(value, "<redacted>")
except Exception:
    pass

status = "PASS" if rc == "0" else "FAIL"
report = f"""# Runtime diagnostic result

- **Test:** `{test_name}`
- **Status:** **{status}**
- **Started:** `{started}`
- **Finished:** `{finished}`
- **Source commit:** `{source_sha}`
- **Runner exit code:** `{rc}`

```text
{text.rstrip()}
```
"""
result_path.write_text(report, encoding="utf-8")
PY
SANITIZE_RC=$?
if [ "$SANITIZE_RC" -ne 0 ]; then
    fail "could not create sanitized report; raw output remains under .workspace/test-runs"
    return 3 2>/dev/null || exit 3
fi

git -C "$REPO_ROOT" add -- "$RESULT_REL"
ADD_RC=$?
if [ "$ADD_RC" -ne 0 ]; then
    fail "could not stage result report"
    return 3 2>/dev/null || exit 3
fi

if git -C "$REPO_ROOT" diff --cached --quiet -- "$RESULT_REL"; then
    fail "result report did not change; nothing to commit"
    return 3 2>/dev/null || exit 3
fi

STATUS_WORD="pass"
if [ "$TEST_RC" -ne 0 ]; then
    STATUS_WORD="fail"
fi

git -C "$REPO_ROOT" commit -m "test(results): $TEST_NAME $STATUS_WORD $(date +%Y-%m-%dT%H:%M:%S%z)" -- "$RESULT_REL"
COMMIT_RC=$?
if [ "$COMMIT_RC" -ne 0 ]; then
    fail "could not commit result report"
    return 3 2>/dev/null || exit 3
fi

git -C "$REPO_ROOT" push origin "HEAD:$EXPECTED_BRANCH"
PUSH_RC=$?
if [ "$PUSH_RC" -ne 0 ]; then
    fail "result was committed locally but push failed"
    return 4 2>/dev/null || exit 4
fi

printf 'Published %s result to %s at commit %s\n' \
    "$TEST_NAME" "$RESULT_REL" "$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

return "$TEST_RC" 2>/dev/null || exit "$TEST_RC"
