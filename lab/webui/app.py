import asyncio
import importlib.util
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from flask import Flask, Response, jsonify, request
from requests.auth import HTTPDigestAuth

MODULE_PATH = Path(os.environ.get("DVRIP_MODULE_PATH", "/opt/icsee_ptz/asyncio_dvrip.py"))
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
feed_lock = threading.Lock()

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
PRESET_SLOT_MIN = 1
PRESET_SLOT_MAX = 255

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
feed_enabled = False


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
        if not await camera.login(asyncio.get_running_loop()):
            raise RuntimeError("camera login failed")
        return await operation(camera)
    finally:
        camera.close()


def soap_post(url: str, action: str, body: str) -> requests.Response:
    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
            "Connection": "close",
        },
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
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:trt="{TRT_NS}"><s:Body><trt:GetProfiles/></s:Body></s:Envelope>'''
    response = soap_post(MEDIA_URL, "http://www.onvif.org/ver10/media/wsdl/GetProfiles", body)
    root = ET.fromstring(response.content)
    profiles = root.findall(f".//{{{TRT_NS}}}Profiles")
    if not profiles or not profiles[0].attrib.get("token"):
        raise RuntimeError("ONVIF GetProfiles returned no usable profile")
    profile_token = profiles[0].attrib["token"]
    add_log("INFO", f"ONVIF profile selected: {profile_token}")
    return profile_token


def onvif_continuous_move(command: str, pulse_seconds: float) -> dict:
    x, y, zoom = VELOCITIES[command]
    token = get_profile_token()
    if command.startswith("zoom_"):
        velocity = f'<tt:Zoom x="{zoom:g}" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"/>'
    else:
        velocity = f'<tt:PanTilt x="{x:g}" y="{y:g}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"/>'
    move_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"><s:Body><tptz:ContinuousMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Velocity>{velocity}</tptz:Velocity></tptz:ContinuousMove></s:Body></s:Envelope>'''
    stop_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}"><s:Body><tptz:Stop><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop></s:Body></s:Envelope>'''
    started = soap_post(PTZ_URL, "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove", move_body)
    time.sleep(pulse_seconds)
    stopped = soap_post(PTZ_URL, "http://www.onvif.org/ver20/ptz/wsdl/Stop", stop_body)
    return {
        "start_status": started.status_code,
        "stop_status": stopped.status_code,
        "profile_token": token,
        "velocity": {"x": x, "y": y, "zoom": zoom},
        "pulse_seconds": pulse_seconds,
    }


def onvif_get_presets() -> list[dict]:
    token = get_profile_token()
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}"><s:Body><tptz:GetPresets><tptz:ProfileToken>{token}</tptz:ProfileToken></tptz:GetPresets></s:Body></s:Envelope>'''
    response = soap_post(PTZ_URL, "http://www.onvif.org/ver20/ptz/wsdl/GetPresets", body)
    root = ET.fromstring(response.content)
    presets = []
    for item in root.findall(f".//{{{TPTZ_NS}}}Preset"):
        preset_token = item.attrib.get("token", "")
        name_node = item.find(f"{{{TT_NS}}}Name")
        preset_name = name_node.text if name_node is not None and name_node.text else preset_token
        if preset_token:
            presets.append({"token": preset_token, "name": preset_name})
    return presets


def onvif_set_preset(name: str, preset_token: str) -> dict:
    profile = get_profile_token()
    safe_name = escape(name)
    safe_token = escape(preset_token)
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}"><s:Body><tptz:SetPreset><tptz:ProfileToken>{profile}</tptz:ProfileToken><tptz:PresetName>{safe_name}</tptz:PresetName><tptz:PresetToken>{safe_token}</tptz:PresetToken></tptz:SetPreset></s:Body></s:Envelope>'''
    response = soap_post(PTZ_URL, "http://www.onvif.org/ver20/ptz/wsdl/SetPreset", body)
    root = ET.fromstring(response.content)
    token_node = root.find(f".//{{{TPTZ_NS}}}PresetToken")
    returned_token = token_node.text if token_node is not None else None
    return {
        "status": response.status_code,
        "requested_token": preset_token,
        "preset_token": returned_token,
        "name": name,
    }


def onvif_goto_preset(preset_token: str) -> dict:
    profile = get_profile_token()
    safe_token = escape(preset_token)
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tptz="{TPTZ_NS}"><s:Body><tptz:GotoPreset><tptz:ProfileToken>{profile}</tptz:ProfileToken><tptz:PresetToken>{safe_token}</tptz:PresetToken></tptz:GotoPreset></s:Body></s:Envelope>'''
    response = soap_post(PTZ_URL, "http://www.onvif.org/ver20/ptz/wsdl/GotoPreset", body)
    return {"status": response.status_code, "preset_token": preset_token}


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
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>iCSee PTZ Lab</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:18px}}main{{max-width:1180px;margin:auto}}.panel{{background:#1d1d1d;border:1px solid #333;border-radius:12px;padding:14px;margin-bottom:14px}}h1,h2{{margin:0 0 10px}}.small{{font-size:13px;color:#aaa;margin:0 0 12px}}.camera-row{{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:14px;align-items:start}}.feed-wrap{{position:relative;background:#000;border-radius:8px;overflow:hidden;min-height:260px}}.feed-wrap img{{display:none;width:100%;max-height:68vh;object-fit:contain;background:#000}}.feed-badge{{position:absolute;left:8px;bottom:8px;background:#000b;padding:4px 7px;border-radius:5px;font:12px ui-monospace,monospace}}.controls{{display:flex;flex-direction:column;gap:10px}}.grid{{display:grid;grid-template-columns:repeat(3,58px);gap:7px;justify-content:center}}.zoom,.preset-actions{{display:grid;grid-template-columns:1fr 1fr;gap:7px}}button{{font-size:20px;min-height:48px;border:1px solid #444;border-radius:8px;background:#333;color:#fff;cursor:pointer;padding:5px 9px}}button:hover{{background:#444}}button:active{{background:#666}}button:disabled{{opacity:.45;cursor:not-allowed}}.zoom button,.restart,.preset-actions button,.feed-toggle,.save-preset{{font-size:13px;min-height:40px}}.step-box,.preset-box{{display:flex;flex-direction:column;gap:7px;background:#272727;border:1px solid #444;border-radius:8px;padding:8px 10px;font-size:13px}}.step-row{{display:flex;align-items:center;justify-content:space-between;gap:10px}}.step-box input{{width:72px;font-size:16px;padding:5px 4px;text-align:center}}.preset-box input,.preset-box select{{width:100%;font-size:13px;padding:7px}}.restart{{background:#633}}.restart:hover{{background:#844}}.console{{height:280px;overflow:auto;background:#080808;border:1px solid #333;border-radius:8px;padding:10px;font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word;color:#c9f7d2}}.console .error{{color:#ff9d9d}}.console .warning{{color:#ffd27d}}.status{{font:12px ui-monospace,monospace;color:#b8f7c5;min-height:34px;white-space:pre-wrap;word-break:break-word}}.footer-row{{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}}.footer-row h2{{margin:0}}.footer-row button{{font-size:12px;min-height:32px}}@media(max-width:760px){{.camera-row{{grid-template-columns:1fr}}.controls{{max-width:300px;margin:auto;width:100%}}.feed-wrap{{min-height:190px}}}}
</style></head><body><main>
<div class="panel"><h1>iCSee PTZ Lab</h1><p class="small">Feed is disabled by default. PTZ and saved positions use ONVIF.</p>
<div class="camera-row"><div class="feed-wrap"><img id="view" alt="Camera snapshot"><div id="feedBadge" class="feed-badge">Feed disabled</div></div>
<div class="controls"><button id="feedToggle" class="feed-toggle">Enable web feed</button><div class="grid"><button data-cmd="up_left">↖</button><button data-cmd="up">▲</button><button data-cmd="up_right">↗</button><button data-cmd="left">◀</button><button id="refresh">●</button><button data-cmd="right">▶</button><button data-cmd="down_left">↙</button><button data-cmd="down">▼</button><button data-cmd="down_right">↘</button></div>
<div class="zoom"><button data-cmd="zoom_in">Zoom +</button><button data-cmd="zoom_out">Zoom −</button></div>
<label class="step-box" for="ptzStep"><span class="step-row"><span>Movement step</span><input id="ptzStep" type="number" min="1" max="10" step="1" value="{DEFAULT_PTZ_STEP}" title="Use Up/Down keys or the spinner arrows"></span><span class="small">Step 1 = {PTZ_STEP_SECONDS:g}s pulse</span></label>
<div class="preset-box"><strong>Saved positions</strong><span class="small">Press Refresh list first. Saving requires an unused explicit slot and will not silently overwrite preset 1.</span><input id="presetName" maxlength="40" placeholder="Position name"><select id="newPresetSlot" disabled><option value="">Refresh list to choose a free slot</option></select><button id="savePreset" class="save-preset" disabled>Save current position</button><select id="presetSelect"><option value="">Press Refresh list to load presets</option></select><div class="preset-actions"><button id="reloadPresets">Refresh list</button><button id="gotoPreset">Go to selected</button></div></div>
<div id="status" class="status">Ready</div><button id="restart" class="restart">Restart web service</button></div></div></div>
<div class="panel"><div class="footer-row"><h2>Console</h2><button id="clearConsole">Clear view</button></div><div id="console" class="console">Loading logs…</div></div>
</main><script>
const image=document.getElementById('view'),status=document.getElementById('status'),consoleBox=document.getElementById('console'),badge=document.getElementById('feedBadge'),feedToggle=document.getElementById('feedToggle'),stepInput=document.getElementById('ptzStep'),presetName=document.getElementById('presetName'),presetSelect=document.getElementById('presetSelect'),newPresetSlot=document.getElementById('newPresetSlot'),savePresetButton=document.getElementById('savePreset');let shownSequence=-1,clearedBefore=0,renderedLogCount=-1,feedEnabled=false,presetsLoaded=false;const savedStep=localStorage.getItem('ptzStep');if(savedStep)stepInput.value=savedStep;function normalizedStep(){{const value=Math.max(1,Math.min(10,parseInt(stepInput.value||'{DEFAULT_PTZ_STEP}',10)));stepInput.value=value;localStorage.setItem('ptzStep',value);return value}}stepInput.addEventListener('change',normalizedStep);stepInput.addEventListener('input',()=>{{if(stepInput.value!=='')localStorage.setItem('ptzStep',stepInput.value)}});
function refreshImage(force=false){{if(!feedEnabled)return;fetch('/api/snapshot-status',{{cache:'no-store'}}).then(r=>r.json()).then(s=>{{badge.textContent=s.error?('Snapshot error: '+s.error):(s.sequence?('Snapshot #'+s.sequence+' • '+s.age_seconds.toFixed(1)+'s old'):'Waiting for snapshot…');if(s.sequence&&(force||s.sequence!==shownSequence)){{shownSequence=s.sequence;image.src='/snapshot.jpg?sequence='+s.sequence+'&t='+Date.now()}}}}).catch(e=>badge.textContent='Status error: '+e)}}
async function setFeed(enabled){{try{{const r=await fetch('/api/feed',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{enabled}})}}),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);feedEnabled=j.enabled;feedToggle.textContent=feedEnabled?'Disable web feed':'Enable web feed';image.style.display=feedEnabled?'block':'none';badge.textContent=feedEnabled?'Waiting for snapshot…':'Feed disabled';if(feedEnabled)refreshImage(true);else image.removeAttribute('src');status.textContent='Web feed '+(feedEnabled?'enabled':'disabled')}}catch(e){{status.textContent='Feed toggle failed: '+e}}finally{{loadLogs()}}}}
async function move(cmd){{const step=normalizedStep();status.textContent='Sending '+cmd+' at step '+step+'…';try{{const r=await fetch('/api/ptz',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{command:cmd,step}})}}),j=await r.json();status.textContent=(r.ok?'Completed: ':'Failed: ')+JSON.stringify(j);if(feedEnabled)setTimeout(()=>refreshImage(true),250)}}catch(e){{status.textContent='Request failed: '+e}}finally{{loadLogs()}}}}
function populateFreeSlots(presets){{const used=new Set(presets.map(p=>String(p.token)));newPresetSlot.innerHTML='<option value="">Select a free preset slot</option>';let freeCount=0;for(let slot={PRESET_SLOT_MIN};slot<={PRESET_SLOT_MAX};slot++){{const token=String(slot);if(!used.has(token)){{const option=document.createElement('option');option.value=token;option.textContent='Preset slot '+token;newPresetSlot.appendChild(option);freeCount++}}}}newPresetSlot.disabled=freeCount===0;savePresetButton.disabled=freeCount===0;}}
async function loadPresets(){{presetSelect.innerHTML='<option value="">Loading presets…</option>';newPresetSlot.innerHTML='<option value="">Loading free slots…</option>';newPresetSlot.disabled=true;savePresetButton.disabled=true;try{{const r=await fetch('/api/presets',{{cache:'no-store'}}),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);presetSelect.innerHTML='<option value="">Select a saved position</option>';for(const p of j.presets){{const o=document.createElement('option');o.value=p.token;o.textContent=p.name+' ['+p.token+']';presetSelect.appendChild(o)}}if(!j.presets.length)presetSelect.innerHTML='<option value="">No saved positions</option>';populateFreeSlots(j.presets);presetsLoaded=true;status.textContent='Preset list refreshed manually'}}catch(e){{presetSelect.innerHTML='<option value="">Unable to load presets</option>';newPresetSlot.innerHTML='<option value="">Refresh failed</option>';status.textContent='Preset list failed: '+e}}finally{{loadLogs()}}}}
async function savePreset(){{const name=presetName.value.trim(),token=newPresetSlot.value;if(!presetsLoaded){{status.textContent='Refresh the preset list first';return}}if(!name){{status.textContent='Enter a position name first';presetName.focus();return}}if(!token){{status.textContent='Select a free preset slot first';newPresetSlot.focus();return}}status.textContent='Saving current position to preset slot '+token+'…';savePresetButton.disabled=true;try{{const r=await fetch('/api/presets',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name,token}})}}),j=await r.json();status.textContent=(r.ok?'Saved: ':'Failed: ')+JSON.stringify(j);if(r.ok){{presetName.value='';presetsLoaded=false;newPresetSlot.innerHTML='<option value="">Refresh list to verify and choose another free slot</option>';newPresetSlot.disabled=true}}}}catch(e){{status.textContent='Save failed: '+e}}finally{{savePresetButton.disabled=!presetsLoaded;loadLogs()}}}}
async function gotoPreset(){{const token=presetSelect.value;if(!token){{status.textContent='Select a saved position first';return}}status.textContent='Moving to saved position…';try{{const r=await fetch('/api/presets/'+encodeURIComponent(token)+'/goto',{{method:'POST'}}),j=await r.json();status.textContent=(r.ok?'Moving: ':'Failed: ')+JSON.stringify(j)}}catch(e){{status.textContent='Preset move failed: '+e}}finally{{loadLogs()}}}}
async function loadLogs(){{try{{const r=await fetch('/api/logs',{{cache:'no-store'}}),j=await r.json(),entries=j.entries.slice(clearedBefore);if(entries.length===renderedLogCount)return;const selection=window.getSelection();if(selection&&!selection.isCollapsed)return;const nearBottom=consoleBox.scrollHeight-consoleBox.scrollTop-consoleBox.clientHeight<24;consoleBox.innerHTML='';for(const e of entries){{const line=document.createElement('div');line.className=e.level.toLowerCase();line.textContent=`${{e.timestamp}} [${{e.level}}] ${{e.message}}`;consoleBox.appendChild(line)}}renderedLogCount=entries.length;if(nearBottom)consoleBox.scrollTop=consoleBox.scrollHeight}}catch(e){{consoleBox.textContent='Unable to load logs: '+e}}}}
async function restartService(){{status.textContent='Restart requested. Reconnecting…';try{{await fetch('/api/restart',{{method:'POST'}})}}catch(e){{}}setTimeout(()=>location.reload(),2200)}}
feedToggle.onclick=()=>setFeed(!feedEnabled);document.querySelectorAll('[data-cmd]').forEach(b=>b.onclick=()=>move(b.dataset.cmd));document.getElementById('refresh').onclick=()=>refreshImage(true);savePresetButton.onclick=savePreset;document.getElementById('reloadPresets').onclick=loadPresets;document.getElementById('gotoPreset').onclick=gotoPreset;document.getElementById('restart').onclick=restartService;document.getElementById('clearConsole').onclick=()=>fetch('/api/logs',{{cache:'no-store'}}).then(r=>r.json()).then(j=>{{clearedBefore=j.entries.length;renderedLogCount=0;consoleBox.textContent=''}});image.onerror=()=>status.textContent='Snapshot image failed to load; see console';loadLogs();setInterval(refreshImage,500);setInterval(loadLogs,1000);
</script></body></html>'''


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
    started = time.monotonic()
    try:
        with onvif_lock:
            result = onvif_continuous_move(name, pulse_seconds)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        add_log("INFO", f"ONVIF PTZ response in {elapsed_ms} ms: {result!r}")
        return jsonify(command=name, backend="onvif", step=step, pulse_seconds=pulse_seconds, result=result)
    except Exception as exc:
        app.logger.exception("ONVIF PTZ command failed")
        add_log("ERROR", f"ONVIF PTZ command failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.get("/api/presets")
def presets_list():
    add_log("INFO", "ONVIF GetPresets requested manually from UI")
    try:
        with onvif_lock:
            presets = onvif_get_presets()
        return jsonify(presets=presets)
    except Exception as exc:
        add_log("ERROR", f"ONVIF GetPresets failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/presets")
def presets_save():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    preset_token = str(payload.get("token", "")).strip()
    if not name:
        return jsonify(error="preset name is required"), 400
    if len(name) > 40:
        return jsonify(error="preset name must be at most 40 characters"), 400
    if not preset_token.isdigit():
        return jsonify(error="an explicit numeric preset slot is required"), 400
    slot = int(preset_token)
    if slot < PRESET_SLOT_MIN or slot > PRESET_SLOT_MAX:
        return jsonify(error=f"preset slot must be from {PRESET_SLOT_MIN} to {PRESET_SLOT_MAX}"), 400
    add_log("INFO", f"ONVIF SetPreset request: name={name!r} token={preset_token!r}")
    try:
        with onvif_lock:
            existing = onvif_get_presets()
            if any(str(item.get("token")) == preset_token for item in existing):
                add_log("WARNING", f"Rejected SetPreset because token {preset_token!r} already exists")
                return jsonify(error="selected preset slot already exists; refresh the list and choose a free slot"), 409
            result = onvif_set_preset(name, preset_token)
        add_log("INFO", f"ONVIF SetPreset response: {result!r}")
        return jsonify(result=result)
    except Exception as exc:
        add_log("ERROR", f"ONVIF SetPreset failed: {type(exc).__name__}: {exc}")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502


@app.post("/api/presets/<preset_token>/goto")
def presets_goto(preset_token):
    add_log("INFO", f"ONVIF GotoPreset request: token={preset_token!r}")
    try:
        with onvif_lock:
            result = onvif_goto_preset(preset_token)
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
