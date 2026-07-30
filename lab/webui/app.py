import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from camera import CameraClient
from onvif import OnvifClient, PRESET_SPEED_SPACE

app = Flask(__name__)

HOST = os.environ["CAMERA_HOST"]
PORT = int(os.environ.get("CAMERA_PORT", "34567"))
ONVIF_PORT = int(os.environ.get("ONVIF_PORT", "8899"))
USERNAME = os.environ["CAMERA_USERNAME"]
PASSWORD = os.environ["CAMERA_PASSWORD"]
PTZ_SPEED = max(0.1, min(float(os.environ.get("PTZ_SPEED", "0.5")), 1.0))
PTZ_STEP_SECONDS = max(0.02, min(float(os.environ.get("PTZ_STEP_SECONDS", "0.04")), 0.2))
DEFAULT_PTZ_STEP = max(1, min(int(os.environ.get("PTZ_STEP", "2")), 10))
SNAPSHOT_INTERVAL = max(0.5, float(os.environ.get("SNAPSHOT_INTERVAL", "1.5")))
ONVIF_TIMEOUT = max(2.0, float(os.environ.get("ONVIF_TIMEOUT", "8")))
PRESET_SPEED_MIN = 1
PRESET_SPEED_MAX = 8

VELOCITIES = {
    "up": (0.0, PTZ_SPEED, 0.0), "down": (0.0, -PTZ_SPEED, 0.0),
    "left": (-PTZ_SPEED, 0.0, 0.0), "right": (PTZ_SPEED, 0.0, 0.0),
    "up_left": (-PTZ_SPEED, PTZ_SPEED, 0.0), "up_right": (PTZ_SPEED, PTZ_SPEED, 0.0),
    "down_left": (-PTZ_SPEED, -PTZ_SPEED, 0.0), "down_right": (PTZ_SPEED, -PTZ_SPEED, 0.0),
    "zoom_in": (0.0, 0.0, PTZ_SPEED), "zoom_out": (0.0, 0.0, -PTZ_SPEED),
}

camera_lock = threading.Lock()
onvif_lock = threading.Lock()
snapshot_lock = threading.Lock()
log_lock = threading.Lock()
feed_lock = threading.Lock()

logs = deque(maxlen=300)
latest_snapshot = None
latest_snapshot_sequence = 0
latest_snapshot_time = None
snapshot_error = None
feed_enabled = False


def add_log(level: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    entry = {"timestamp": timestamp, "level": level, "message": message}
    with log_lock:
        logs.append(entry)
    print(f"[{timestamp}] [{level}] {message}", flush=True)


camera = CameraClient(Path(os.environ.get("DVRIP_MODULE_PATH", "/opt/icsee_ptz/asyncio_dvrip.py")), HOST, PORT, USERNAME, PASSWORD)
onvif = OnvifClient(HOST, ONVIF_PORT, USERNAME, PASSWORD, ONVIF_TIMEOUT, add_log)


def capture_snapshot_once() -> None:
    global latest_snapshot, latest_snapshot_sequence, latest_snapshot_time, snapshot_error
    try:
        with camera_lock:
            jpeg = camera.snapshot()
        with snapshot_lock:
            latest_snapshot = jpeg
            latest_snapshot_sequence += 1
            latest_snapshot_time = time.time()
            snapshot_error = None
    except Exception as exc:
        with snapshot_lock:
            snapshot_error = f"{type(exc).__name__}: {exc}"
        add_log("ERROR", f"Snapshot capture failed: {type(exc).__name__}: {exc}")


def snapshot_worker() -> None:
    add_log("INFO", "Snapshot worker started; feed is disabled by default")
    while True:
        with feed_lock:
            enabled = feed_enabled
        if not enabled:
            time.sleep(0.25)
            continue
        started = time.monotonic()
        capture_snapshot_once()
        remaining = SNAPSHOT_INTERVAL - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


def exit_for_restart() -> None:
    add_log("WARNING", "Web service process is exiting for supervisor restart")
    os._exit(0)


@app.get("/")
def index():
    return render_template("index.html", default_ptz_step=DEFAULT_PTZ_STEP, ptz_step_seconds=PTZ_STEP_SECONDS)


@app.get("/snapshot.jpg")
def snapshot():
    with snapshot_lock:
        jpeg, error, sequence = latest_snapshot, snapshot_error, latest_snapshot_sequence
    if jpeg is None:
        return jsonify(error=error or "snapshot is not available yet"), 503
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "X-Snapshot-Sequence": str(sequence)})


@app.get("/api/snapshot-status")
def snapshot_status():
    with feed_lock:
        enabled = feed_enabled
    with snapshot_lock:
        sequence, captured_at, error = latest_snapshot_sequence, latest_snapshot_time, snapshot_error
    age = None if captured_at is None else max(0.0, time.time() - captured_at)
    return jsonify(enabled=enabled, sequence=sequence, age_seconds=age, error=error)


@app.post("/api/feed")
def feed_control():
    global feed_enabled
    enabled = bool((request.get_json(silent=True) or {}).get("enabled", False))
    with feed_lock:
        changed = feed_enabled != enabled
        feed_enabled = enabled
    if changed:
        add_log("WARNING" if enabled else "INFO", f"Web feed {'enabled' if enabled else 'disabled'} from UI")
    return jsonify(enabled=enabled)


@app.post("/api/ptz")
def ptz():
    payload = request.get_json(silent=True) or {}
    name = payload.get("command")
    if name not in VELOCITIES:
        add_log("WARNING", f"Rejected unsupported PTZ command: {name!r}")
        return jsonify(error="unsupported command"), 400
    try:
        step = max(1, min(int(payload.get("step", DEFAULT_PTZ_STEP)), 10))
    except (TypeError, ValueError):
        return jsonify(error="step must be an integer from 1 to 10"), 400
    pulse_seconds = PTZ_STEP_SECONDS * step
    x, y, zoom = VELOCITIES[name]
    add_log("INFO", f"ONVIF PTZ request: command={name} step={step} velocity=({x:g},{y:g},{zoom:g}) duration={pulse_seconds:g}s")
    try:
        with onvif_lock:
            result = onvif.continuous_move(name, pulse_seconds, VELOCITIES[name])
        add_log("INFO", f"ONVIF PTZ response: {result!r}")
        return jsonify(command=name, backend="onvif", step=step, pulse_seconds=pulse_seconds, result=result)
    except Exception as exc:
        add_log("ERROR", f"ONVIF PTZ command failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/diagnostics")
def diagnostics():
    add_log("INFO", "Read-only ONVIF PTZ diagnostics requested manually from UI")
    try:
        with onvif_lock:
            result = onvif.diagnostics()
        add_log("INFO", "Read-only ONVIF PTZ diagnostics completed")
        return jsonify(result)
    except Exception as exc:
        add_log("ERROR", f"ONVIF PTZ diagnostics failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.get("/api/presets")
def presets_list():
    add_log("INFO", "ONVIF GetPresets requested manually from UI")
    try:
        started = time.monotonic()
        with onvif_lock:
            presets = onvif.get_presets()
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        add_log("INFO", f"ONVIF GetPresets response: count={len(presets)} elapsed_ms={elapsed_ms}")
        for preset in presets:
            add_log("INFO", f"ONVIF GetPresets preset: {preset!r}")
        return jsonify(presets=presets, elapsed_ms=elapsed_ms)
    except Exception as exc:
        add_log("ERROR", f"ONVIF GetPresets failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/presets/<preset_token>")
def presets_save(preset_token):
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    if len(name) > 40:
        return jsonify(error="preset name must be at most 40 characters"), 400
    add_log("INFO", f"ONVIF SetPreset request: token={preset_token!r} name={name!r}")
    try:
        with onvif_lock:
            existing = onvif.get_presets()
            if not any(str(item.get("token")) == preset_token for item in existing):
                return jsonify(error="selected preset no longer exists; refresh the list"), 409
            result = onvif.set_preset(name, preset_token)
        add_log("INFO", f"ONVIF SetPreset response: {result!r}")
        return jsonify(result=result)
    except Exception as exc:
        add_log("ERROR", f"ONVIF SetPreset failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/presets/<preset_token>/goto")
def presets_goto(preset_token):
    payload = request.get_json(silent=True) or {}
    try:
        speed_x = int(payload.get("speed_x", PRESET_SPEED_MIN))
        speed_y = int(payload.get("speed_y", PRESET_SPEED_MIN))
    except (TypeError, ValueError):
        return jsonify(error="speed_x and speed_y must be integers from 1 to 8"), 400
    if not (PRESET_SPEED_MIN <= speed_x <= PRESET_SPEED_MAX and PRESET_SPEED_MIN <= speed_y <= PRESET_SPEED_MAX):
        return jsonify(error="speed_x and speed_y must be integers from 1 to 8"), 400
    add_log("INFO", f"ONVIF GotoPreset request: token={preset_token!r} speed_x={speed_x} speed_y={speed_y} speed_space={PRESET_SPEED_SPACE}")
    try:
        with onvif_lock:
            result = onvif.goto_preset(preset_token, speed_x, speed_y)
        add_log("INFO", f"ONVIF GotoPreset response: {result!r}")
        return jsonify(result=result)
    except Exception as exc:
        add_log("ERROR", f"ONVIF GotoPreset failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.get("/api/logs")
def get_logs():
    with log_lock:
        entries = list(logs)
    return jsonify(entries=entries)


@app.post("/api/restart")
def restart():
    add_log("WARNING", "Web service restart requested from UI")
    threading.Timer(0.25, exit_for_restart).start()
    return jsonify(status="restarting")


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    add_log("INFO", f"Starting PTZ lab web service; DVRIP={HOST}:{PORT} ONVIF={HOST}:{ONVIF_PORT}")
    threading.Thread(target=snapshot_worker, name="snapshot-worker", daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("WEB_UI_PORT", "8095")), threaded=True)
