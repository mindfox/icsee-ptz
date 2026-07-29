import asyncio
import importlib.util
import os
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request

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
lock = threading.Lock()

HOST = os.environ["CAMERA_HOST"]
PORT = int(os.environ.get("CAMERA_PORT", "34567"))
USERNAME = os.environ["CAMERA_USERNAME"]
PASSWORD = os.environ["CAMERA_PASSWORD"]
DEFAULT_STEP = int(os.environ.get("PTZ_STEP", "2"))

COMMANDS = {
    "up": "DirectionUp",
    "down": "DirectionDown",
    "left": "DirectionLeft",
    "right": "DirectionRight",
    "up_left": "DirectionLeftUp",
    "up_right": "DirectionRightUp",
    "down_left": "DirectionLeftDown",
    "down_right": "DirectionRightDown",
    "zoom_in": "ZoomTile",
    "zoom_out": "ZoomWide",
}


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


def exit_for_restart() -> None:
    app.logger.warning("Web service restart requested from UI")
    os._exit(0)


@app.get("/")
def index():
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>iCSee PTZ Lab</title>
<style>
body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:20px}main{max-width:900px;margin:auto}.panel{background:#1d1d1d;border:1px solid #333;border-radius:12px;padding:16px;margin-bottom:16px}img{width:100%;max-height:62vh;object-fit:contain;background:#000;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(3,80px);gap:10px;justify-content:center}.zoom,.tools{display:flex;gap:10px;justify-content:center;margin-top:14px}button{font-size:24px;min-height:64px;border:0;border-radius:10px;background:#333;color:#fff;cursor:pointer;padding:0 18px}button:hover{background:#444}button:active{background:#666}.tools button{font-size:16px;min-height:44px}.restart{background:#633}.restart:hover{background:#844}.status{font-family:ui-monospace,monospace;white-space:pre-wrap;margin-top:12px;color:#b8f7c5}.small{font-size:14px;color:#aaa}
</style></head><body><main>
<div class="panel"><h1>iCSee PTZ Lab</h1><p class="small">Snapshot refreshes every 1.5 seconds. Each button sends one conservative DVRIP PTZ step.</p><img id="view" alt="Camera snapshot"></div>
<div class="panel"><div class="grid">
<button data-cmd="up_left">↖</button><button data-cmd="up">▲</button><button data-cmd="up_right">↗</button>
<button data-cmd="left">◀</button><button id="refresh">●</button><button data-cmd="right">▶</button>
<button data-cmd="down_left">↙</button><button data-cmd="down">▼</button><button data-cmd="down_right">↘</button>
</div><div class="zoom"><button data-cmd="zoom_in">Zoom +</button><button data-cmd="zoom_out">Zoom −</button></div>
<div class="tools"><button id="restart" class="restart">Restart web service</button></div>
<div id="status" class="status">Ready</div></div>
</main><script>
const image=document.getElementById('view');const status=document.getElementById('status');
function refresh(){image.src='/snapshot.jpg?t='+Date.now()}
async function move(cmd){status.textContent='Sending '+cmd+'...';try{const r=await fetch('/api/ptz',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd})});const j=await r.json();status.textContent=JSON.stringify(j,null,2);setTimeout(refresh,400)}catch(e){status.textContent=String(e)}}
async function restartService(){if(!confirm('Restart only the web service process?'))return;status.textContent='Restart requested. Reconnecting...';try{await fetch('/api/restart',{method:'POST'})}catch(e){};setTimeout(()=>location.reload(),2200)}
document.querySelectorAll('[data-cmd]').forEach(b=>b.onclick=()=>move(b.dataset.cmd));document.getElementById('refresh').onclick=refresh;document.getElementById('restart').onclick=restartService;image.onerror=()=>status.textContent='Snapshot failed; check container logs';refresh();setInterval(refresh,1500);
</script></body></html>"""


@app.get("/snapshot.jpg")
def snapshot():
    def operation(camera):
        return camera.snapshot(channel=0)

    try:
        with lock:
            jpeg = run(with_camera(operation))
        if not jpeg:
            return jsonify(error="camera returned no snapshot"), 502
        return Response(bytes(jpeg), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as exc:
        app.logger.exception("snapshot failed")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/ptz")
def ptz():
    payload = request.get_json(silent=True) or {}
    name = payload.get("command")
    if name not in COMMANDS:
        return jsonify(error="unsupported command"), 400
    try:
        step = int(payload.get("step", DEFAULT_STEP))
    except (TypeError, ValueError):
        return jsonify(error="step must be an integer"), 400
    step = max(1, min(step, 8))

    async def operation(camera):
        return await camera.ptz(COMMANDS[name], step=step, ch=0)

    try:
        with lock:
            result = run(with_camera(operation))
        return jsonify(command=name, dvrip_command=COMMANDS[name], step=step, result=result)
    except Exception as exc:
        app.logger.exception("PTZ command failed")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/restart")
def restart():
    threading.Timer(0.25, exit_for_restart).start()
    return jsonify(status="restarting")


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("WEB_UI_PORT", "8095")), threaded=True)
