import asyncio
import importlib.util
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request
from requests.auth import HTTPDigestAuth

MODULE_PATH = Path(
    os.environ.get("DVRIP_MODULE_PATH", "/opt/icsee_ptz/asyncio_dvrip.py")
)
spec = importlib.util.spec_from_file_location("icsee_asyncio_dvrip", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load DVRIP module from {MODULE_PATH}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
DVRIPCam = module.DVRIPCam

app = Flask(__name__)
camera_lock = threading.Lock()
onvif_lock = threading.Lock()
snapshot_lock = threading.Lock()
log_lock = threading.Lock()

HOST = os.environ["CAMERA_HOST"]
PORT = int(os.environ.get("CAMERA_PORT", "34567"))
ONVIF_PORT = int(os.environ.get("ONVIF_PORT", "8899"))
USERNAME = os.environ["CAMERA_USERNAME"]
PASSWORD = os.environ["CAMERA_PASSWORD"]
PTZ_PULSE_SECONDS = max(0.1, min(float(os.environ.get("PTZ_PULSE_SECONDS", "0.4")), 2.0))
PTZ_SPEED = max(0.1, min(float(os.environ.get("PTZ_SPEED", "0.5")), 1.0))
SNAPSHOT_INTERVAL = max(0.5, float(os.environ.get("SNAPSHOT_INTERVAL", "1.5")))
ONVIF_TIMEOUT = max(2.0, float(os.environ.get("ONVIF_TIMEOUT", "8")))

VELOCITIES = {
    "up": (0.0, PTZ_SPEED, 0.0),
    "down": (0.0, -PTZ_SPEED, 0.0),
    "left": (-PTZ_SPEED, 0.0, 0.0),
    "right": (PTZ_SPEED, 0.0, 0.0),
    "up_left": (-PTZ_SPEED, PTZ_SPEED, 0.0),
    "up_right": (PTZ_SPEED, PTZ_SPEED, 0.0),
    "down_left": (-PTZ_SPEED, -PTZ_SPEED, 0.0),
    "down_right": (PTZ_SPEED, -PTZ_SPEED, 0.0),
    "zoom_in": (0.0, 0.0, PTZ_SPEED),
    "zoom_out": (0.0, 0.0, -PTZ_SPEED),
}

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TPTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
MEDIA_URL = f"http://{HOST}:{ONVIF_PORT}/onvif/media_service"
PTZ_URL = f"http://{HOST}:{ONVIF_PORT}/onvif/ptz_service"
AUTH = HTTPDigestAuth(USERNAME, PASSWORD)

logs = deque(maxlen=300)
latest_snapshot = None
latest_snapshot_sequence = 0
latest_snapshot_time = None
snapshot_error = None
profile_token = None


def add_log(level: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    entry = {"timestamp": timestamp, "level": level, "message": message}
    with log_lock:
        logs.append(entry)
    print(f"[{timestamp}] [{level}] {message}", flush=True)


def run(coro):
    return asyncio.run(coro)


async def with_camera(operation):
    camera = DVRIPCam(HOST, port=PORT, user=USERNAME, password=PASSWORD)
    try:
        logged_in = await camera.login(asyncio.get_running_loop())
        if not logged_in:
            raise RuntimeError("camera login failed")
        return await operation(camera)
    finally:
        camera.close()


def soap_post(url: str, action: str, body: str) -> requests.Response:
    headers = {
        "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
        "Connection": "close",
    }
    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        auth=AUTH,
        timeout=ONVIF_TIMEOUT,
    )
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:500]
        raise RuntimeError(f"ONVIF HTTP {response.status_code}: {detail}")
    if b"Fault" in response.content:
        detail = response.text.strip().replace("\n", " ")[:700]
        raise RuntimeError(f"ONVIF SOAP fault: {detail}")
    return response


def get_profile_token() -> str:
    global profile_token
    if profile_token:
        return profile_token

    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:trt="{TRT_NS}">
  <s:Body><trt:GetProfiles/></s:Body>
</s:Envelope>'''
    response = soap_post(
        MEDIA_URL,
        "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
        body,
    )
    root = ET.fromstring(response.content)
    profiles = root.findall(f".//{{{TRT_NS}}}Profiles")
    if not profiles:
        raise RuntimeError("ONVIF GetProfiles returned no profiles")
    token = profiles[0].attrib.get("token")
    if not token:
        raise RuntimeError("ONVIF profile has no token")
    profile_token = token
    add_log("INFO", f"ONVIF profile selected: {token}")
    return token


def onvif_continuous_move(command: str) -> dict:
    x, y, zoom = VELOCITIES[command]
    token = get_profile_token()
    if command.startswith("zoom_"):
        velocity = f'<tt:Zoom x="{zoom:g}" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"/>'
    else:
        velocity = f'<tt:PanTilt x="{x:g}" y="{y:g}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"/>'

    move_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}">
  <s:Body>
    <tptz:ContinuousMove>
      <tptz:ProfileToken>{token}</tptz:ProfileToken>
      <tptz:Velocity>{velocity}</tptz:Velocity>
    </tptz:ContinuousMove>
  </s:Body>
</s:Envelope>'''
    stop_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}">
  <s:Body>
    <tptz:Stop>
      <tptz:ProfileToken>{token}</tptz:ProfileToken>
      <tptz:PanTilt>true</tptz:PanTilt>
      <tptz:Zoom>true</tptz:Zoom>
    </tptz:Stop>
  </s:Body>
</s:Envelope>'''

    started = soap_post(
        PTZ_URL,
        "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove",
        move_body,
    )
    time.sleep(PTZ_PULSE_SECONDS)
    stopped = soap_post(
        PTZ_URL,
        "http://www.onvif.org/ver20/ptz/wsdl/Stop",
        stop_body,
    )
    return {
        "start_status": started.status_code,
        "stop_status": stopped.status_code,
        "profile_token": token,
        "velocity": {"x": x, "y": y, "zoom": zoom},
    }


def capture_snapshot_once() -> None:
    global latest_snapshot, latest_snapshot_sequence, latest_snapshot_time, snapshot_error

    async def operation(camera):
        return await camera.snapshot(channel=0)

    try:
        with camera_lock:
            jpeg = run(with_camera(operation))
        if not jpeg:
            raise RuntimeError("camera returned no snapshot")
        with snapshot_lock:
            latest_snapshot = bytes(jpeg)
            latest_snapshot_sequence += 1
            latest_snapshot_time = time.time()
            snapshot_error = None
    except Exception as exc:
        with snapshot_lock:
            snapshot_error = f"{type(exc).__name__}: {exc}"
        add_log("ERROR", f"Snapshot capture failed: {type(exc).__name__}: {exc}")


def snapshot_worker() -> None:
    add_log("INFO", f"Snapshot worker started; interval={SNAPSHOT_INTERVAL:g}s")
    while True:
        cycle_started = time.monotonic()
        capture_snapshot_once()
        remaining = SNAPSHOT_INTERVAL - (time.monotonic() - cycle_started)
        if remaining > 0:
            time.sleep(remaining)


def exit_for_restart() -> None:
    add_log("WARNING", "Web service process is exiting for supervisor restart")
    os._exit(0)


@app.get("/")
def index():
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>iCSee PTZ Lab</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:18px}main{max-width:1180px;margin:auto}.panel{background:#1d1d1d;border:1px solid #333;border-radius:12px;padding:14px;margin-bottom:14px}h1,h2{margin:0 0 10px}.small{font-size:13px;color:#aaa;margin:0 0 12px}.camera-row{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:14px;align-items:start}.feed-wrap{position:relative;background:#000;border-radius:8px;overflow:hidden;min-height:260px}.feed-wrap img{display:block;width:100%;max-height:68vh;object-fit:contain;background:#000}.feed-badge{position:absolute;left:8px;bottom:8px;background:#000b;padding:4px 7px;border-radius:5px;font:12px ui-monospace,monospace}.controls{display:flex;flex-direction:column;gap:10px}.grid{display:grid;grid-template-columns:repeat(3,58px);gap:7px;justify-content:center}.zoom,.tools{display:grid;grid-template-columns:1fr 1fr;gap:7px}button{font-size:20px;min-height:48px;border:1px solid #444;border-radius:8px;background:#333;color:#fff;cursor:pointer;padding:5px 9px}button:hover{background:#444}button:active{background:#666}.zoom button,.tools button{font-size:13px;min-height:40px}.tools{grid-template-columns:1fr}.restart{background:#633}.restart:hover{background:#844}.console{height:280px;overflow:auto;background:#080808;border:1px solid #333;border-radius:8px;padding:10px;font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word;color:#c9f7d2}.console .error{color:#ff9d9d}.console .warning{color:#ffd27d}.console .info{color:#c9f7d2}.status{font:12px ui-monospace,monospace;color:#b8f7c5;min-height:34px;white-space:pre-wrap;word-break:break-word}.footer-row{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}.footer-row h2{margin:0}.footer-row button{font-size:12px;min-height:32px}@media(max-width:760px){.camera-row{grid-template-columns:1fr}.controls{max-width:260px;margin:auto;width:100%}.feed-wrap{min-height:190px}}
</style></head><body><main>
<div class="panel"><h1>iCSee PTZ Lab</h1><p class="small">Snapshots use DVRIP. PTZ controls use ONVIF ContinuousMove and Stop.</p>
<div class="camera-row"><div class="feed-wrap"><img id="view" alt="Camera snapshot"><div id="feedBadge" class="feed-badge">Waiting for snapshot…</div></div>
<div class="controls"><div class="grid">
<button data-cmd="up_left">↖</button><button data-cmd="up">▲</button><button data-cmd="up_right">↗</button>
<button data-cmd="left">◀</button><button id="refresh">●</button><button data-cmd="right">▶</button>
<button data-cmd="down_left">↙</button><button data-cmd="down">▼</button><button data-cmd="down_right">↘</button>
</div><div class="zoom"><button data-cmd="zoom_in">Zoom +</button><button data-cmd="zoom_out">Zoom −</button></div>
<div id="status" class="status">Ready</div><div class="tools"><button id="restart" class="restart">Restart web service</button></div></div></div></div>
<div class="panel"><div class="footer-row"><h2>Console</h2><button id="clearConsole">Clear view</button></div><div id="console" class="console">Loading logs…</div></div>
</main><script>
const image=document.getElementById('view');const status=document.getElementById('status');const consoleBox=document.getElementById('console');const badge=document.getElementById('feedBadge');let shownSequence=-1;let clearedBefore=0;let renderedLogCount=-1;
function refreshImage(force=false){fetch('/api/snapshot-status',{cache:'no-store'}).then(r=>r.json()).then(s=>{badge.textContent=s.error?('Snapshot error: '+s.error):(s.sequence?('Snapshot #'+s.sequence+' • '+s.age_seconds.toFixed(1)+'s old'):'Waiting for snapshot…');if(s.sequence&&(force||s.sequence!==shownSequence)){shownSequence=s.sequence;image.src='/snapshot.jpg?sequence='+s.sequence+'&t='+Date.now()}}).catch(e=>{badge.textContent='Status error: '+e})}
async function move(cmd){status.textContent='Sending '+cmd+'…';try{const r=await fetch('/api/ptz',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd})});const j=await r.json();status.textContent=(r.ok?'Completed: ':'Failed: ')+JSON.stringify(j);setTimeout(()=>refreshImage(true),250)}catch(e){status.textContent='Request failed: '+e}finally{loadLogs()}}
async function loadLogs(){try{const r=await fetch('/api/logs',{cache:'no-store'});const j=await r.json();const entries=j.entries.slice(clearedBefore);if(entries.length===renderedLogCount)return;const selection=window.getSelection();if(selection&&!selection.isCollapsed)return;const nearBottom=consoleBox.scrollHeight-consoleBox.scrollTop-consoleBox.clientHeight<24;consoleBox.innerHTML='';for(const e of entries){const line=document.createElement('div');line.className=e.level.toLowerCase();line.textContent=`${e.timestamp} [${e.level}] ${e.message}`;consoleBox.appendChild(line)}renderedLogCount=entries.length;if(nearBottom)consoleBox.scrollTop=consoleBox.scrollHeight}catch(e){consoleBox.textContent='Unable to load logs: '+e}}
async function restartService(){status.textContent='Restart requested. Reconnecting…';try{await fetch('/api/restart',{method:'POST'})}catch(e){}setTimeout(()=>location.reload(),2200)}
document.querySelectorAll('[data-cmd]').forEach(b=>b.onclick=()=>move(b.dataset.cmd));document.getElementById('refresh').onclick=()=>refreshImage(true);document.getElementById('restart').onclick=restartService;document.getElementById('clearConsole').onclick=()=>{fetch('/api/logs',{cache:'no-store'}).then(r=>r.json()).then(j=>{clearedBefore=j.entries.length;renderedLogCount=0;consoleBox.textContent=''})};image.onerror=()=>{status.textContent='Snapshot image failed to load; see console'};refreshImage(true);loadLogs();setInterval(refreshImage,500);setInterval(loadLogs,1000);
</script></body></html>"""


@app.get("/snapshot.jpg")
def snapshot():
    with snapshot_lock:
        jpeg = latest_snapshot
        error = snapshot_error
        sequence = latest_snapshot_sequence
    if jpeg is None:
        return jsonify(error=error or "snapshot is not available yet"), 503
    return Response(
        jpeg,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Snapshot-Sequence": str(sequence),
        },
    )


@app.get("/api/snapshot-status")
def snapshot_status():
    with snapshot_lock:
        sequence = latest_snapshot_sequence
        captured_at = latest_snapshot_time
        error = snapshot_error
    age = None if captured_at is None else max(0.0, time.time() - captured_at)
    return jsonify(sequence=sequence, age_seconds=age, error=error)


@app.post("/api/ptz")
def ptz():
    name = (request.get_json(silent=True) or {}).get("command")
    if name not in VELOCITIES:
        add_log("WARNING", f"Rejected unsupported PTZ command: {name!r}")
        return jsonify(error="unsupported command"), 400

    x, y, zoom = VELOCITIES[name]
    add_log(
        "INFO",
        f"ONVIF PTZ pulse request: command={name} velocity=({x:g},{y:g},{zoom:g}) duration={PTZ_PULSE_SECONDS:g}s",
    )
    started = time.monotonic()
    try:
        with onvif_lock:
            result = onvif_continuous_move(name)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        add_log("INFO", f"ONVIF PTZ pulse response in {elapsed_ms} ms: {result!r}")
        return jsonify(
            command=name,
            backend="onvif",
            pulse_seconds=PTZ_PULSE_SECONDS,
            result=result,
        )
    except Exception as exc:
        app.logger.exception("ONVIF PTZ command failed")
        add_log("ERROR", f"ONVIF PTZ command failed: {type(exc).__name__}: {exc}")
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
    add_log(
        "INFO",
        f"Starting PTZ lab web service; DVRIP={HOST}:{PORT} ONVIF={HOST}:{ONVIF_PORT}",
    )
    threading.Thread(target=snapshot_worker, name="snapshot-worker", daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("WEB_UI_PORT", "8095")), threaded=True)
