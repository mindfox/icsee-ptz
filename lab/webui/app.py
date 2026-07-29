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
PRESET_SPEED_MIN = 1
PRESET_SPEED_MAX = 8
PRESET_SPEED_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"

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
        detail = response.text.strip().replace("\n", " ")[:700]
        raise RuntimeError(f"ONVIF HTTP {response.status_code}: {detail}")
    if b"Fault" in response.content:
        detail = response.text.strip().replace("\n", " ")[:900]
        raise RuntimeError(f"ONVIF SOAP fault: {detail}")
    return response


def envelope(content: str, namespaces: str = "") -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" {namespaces}><s:Body>{content}</s:Body></s:Envelope>'''


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_to_data(node: ET.Element):
    children = list(node)
    result = {f"@{local_name(key)}": value for key, value in node.attrib.items()}
    text = (node.text or "").strip()
    if not children:
        if result:
            if text:
                result["#text"] = text
            return result
        return text
    for child in children:
        key = local_name(child.tag)
        value = element_to_data(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    if text:
        result["#text"] = text
    return result


def get_profile_details() -> dict:
    global profile_token
    root = ET.fromstring(
        soap_post(
            MEDIA_URL,
            f"{TRT_NS}/GetProfiles",
            envelope("<trt:GetProfiles/>", f'xmlns:trt="{TRT_NS}"'),
        ).content
    )
    profiles = root.findall(f".//{{{TRT_NS}}}Profiles")
    if not profiles:
        raise RuntimeError("ONVIF GetProfiles returned no profiles")
    selected = profiles[0]
    token = selected.attrib.get("token")
    if not token:
        raise RuntimeError("ONVIF profile has no token")
    profile_token = token
    ptz_config = selected.find(f"{{{TT_NS}}}PTZConfiguration")
    node_token_node = ptz_config.find(f"{{{TT_NS}}}NodeToken") if ptz_config is not None else None
    return {
        "token": token,
        "name": (selected.findtext(f"{{{TT_NS}}}Name") or "").strip(),
        "ptz_configuration_token": ptz_config.attrib.get("token") if ptz_config is not None else None,
        "ptz_node_token": (node_token_node.text or "").strip() if node_token_node is not None else None,
        "raw": element_to_data(selected),
    }


def get_profile_token() -> str:
    global profile_token
    if not profile_token:
        details = get_profile_details()
        add_log("INFO", f"ONVIF profile selected: {details['token']}")
    return profile_token


def query_ptz(action: str, xml: str) -> ET.Element:
    body = envelope(xml, f'xmlns:tptz="{TPTZ_NS}"')
    return ET.fromstring(soap_post(PTZ_URL, f"{TPTZ_NS}/{action}", body).content)


def onvif_diagnostics() -> dict:
    profile = get_profile_details()
    config_token = profile.get("ptz_configuration_token")
    node_token = profile.get("ptz_node_token")
    if not config_token:
        raise RuntimeError("selected media profile has no PTZ configuration token")
    cfg_root = query_ptz("GetConfiguration", f"<tptz:GetConfiguration><tptz:PTZConfigurationToken>{escape(config_token)}</tptz:PTZConfigurationToken></tptz:GetConfiguration>")
    cfg_node = cfg_root.find(f".//{{{TPTZ_NS}}}PTZConfiguration")
    configuration = element_to_data(cfg_node) if cfg_node is not None else element_to_data(cfg_root)
    if not node_token and cfg_node is not None:
        node_el = cfg_node.find(f"{{{TT_NS}}}NodeToken")
        node_token = (node_el.text or "").strip() if node_el is not None else None
    if not node_token:
        raise RuntimeError("PTZ configuration has no node token")
    node_root = query_ptz("GetNode", f"<tptz:GetNode><tptz:NodeToken>{escape(node_token)}</tptz:NodeToken></tptz:GetNode>")
    node_el = node_root.find(f".//{{{TPTZ_NS}}}PTZNode")
    node = element_to_data(node_el) if node_el is not None else element_to_data(node_root)
    options_root = query_ptz("GetConfigurationOptions", f"<tptz:GetConfigurationOptions><tptz:ConfigurationToken>{escape(config_token)}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>")
    options_el = options_root.find(f".//{{{TPTZ_NS}}}PTZConfigurationOptions")
    options = element_to_data(options_el) if options_el is not None else element_to_data(options_root)
    status_root = query_ptz("GetStatus", f"<tptz:GetStatus><tptz:ProfileToken>{escape(profile['token'])}</tptz:ProfileToken></tptz:GetStatus>")
    status_el = status_root.find(f".//{{{TPTZ_NS}}}PTZStatus")
    status = element_to_data(status_el) if status_el is not None else element_to_data(status_root)
    return {"collected_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), "sources": {"profile": "GetProfiles", "configuration": "GetConfiguration", "node": "GetNode", "configuration_options": "GetConfigurationOptions", "status": "GetStatus"}, "profile": profile, "configuration": configuration, "node": node, "configuration_options": options, "status": status}


def onvif_continuous_move(command: str, pulse_seconds: float) -> dict:
    x, y, zoom = VELOCITIES[command]
    token = get_profile_token()
    velocity = f'<tt:Zoom x="{zoom:g}" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"/>' if command.startswith("zoom_") else f'<tt:PanTilt x="{x:g}" y="{y:g}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"/>'
    move_body = envelope(f"<tptz:ContinuousMove><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken><tptz:Velocity>{velocity}</tptz:Velocity></tptz:ContinuousMove>", f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"')
    stop_body = envelope(f"<tptz:Stop><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>", f'xmlns:tptz="{TPTZ_NS}"')
    started = soap_post(PTZ_URL, f"{TPTZ_NS}/ContinuousMove", move_body)
    time.sleep(pulse_seconds)
    stopped = soap_post(PTZ_URL, f"{TPTZ_NS}/Stop", stop_body)
    return {"start_status": started.status_code, "stop_status": stopped.status_code, "profile_token": token, "velocity": {"x": x, "y": y, "zoom": zoom}, "pulse_seconds": pulse_seconds}


def onvif_get_presets() -> list[dict]:
    token = get_profile_token()
    root = query_ptz("GetPresets", f"<tptz:GetPresets><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetPresets>")
    presets = []
    for item in root.findall(f".//{{{TPTZ_NS}}}Preset"):
        preset_token = item.attrib.get("token", "")
        name_node = item.find(f"{{{TT_NS}}}Name")
        preset_name = name_node.text.strip() if name_node is not None and name_node.text else ""
        if preset_token:
            presets.append({"token": preset_token, "name": preset_name})
    return presets


def onvif_set_preset(name: str, preset_token: str) -> dict:
    profile = get_profile_token()
    name_xml = f"<tptz:PresetName>{escape(name)}</tptz:PresetName>" if name else ""
    body = envelope(f"<tptz:SetPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>{name_xml}<tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken></tptz:SetPreset>", f'xmlns:tptz="{TPTZ_NS}"')
    response = soap_post(PTZ_URL, f"{TPTZ_NS}/SetPreset", body)
    root = ET.fromstring(response.content)
    token_node = root.find(f".//{{{TPTZ_NS}}}PresetToken")
    return {"status": response.status_code, "requested_token": preset_token, "preset_token": token_node.text if token_node is not None else None, "name": name}


def onvif_goto_preset(preset_token: str, speed_x: int, speed_y: int) -> dict:
    profile = get_profile_token()
    speed_xml = f'<tptz:Speed><tt:PanTilt x="{speed_x}" y="{speed_y}" space="{PRESET_SPEED_SPACE}"/></tptz:Speed>'
    body = envelope(f"<tptz:GotoPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken><tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken>{speed_xml}</tptz:GotoPreset>", f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"')
    started = time.monotonic()
    response = soap_post(PTZ_URL, f"{TPTZ_NS}/GotoPreset", body)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    return {"status": response.status_code, "preset_token": preset_token, "speed": {"x": speed_x, "y": speed_y, "space": PRESET_SPEED_SPACE}, "elapsed_ms": elapsed_ms}


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
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>iCSee PTZ Lab</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:18px}}main{{max-width:1180px;margin:auto}}.panel{{background:#1d1d1d;border:1px solid #333;border-radius:12px;padding:14px;margin-bottom:14px}}h1,h2{{margin:0 0 10px}}.small{{font-size:13px;color:#aaa;margin:0 0 12px}}.camera-row{{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:14px;align-items:start}}.feed-wrap{{position:relative;background:#000;border-radius:8px;overflow:hidden;min-height:260px}}.feed-wrap img{{display:none;width:100%;max-height:68vh;object-fit:contain}}.feed-badge{{position:absolute;left:8px;bottom:8px;background:#000b;padding:4px 7px;border-radius:5px;font:12px ui-monospace,monospace}}.controls{{display:flex;flex-direction:column;gap:10px}}.grid{{display:grid;grid-template-columns:repeat(3,58px);gap:7px;justify-content:center}}.zoom,.preset-actions,.preset-speed{{display:grid;grid-template-columns:1fr 1fr;gap:7px}}button{{font-size:20px;min-height:48px;border:1px solid #444;border-radius:8px;background:#333;color:#fff;cursor:pointer;padding:5px 9px}}button:hover{{background:#444}}button:disabled{{opacity:.45;cursor:not-allowed}}.zoom button,.restart,.preset-actions button,.feed-toggle,.save-preset,.diag-button,.tab-button{{font-size:13px;min-height:40px}}.step-box,.preset-box{{display:flex;flex-direction:column;gap:7px;background:#272727;border:1px solid #444;border-radius:8px;padding:8px 10px;font-size:13px}}.step-row{{display:flex;align-items:center;justify-content:space-between;gap:10px}}.step-box input{{width:72px;font-size:16px;padding:5px;text-align:center}}.preset-box input,.preset-box select{{width:100%;font-size:13px;padding:7px}}.preset-speed label{{display:flex;flex-direction:column;gap:4px;color:#bbb}}.preset-speed input{{text-align:center;font-size:16px}}.restart{{background:#633}}.tabs{{display:flex;gap:6px;border-bottom:1px solid #444;margin-bottom:10px}}.tab-button{{border-radius:7px 7px 0 0;border-bottom:0;background:#292929}}.tab-button.active{{background:#444}}.tab-panel{{display:none}}.tab-panel.active{{display:block}}.console{{height:300px;overflow:auto;background:#080808;border:1px solid #333;border-radius:8px;padding:10px;font:12px/1.45 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word;color:#c9f7d2}}.console .error{{color:#ff9d9d}}.console .warning{{color:#ffd27d}}.diagnostics{{min-height:300px;max-height:560px;overflow:auto;background:#141414;border:1px solid #333;border-radius:8px;padding:10px}}.diag-toolbar{{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}}.diag-note{{padding:9px 10px;margin-bottom:10px;border:1px solid #60521d;border-radius:7px;background:#302a13;color:#ffe6a6;font-size:13px}}.diag-section{{border:1px solid #3a3a3a;border-radius:8px;margin-bottom:10px;overflow:hidden}}.diag-section h3{{margin:0;padding:9px 10px;background:#292929;font-size:14px}}.diag-provenance{{padding:6px 10px;background:#202020;color:#aaa;font-size:12px;border-top:1px solid #303030}}.diag-grid{{display:grid;grid-template-columns:minmax(180px,34%) 1fr}}.diag-key,.diag-value{{padding:7px 9px;border-top:1px solid #303030;font:12px/1.4 ui-monospace,monospace;word-break:break-word}}.diag-key{{color:#bbb;background:#1a1a1a}}.diag-value.suspicious{{background:#342514;color:#ffd38a}}.diag-warning{{display:block;margin-top:5px;color:#ffca72;font-family:system-ui,sans-serif;font-size:12px}}.diag-empty{{color:#999;padding:20px;text-align:center}}.status{{font:12px ui-monospace,monospace;color:#b8f7c5;min-height:34px;white-space:pre-wrap;word-break:break-word}}.footer-row{{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}}.footer-row h2{{margin:0}}.footer-row button{{font-size:12px;min-height:32px}}@media(max-width:760px){{.camera-row{{grid-template-columns:1fr}}.controls{{max-width:300px;margin:auto;width:100%}}.diag-grid{{grid-template-columns:1fr}}}}</style></head><body><main>
<div class="panel"><h1>iCSee PTZ Lab</h1><p class="small">Feed is disabled by default. PTZ and saved positions use ONVIF.</p><div class="camera-row"><div class="feed-wrap"><img id="view" alt="Camera snapshot"><div id="feedBadge" class="feed-badge">Feed disabled</div></div><div class="controls"><button id="feedToggle" class="feed-toggle">Enable web feed</button><div class="grid"><button data-cmd="up_left">↖</button><button data-cmd="up">▲</button><button data-cmd="up_right">↗</button><button data-cmd="left">◀</button><button id="refresh">●</button><button data-cmd="right">▶</button><button data-cmd="down_left">↙</button><button data-cmd="down">▼</button><button data-cmd="down_right">↘</button></div><div class="zoom"><button data-cmd="zoom_in">Zoom +</button><button data-cmd="zoom_out">Zoom −</button></div><label class="step-box" for="ptzStep"><span class="step-row"><span>Movement step</span><input id="ptzStep" type="number" min="1" max="10" step="1" value="{DEFAULT_PTZ_STEP}"></span><span class="small">Step 1 = {PTZ_STEP_SECONDS:g}s pulse</span></label><div class="preset-box"><strong>Saved positions</strong><span class="small">Refresh manually. Select an existing preset token, optionally edit its name, then save the current position back to that same preset.</span><select id="presetSelect"><option value="">Press Refresh list to load presets</option></select><input id="presetName" maxlength="40" placeholder="Preset name" disabled><button id="savePreset" class="save-preset" disabled>Save current position</button><div class="preset-speed"><label>Speed X<input id="presetSpeedX" type="number" min="1" max="8" step="1" value="1"></label><label>Speed Y<input id="presetSpeedY" type="number" min="1" max="8" step="1" value="1"></label></div><span class="small">Camera-reported preset speed range: 1–8. Change one value at a time.</span><div class="preset-actions"><button id="reloadPresets">Refresh list</button><button id="gotoPreset">Go to selected</button></div></div><div id="status" class="status">Ready</div><button id="restart" class="restart">Restart web service</button></div></div></div>
<div class="panel"><div class="tabs"><button class="tab-button active" data-tab="consoleTab">Console log</button><button class="tab-button" data-tab="diagnosticsTab">Camera diagnostics</button></div><div id="consoleTab" class="tab-panel active"><div class="footer-row"><h2>Console</h2><button id="clearConsole">Clear view</button></div><div id="console" class="console">Loading logs…</div></div><div id="diagnosticsTab" class="tab-panel"><div class="diag-toolbar"><div><h2>Camera diagnostics</h2><div id="diagTime" class="small">Not collected yet</div></div><button id="runDiagnostics" class="diag-button">Run read-only diagnostics</button></div><div class="diag-note">Values below are reported by the camera's ONVIF implementation. A reported value is not automatically verified against physical behavior or another camera subsystem.</div><div id="diagnostics" class="diagnostics"><div class="diag-empty">Run diagnostics to query the camera's PTZ profile, configuration, node, supported spaces, ranges, and current status.</div></div></div></div></main><script>
const image=document.getElementById('view'),status=document.getElementById('status'),consoleBox=document.getElementById('console'),badge=document.getElementById('feedBadge'),feedToggle=document.getElementById('feedToggle'),stepInput=document.getElementById('ptzStep'),presetSelect=document.getElementById('presetSelect'),presetName=document.getElementById('presetName'),savePresetButton=document.getElementById('savePreset'),presetSpeedX=document.getElementById('presetSpeedX'),presetSpeedY=document.getElementById('presetSpeedY'),diagnosticsBox=document.getElementById('diagnostics'),diagTime=document.getElementById('diagTime'),runDiagnosticsButton=document.getElementById('runDiagnostics');let shownSequence=-1,clearedBefore=0,renderedLogCount=-1,feedEnabled=false;const savedStep=localStorage.getItem('ptzStep');if(savedStep)stepInput.value=savedStep;function normalizedStep(){{const value=Math.max(1,Math.min(10,parseInt(stepInput.value||'{DEFAULT_PTZ_STEP}',10)));stepInput.value=value;localStorage.setItem('ptzStep',value);return value}}function normalizedPresetSpeed(input){{const value=Math.max(1,Math.min(8,parseInt(input.value||'1',10)));input.value=value;return value}}stepInput.addEventListener('change',normalizedStep);presetSpeedX.addEventListener('change',()=>normalizedPresetSpeed(presetSpeedX));presetSpeedY.addEventListener('change',()=>normalizedPresetSpeed(presetSpeedY));
function refreshImage(force=false){{if(!feedEnabled)return;fetch('/api/snapshot-status',{{cache:'no-store'}}).then(r=>r.json()).then(s=>{{badge.textContent=s.error?('Snapshot error: '+s.error):(s.sequence?('Snapshot #'+s.sequence+' • '+s.age_seconds.toFixed(1)+'s old'):'Waiting for snapshot…');if(s.sequence&&(force||s.sequence!==shownSequence)){{shownSequence=s.sequence;image.src='/snapshot.jpg?sequence='+s.sequence+'&t='+Date.now()}}}}).catch(e=>badge.textContent='Status error: '+e)}}
async function setFeed(enabled){{try{{const r=await fetch('/api/feed',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{enabled}})}}),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);feedEnabled=j.enabled;feedToggle.textContent=feedEnabled?'Disable web feed':'Enable web feed';image.style.display=feedEnabled?'block':'none';badge.textContent=feedEnabled?'Waiting for snapshot…':'Feed disabled';if(feedEnabled)refreshImage(true);else image.removeAttribute('src');status.textContent='Web feed '+(feedEnabled?'enabled':'disabled')}}catch(e){{status.textContent='Feed toggle failed: '+e}}finally{{loadLogs()}}}}
async function move(cmd){{const step=normalizedStep();status.textContent='Sending '+cmd+' at step '+step+'…';try{{const r=await fetch('/api/ptz',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{command:cmd,step}})}}),j=await r.json();status.textContent=(r.ok?'Completed: ':'Failed: ')+JSON.stringify(j);if(feedEnabled)setTimeout(()=>refreshImage(true),250)}}catch(e){{status.textContent='Request failed: '+e}}finally{{loadLogs()}}}}
function syncPresetSelection(){{const option=presetSelect.options[presetSelect.selectedIndex],selected=Boolean(presetSelect.value);presetName.disabled=!selected;savePresetButton.disabled=!selected;presetName.value=selected?(option.dataset.name||''):''}}
async function loadPresets(){{presetSelect.innerHTML='<option value="">Loading presets…</option>';presetName.value='';presetName.disabled=true;savePresetButton.disabled=true;try{{const r=await fetch('/api/presets',{{cache:'no-store'}}),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);presetSelect.innerHTML='<option value="">Select a saved position</option>';for(const p of j.presets){{const o=document.createElement('option');o.value=p.token;o.dataset.name=p.name||'';o.textContent=(p.name||'(unnamed)')+' ['+p.token+']';presetSelect.appendChild(o)}}if(!j.presets.length)presetSelect.innerHTML='<option value="">No saved positions</option>';status.textContent='Preset list refreshed manually'}}catch(e){{presetSelect.innerHTML='<option value="">Unable to load presets</option>';status.textContent='Preset list failed: '+e}}finally{{loadLogs()}}}}
async function savePreset(){{const token=presetSelect.value,name=presetName.value.trim();if(!token){{status.textContent='Select a saved position first';return}}status.textContent='Saving current position to preset '+token+'…';savePresetButton.disabled=true;try{{const r=await fetch('/api/presets/'+encodeURIComponent(token),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name}})}}),j=await r.json();status.textContent=(r.ok?'Saved: ':'Failed: ')+JSON.stringify(j);if(r.ok){{const option=presetSelect.options[presetSelect.selectedIndex];option.dataset.name=name;option.textContent=(name||'(unnamed)')+' ['+token+']'}}}}catch(e){{status.textContent='Save failed: '+e}}finally{{savePresetButton.disabled=!presetSelect.value;loadLogs()}}}}
async function gotoPreset(){{const token=presetSelect.value;if(!token){{status.textContent='Select a saved position first';return}}const speedX=normalizedPresetSpeed(presetSpeedX),speedY=normalizedPresetSpeed(presetSpeedY);status.textContent='Moving to saved position with speed x='+speedX+', y='+speedY+'…';try{{const r=await fetch('/api/presets/'+encodeURIComponent(token)+'/goto',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{speed_x:speedX,speed_y:speedY}})}}),j=await r.json();status.textContent=(r.ok?'Moving: ':'Failed: ')+JSON.stringify(j)}}catch(e){{status.textContent='Preset move failed: '+e}}finally{{loadLogs()}}}}
function flatten(value,prefix='',rows=[]){{if(value===null||value===undefined||typeof value!=='object'){{rows.push([prefix||'value',value===null?'null':String(value??'')]);return rows}}if(Array.isArray(value)){{value.forEach((v,i)=>flatten(v,prefix+'['+i+']',rows));return rows}}const entries=Object.entries(value);if(!entries.length)rows.push([prefix||'value','{{}}']);else entries.forEach(([k,v])=>flatten(v,prefix?prefix+'.'+k:k,rows));return rows}}
function warningFor(section,key,val){{const s=String(val).trim();if(section==='status'&&key.toLowerCase().endsWith('utctime')&&/^1970-01-01t00:00:00(?:\.0+)?z$/i.test(s))return 'Likely placeholder from PTZ GetStatus; this is not evidence that the camera system clock is wrong.';if(section==='status'&&/^Position\.(PanTilt\.@x|PanTilt\.@y|Zoom\.@x)$/.test(key)&&Number(s)===0)return 'Unverified position value. Repeat diagnostics after moving or zooming and confirm whether it changes.';if(section==='node'&&key.endsWith('MaximumNumberOfPresets'))return 'Declared capacity only. It does not prove all slots are usable or that tokens form a numeric range.';if(section==='node'&&(key.endsWith('HomeSupported')||key.includes('SupportedPTZSpaces')))return 'Declared capability only; functional behavior still requires a controlled test.';return ''}}
function renderDiagnostics(data){{diagnosticsBox.innerHTML='';const sources=data.sources||{{}};for(const [section,value] of Object.entries(data)){{if(section==='collected_at'||section==='sources')continue;const box=document.createElement('section');box.className='diag-section';const title=document.createElement('h3');title.textContent=section.replaceAll('_',' ');box.appendChild(title);if(sources[section]){{const p=document.createElement('div');p.className='diag-provenance';p.textContent='Camera-reported via ONVIF '+sources[section];box.appendChild(p)}}const grid=document.createElement('div');grid.className='diag-grid';for(const [key,val] of flatten(value)){{const k=document.createElement('div'),v=document.createElement('div');k.className='diag-key';v.className='diag-value';k.textContent=key;v.textContent=val;const warning=warningFor(section,key,val);if(warning){{v.classList.add('suspicious');const n=document.createElement('span');n.className='diag-warning';n.textContent='⚠ '+warning;v.appendChild(n)}}grid.append(k,v)}}box.appendChild(grid);diagnosticsBox.appendChild(box)}}diagTime.textContent='Collected '+data.collected_at}}
async function runDiagnostics(){{runDiagnosticsButton.disabled=true;diagnosticsBox.innerHTML='<div class="diag-empty">Querying camera…</div>';status.textContent='Running read-only ONVIF diagnostics…';try{{const r=await fetch('/api/diagnostics',{{method:'POST'}}),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);renderDiagnostics(j);status.textContent='Diagnostics completed'}}catch(e){{diagnosticsBox.innerHTML='<div class="diag-empty"></div>';diagnosticsBox.firstChild.textContent='Diagnostics failed: '+e;status.textContent='Diagnostics failed: '+e}}finally{{runDiagnosticsButton.disabled=false;loadLogs()}}}}
async function loadLogs(){{try{{const r=await fetch('/api/logs',{{cache:'no-store'}}),j=await r.json(),entries=j.entries.slice(clearedBefore);if(entries.length===renderedLogCount)return;const selection=window.getSelection();if(selection&&!selection.isCollapsed)return;const nearBottom=consoleBox.scrollHeight-consoleBox.scrollTop-consoleBox.clientHeight<24;consoleBox.innerHTML='';for(const e of entries){{const line=document.createElement('div');line.className=e.level.toLowerCase();line.textContent=`${{e.timestamp}} [${{e.level}}] ${{e.message}}`;consoleBox.appendChild(line)}}renderedLogCount=entries.length;if(nearBottom)consoleBox.scrollTop=consoleBox.scrollHeight}}catch(e){{consoleBox.textContent='Unable to load logs: '+e}}}}
async function restartService(){{status.textContent='Restart requested. Reconnecting…';try{{await fetch('/api/restart',{{method:'POST'}})}}catch(e){{}}setTimeout(()=>location.reload(),2200)}}
document.querySelectorAll('.tab-button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.tab-button,.tab-panel').forEach(e=>e.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')}});feedToggle.onclick=()=>setFeed(!feedEnabled);document.querySelectorAll('[data-cmd]').forEach(b=>b.onclick=()=>move(b.dataset.cmd));document.getElementById('refresh').onclick=()=>refreshImage(true);presetSelect.onchange=syncPresetSelection;savePresetButton.onclick=savePreset;document.getElementById('reloadPresets').onclick=loadPresets;document.getElementById('gotoPreset').onclick=gotoPreset;runDiagnosticsButton.onclick=runDiagnostics;document.getElementById('restart').onclick=restartService;document.getElementById('clearConsole').onclick=()=>fetch('/api/logs',{{cache:'no-store'}}).then(r=>r.json()).then(j=>{{clearedBefore=j.entries.length;renderedLogCount=0;consoleBox.textContent=''}});image.onerror=()=>status.textContent='Snapshot image failed to load; see console';loadLogs();setInterval(refreshImage,500);setInterval(loadLogs,1000);
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
    try:
        with onvif_lock:
            result = onvif_continuous_move(name, pulse_seconds)
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
            result = onvif_diagnostics()
        add_log("INFO", "Read-only ONVIF PTZ diagnostics completed")
        return jsonify(result)
    except Exception as exc:
        add_log("ERROR", f"ONVIF PTZ diagnostics failed: {type(exc).__name__}: {exc}")
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


@app.post("/api/presets/<preset_token>")
def presets_save(preset_token):
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    if len(name) > 40:
        return jsonify(error="preset name must be at most 40 characters"), 400
    add_log("INFO", f"ONVIF SetPreset request: token={preset_token!r} name={name!r}")
    try:
        with onvif_lock:
            existing = onvif_get_presets()
            if not any(str(item.get("token")) == preset_token for item in existing):
                return jsonify(error="selected preset no longer exists; refresh the list"), 409
            result = onvif_set_preset(name, preset_token)
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
            result = onvif_goto_preset(preset_token, speed_x, speed_y)
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
