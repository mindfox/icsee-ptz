from __future__ import annotations

HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ONVIF Camera Control</title>
  <style>
    :root { color-scheme:dark; --bg:#0b1220; --panel:#111a2b; --panel2:#172236; --border:#2a3952; --text:#edf3ff; --muted:#9aa9bf; --accent:#5ea0ff; --ok:#43d17d; --bad:#ff6b6b; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); }
    .shell { max-width:1100px; margin:auto; padding:28px 20px 50px; }
    header { display:flex; justify-content:space-between; gap:18px; align-items:flex-start; margin-bottom:22px; }
    h1 { margin:0; font-size:2rem; }
    .subtitle,.meta { color:var(--muted); }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:18px; }
    .card { border:1px solid var(--border); border-radius:16px; padding:20px; background:var(--panel); }
    .card-head { display:flex; justify-content:space-between; gap:14px; }
    .camera-name { margin:0 0 4px; font-size:1.2rem; }
    .status { border-radius:999px; padding:6px 10px; font-size:.75rem; font-weight:800; background:#173425; color:#a7f3c1; height:max-content; }
    .status.bad { background:#3d1e24; color:#ffb7b7; }
    .status.untested { background:#3b321c; color:#ffe09a; }
    .controls { display:grid; grid-template-columns:repeat(3,64px); grid-template-rows:repeat(3,52px); justify-content:center; gap:8px; margin:22px 0 16px; }
    button { border:1px solid var(--border); border-radius:10px; background:var(--panel2); color:var(--text); cursor:pointer; }
    button:hover:not(:disabled) { border-color:var(--accent); }
    button:disabled { opacity:.35; cursor:not-allowed; }
    .ptz { font-size:1.25rem; font-weight:800; }
    .up{grid-column:2}.left{grid-column:1;grid-row:2}.stop{grid-column:2;grid-row:2}.right{grid-column:3;grid-row:2}.down{grid-column:2;grid-row:3}
    .refresh { padding:10px 14px; }
    .caps { display:flex; flex-wrap:wrap; gap:7px; }
    .cap { border:1px solid var(--border); border-radius:8px; padding:5px 8px; font-size:.8rem; color:#c5d2e5; }
    .cap.off { opacity:.42; text-decoration:line-through; }
    .message { min-height:1.4em; margin-top:14px; font-size:.88rem; color:var(--muted); }
    .message.ok { color:#a7f3c1; }
    .message.bad,.error { color:#ffc1c1; }
    .error { margin-top:12px; padding:10px; border:1px solid #66303a; border-radius:9px; background:#381d23; word-break:break-word; }
    @media(max-width:620px){header{flex-direction:column}.refresh{width:100%}.grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div><h1>ONVIF Camera Control</h1><div class="subtitle">Multi-camera PTZ proxy</div></div>
    <button id="refresh" class="refresh" type="button">Refresh status</button>
  </header>
  <main id="grid" class="grid">Loading cameras…</main>
</div>
<script>
const grid = document.getElementById('grid');
const refresh = document.getElementById('refresh');
const messages = new Map();

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function capability(name, enabled) {
  return `<span class="cap${enabled ? '' : ' off'}">${esc(name)}</span>`;
}

function card(camera) {
  const state = camera.connection_state || (camera.available === false ? 'unavailable' : 'available');
  const usable = camera.available !== false;
  const caps = camera.capabilities || {};
  const disabled = !usable || !caps.pan_tilt;
  const message = messages.get(camera.id);
  return `<section class="card" data-camera-card="${esc(camera.id)}">
    <div class="card-head">
      <div><h2 class="camera-name">${esc(camera.name)}</h2><div class="meta">${esc(camera.driver)} · ${esc(camera.host)}<br>${esc(camera.listen || '')}</div></div>
      <span class="status ${state === 'unavailable' ? 'bad' : state === 'untested' ? 'untested' : ''}">${esc(state)}</span>
    </div>
    <div class="controls">
      <button type="button" class="ptz up" data-camera="${esc(camera.id)}" data-command="move" data-tilt="1" ${disabled?'disabled':''}>↑</button>
      <button type="button" class="ptz left" data-camera="${esc(camera.id)}" data-command="move" data-pan="-1" ${disabled?'disabled':''}>←</button>
      <button type="button" class="ptz stop" data-camera="${esc(camera.id)}" data-command="stop" ${disabled?'disabled':''}>■</button>
      <button type="button" class="ptz right" data-camera="${esc(camera.id)}" data-command="move" data-pan="1" ${disabled?'disabled':''}>→</button>
      <button type="button" class="ptz down" data-camera="${esc(camera.id)}" data-command="move" data-tilt="-1" ${disabled?'disabled':''}>↓</button>
    </div>
    <div class="caps">${capability('Pan / tilt',!!caps.pan_tilt)}${capability('Zoom',!!caps.zoom)}${capability('Presets',!!caps.presets)}${capability('Audio',!!caps.audio)}</div>
    <div class="message ${message?.ok ? 'ok' : message ? 'bad' : ''}">${message ? esc(message.text) : ''}</div>
    ${camera.error ? `<div class="error">${esc(camera.error)}</div>` : ''}
  </section>`;
}

async function loadCameras() {
  const response = await fetch(`/api/cameras?_=${Date.now()}`, {cache:'no-store'});
  if (!response.ok) throw new Error(`Status request failed: HTTP ${response.status}`);
  const cameras = await response.json();
  grid.innerHTML = cameras.length ? cameras.map(card).join('') : 'No cameras configured.';
}

async function sendCommand(button) {
  const id = button.dataset.camera;
  const command = button.dataset.command;
  const payload = {};
  if (button.dataset.pan) payload.pan = Number(button.dataset.pan);
  if (button.dataset.tilt) payload.tilt = Number(button.dataset.tilt);
  button.disabled = true;
  messages.set(id, {ok:true, text:`Sending ${command}…`});
  await loadCameras();
  try {
    const response = await fetch(`/api/cameras/${encodeURIComponent(id)}/${command}`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), cache:'no-store'
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    messages.set(id, {ok:true, text:`${command} accepted at ${new Date().toLocaleTimeString()}`});
  } catch (error) {
    messages.set(id, {ok:false, text:error.message});
  }
  await loadCameras();
}

grid.addEventListener('click', event => {
  const button = event.target.closest('button[data-camera][data-command]');
  if (!button || button.disabled) return;
  sendCommand(button);
});
refresh.addEventListener('click', () => loadCameras().catch(error => { grid.textContent = error.message; }));
loadCameras().catch(error => { grid.textContent = error.message; });
setInterval(() => loadCameras().catch(() => {}), 10000);
</script>
</body>
</html>'''
