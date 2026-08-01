from __future__ import annotations

HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ONVIF Camera Control</title>
  <style>
    :root { color-scheme:dark; --bg:#0b1220; --panel:#111a2b; --panel2:#172236; --border:#2a3952; --text:#edf3ff; --muted:#9aa9bf; --accent:#5ea0ff; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); }
    .shell { max-width:1280px; margin:auto; padding:28px 20px 50px; }
    header { display:flex; justify-content:space-between; gap:18px; align-items:flex-start; margin-bottom:22px; }
    h1,h2,h3 { margin:0; }
    .subtitle,.meta,.hint { color:var(--muted); }
    .selector-wrap { min-width:210px; }
    .selector-wrap label { display:block; margin-bottom:5px; color:var(--muted); font-size:.78rem; text-align:right; }
    select,input,button { border:1px solid var(--border); border-radius:9px; background:var(--panel2); color:var(--text); }
    select,input { padding:8px 10px; }
    .selector-wrap select { width:220px; max-width:42vw; }
    button { padding:8px 11px; cursor:pointer; }
    button:hover:not(:disabled) { border-color:var(--accent); }
    button:disabled { opacity:.35; cursor:not-allowed; }
    .card { border:1px solid var(--border); border-radius:16px; padding:20px; background:var(--panel); }
    .card-head { display:flex; justify-content:space-between; gap:14px; }
    .camera-name { margin:0 0 4px; font-size:1.2rem; }
    .status { border-radius:999px; padding:6px 10px; font-size:.75rem; font-weight:800; background:#173425; color:#a7f3c1; height:max-content; }
    .status.bad { background:#3d1e24; color:#ffb7b7; }
    .status.untested { background:#3b321c; color:#ffe09a; }
    .workspace { display:grid; grid-template-columns:minmax(0,1.7fr) minmax(320px,.8fr); gap:18px; align-items:start; margin-top:18px; }
    .feed-panel,.control-panel { border-top:1px solid var(--border); padding-top:18px; }
    .control-panel { display:flex; flex-direction:column; gap:18px; }
    .control-section + .control-section { border-top:1px solid var(--border); padding-top:18px; }
    .section-head { display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:12px; }
    .controls { display:grid; grid-template-columns:repeat(3,64px); grid-template-rows:repeat(3,52px); justify-content:center; gap:8px; margin:16px 0; }
    .ptz { font-size:1.25rem; font-weight:800; }
    .up{grid-column:2}.left{grid-column:1;grid-row:2}.stop{grid-column:2;grid-row:2}.right{grid-column:3;grid-row:2}.down{grid-column:2;grid-row:3}
    .zoom { display:flex; justify-content:center; gap:10px; }
    .feed-box { display:none; margin-top:12px; border:1px solid var(--border); border-radius:12px; overflow:hidden; background:#05080d; min-height:360px; align-items:center; justify-content:center; }
    .feed-box.enabled { display:flex; }
    .feed-box img { display:block; width:100%; height:auto; max-height:72vh; object-fit:contain; }
    .feed-placeholder { margin-top:12px; min-height:360px; border:1px dashed var(--border); border-radius:12px; display:flex; align-items:center; justify-content:center; color:var(--muted); text-align:center; padding:24px; }
    .toggle { display:flex; gap:8px; align-items:center; color:var(--muted); font-size:.9rem; }
    .preset-row { display:grid; grid-template-columns:minmax(150px,1fr) auto; gap:8px; }
    .preset-actions { display:flex; gap:8px; }
    .preset-edit { display:grid; grid-template-columns:minmax(150px,1fr); gap:8px; margin-top:8px; }
    .caps { display:flex; flex-wrap:wrap; gap:7px; margin-top:18px; }
    .cap { border:1px solid var(--border); border-radius:8px; padding:5px 8px; font-size:.8rem; color:#c5d2e5; }
    .cap.off { opacity:.42; text-decoration:line-through; }
    .message { min-height:1.4em; margin-top:14px; font-size:.88rem; color:var(--muted); }
    .message.ok { color:#a7f3c1; }
    .message.bad,.error { color:#ffc1c1; }
    .error { margin-top:12px; padding:10px; border:1px solid #66303a; border-radius:9px; background:#381d23; word-break:break-word; }
    .empty { color:var(--muted); padding:18px 0; }
    @media(max-width:900px){.workspace{grid-template-columns:1fr}.feed-box,.feed-placeholder{min-height:280px}.control-panel{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.control-section + .control-section{border-top:0;padding-top:0}.control-section.presets{grid-column:1/-1;border-top:1px solid var(--border);padding-top:18px}}
    @media(max-width:620px){header{flex-direction:column}.selector-wrap{width:100%}.selector-wrap label{text-align:left}.selector-wrap select{width:100%;max-width:none}.control-panel{display:flex}.control-section + .control-section{border-top:1px solid var(--border);padding-top:18px}.preset-row,.preset-edit{grid-template-columns:1fr}.preset-actions{display:grid;grid-template-columns:1fr 1fr}.feed-box,.feed-placeholder{min-height:220px}}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div><h1>ONVIF Camera Control</h1><div class="subtitle">Test one configured camera at a time</div></div>
    <div class="selector-wrap">
      <label for="camera-selector">Camera</label>
      <select id="camera-selector" disabled><option>Loading cameras…</option></select>
    </div>
  </header>
  <main id="active-camera" class="empty">Loading cameras…</main>
</div>
<script>
const selector = document.getElementById('camera-selector');
const active = document.getElementById('active-camera');
const messages = new Map();
const presetCache = new Map();
let cameras = [];
let selectedId = localStorage.getItem('multicam-active-camera') || '';
let feedTimer = null;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function capability(name, enabled) {
  return `<span class="cap${enabled ? '' : ' off'}">${esc(name)}</span>`;
}
function currentCamera() { return cameras.find(item => item.id === selectedId); }
function renderSelector() {
  if (!cameras.length) {
    selector.innerHTML = '<option>No configured cameras</option>';
    selector.disabled = true;
    active.className = 'empty';
    active.textContent = 'No configured cameras were returned by the proxy.';
    return;
  }
  selector.innerHTML = cameras.map(camera => `<option value="${esc(camera.id)}">${esc(camera.name)}</option>`).join('');
  if (!cameras.some(camera => camera.id === selectedId)) selectedId = cameras[0].id;
  selector.value = selectedId;
  selector.disabled = false;
}
function presetOptions(camera) {
  const presets = presetCache.get(camera.id) || [];
  if (!presets.length) return '<option value="">Press Refresh to load presets</option>';
  return '<option value="">Select a saved position</option>' + presets.map(p =>
    `<option value="${esc(p.token)}" data-name="${esc(p.name || '')}">${esc(p.name || '(unnamed)')} [${esc(p.token)}]</option>`
  ).join('');
}
function syncPresetName() {
  const select = document.getElementById('preset-selector');
  const input = document.getElementById('preset-name');
  if (!select || !input) return;
  const option = select.options[select.selectedIndex];
  input.value = select.value && option ? (option.dataset.name || '') : '';
}
function updateMessage(id) {
  if (id !== selectedId) return;
  const element = active.querySelector('.message');
  if (!element) return;
  const message = messages.get(id);
  element.className = `message ${message?.ok ? 'ok' : message ? 'bad' : ''}`;
  element.textContent = message ? message.text : '';
}
function updatePresetSelector(camera) {
  const element = document.getElementById('preset-selector');
  if (element && camera.id === selectedId) {
    element.innerHTML = presetOptions(camera);
    syncPresetName();
  }
}
function renderActive() {
  const camera = currentCamera();
  if (!camera) return;
  const state = camera.connection_state || (camera.available === false ? 'unavailable' : 'available');
  const caps = camera.capabilities || {};
  const disabled = camera.available === false || !caps.pan_tilt;
  const message = messages.get(camera.id);
  active.className = '';
  active.innerHTML = `<section class="card">
    <div class="card-head">
      <div><h2 class="camera-name">${esc(camera.name)}</h2><div class="meta">${esc(camera.driver)} · ${esc(camera.host)}<br>${esc(camera.listen || '')}</div></div>
      <span class="status ${state === 'unavailable' ? 'bad' : state === 'untested' ? 'untested' : ''}">${esc(state)}</span>
    </div>

    <div class="workspace">
      <div class="feed-panel">
        <div class="section-head"><h3>Live feed</h3><label class="toggle"><input id="feed-toggle" type="checkbox" ${camera.feed_enabled?'checked':''} ${camera.feed_supported?'':'disabled'}> Enabled</label></div>
        <div class="hint">Snapshot feed is opt-in and disabled by default.</div>
        ${camera.feed_enabled
          ? `<div id="feed-box" class="feed-box enabled"><img id="feed-image" alt="${esc(camera.name)} live snapshot"></div>`
          : `<div class="feed-placeholder">Enable the live feed to view the camera while operating the controls.</div>`}
      </div>

      <div class="control-panel">
        <div class="control-section">
          <h3>Pan / tilt</h3>
          <div class="controls">
            <button type="button" class="ptz up" data-command="move" data-tilt="1" ${disabled?'disabled':''}>↑</button>
            <button type="button" class="ptz left" data-command="move" data-pan="-1" ${disabled?'disabled':''}>←</button>
            <button type="button" class="ptz stop" data-command="stop" ${disabled?'disabled':''}>■</button>
            <button type="button" class="ptz right" data-command="move" data-pan="1" ${disabled?'disabled':''}>→</button>
            <button type="button" class="ptz down" data-command="move" data-tilt="-1" ${disabled?'disabled':''}>↓</button>
          </div>
        </div>

        <div class="control-section">
          <h3>Zoom</h3>
          <div class="zoom">
            <button type="button" data-command="zoom" data-direction="out" ${caps.zoom?'':'disabled'}>Zoom out</button>
            <button type="button" data-command="zoom" data-direction="in" ${caps.zoom?'':'disabled'}>Zoom in</button>
          </div>
        </div>

        <div class="control-section presets">
          <div class="section-head"><h3>Presets</h3><button type="button" data-command="load-presets" ${caps.presets?'':'disabled'}>Load presets</button></div>
          <div class="preset-row">
            <select id="preset-selector">${presetOptions(camera)}</select>
            <div class="preset-actions">
              <button type="button" data-command="goto-preset" ${caps.presets?'':'disabled'}>Go to</button>
              <button type="button" data-command="refresh-presets" ${caps.presets?'':'disabled'}>Refresh</button>
            </div>
          </div>
          <div class="preset-edit">
            <input id="preset-name" maxlength="40" placeholder="Preset name">
            <button type="button" data-command="save-preset" ${caps.presets?'':'disabled'}>Save current position</button>
          </div>
        </div>
      </div>
    </div>

    <div class="caps">${capability('Pan / tilt',!!caps.pan_tilt)}${capability('Zoom',!!caps.zoom)}${capability('Presets',!!caps.presets)}${capability('Live feed',!!camera.feed_supported)}</div>
    <div class="message ${message?.ok ? 'ok' : message ? 'bad' : ''}">${message ? esc(message.text) : ''}</div>
    ${camera.error ? `<div class="error">${esc(camera.error)}</div>` : ''}
  </section>`;
  syncPresetName();
  syncFeedTimer(camera);
}
function syncFeedTimer(camera) {
  if (feedTimer) clearInterval(feedTimer);
  feedTimer = null;
  if (!camera.feed_enabled) return;
  const refreshImage = () => {
    const image = document.getElementById('feed-image');
    if (image) image.src = `/api/cameras/${encodeURIComponent(camera.id)}/snapshot.jpg?_=${Date.now()}`;
  };
  refreshImage();
  feedTimer = setInterval(refreshImage, 1500);
}
async function loadCameras(render=true) {
  const response = await fetch('/api/cameras', {cache:'no-store'});
  if (!response.ok) throw new Error(`Status request failed: HTTP ${response.status}`);
  const data = await response.json();
  if (!Array.isArray(data)) throw new Error('Status response was not a camera list');
  cameras = data;
  renderSelector();
  if (render) renderActive();
}
async function post(command, payload={}) {
  const id = selectedId;
  const response = await fetch(`/api/cameras/${encodeURIComponent(id)}/${command}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), cache:'no-store'
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function loadPresets() {
  const response = await fetch(`/api/cameras/${encodeURIComponent(selectedId)}/presets`, {cache:'no-store'});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  presetCache.set(selectedId, data.presets || []);
  const camera = currentCamera();
  if (camera) updatePresetSelector(camera);
}
async function runAction(button) {
  const id = selectedId;
  const command = button.dataset.command;
  const originalDisabled = button.disabled;
  button.disabled = true;
  messages.set(id, {ok:true, text:'Working…'});
  updateMessage(id);
  try {
    if (command === 'move') await post('move', {pan:Number(button.dataset.pan||0), tilt:Number(button.dataset.tilt||0)});
    else if (command === 'stop') await post('stop');
    else if (command === 'zoom') await post('zoom', {direction:button.dataset.direction});
    else if (command === 'load-presets' || command === 'refresh-presets') await loadPresets();
    else if (command === 'goto-preset') {
      const token = document.getElementById('preset-selector')?.value;
      if (!token) throw new Error('Select a preset');
      await fetch(`/api/cameras/${encodeURIComponent(id)}/presets/${encodeURIComponent(token)}/goto`, {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(async r=>{const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||`HTTP ${r.status}`)});
    } else if (command === 'save-preset') {
      const token = document.getElementById('preset-selector')?.value;
      const name = document.getElementById('preset-name')?.value || '';
      if (!token) throw new Error('Select the preset slot to overwrite');
      await fetch(`/api/cameras/${encodeURIComponent(id)}/presets/${encodeURIComponent(token)}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}).then(async r=>{const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||`HTTP ${r.status}`)});
      await loadPresets();
    }
    messages.set(id, {ok:true, text:`${command} accepted at ${new Date().toLocaleTimeString()}`});
  } catch (error) {
    messages.set(id, {ok:false, text:error.message});
  } finally {
    button.disabled = originalDisabled;
    updateMessage(id);
    await loadCameras(false).catch(() => {});
  }
}
active.addEventListener('click', event => {
  const button = event.target.closest('button[data-command]');
  if (!button || button.disabled) return;
  runAction(button);
});
active.addEventListener('change', async event => {
  if (event.target.id === 'preset-selector') {
    syncPresetName();
    return;
  }
  if (event.target.id !== 'feed-toggle') return;
  try {
    await post('feed', {enabled:event.target.checked});
    messages.set(selectedId, {ok:true, text:`Live feed ${event.target.checked?'enabled':'disabled'}`});
  } catch (error) {
    messages.set(selectedId, {ok:false, text:error.message});
  }
  await loadCameras();
});
selector.addEventListener('change', () => {
  selectedId = selector.value;
  localStorage.setItem('multicam-active-camera', selectedId);
  renderActive();
});
loadCameras().catch(error => {
  selector.innerHTML = '<option>Camera list unavailable</option>';
  selector.disabled = true;
  active.className = 'error';
  active.textContent = error.message;
});
setInterval(() => loadCameras(false).catch(() => {}), 10000);
</script>
</body>
</html>'''
