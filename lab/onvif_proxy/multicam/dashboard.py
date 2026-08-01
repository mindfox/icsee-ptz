from __future__ import annotations

HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ONVIF Camera Control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1220;
      --panel: #111a2b;
      --panel-2: #172236;
      --border: #2a3952;
      --text: #edf3ff;
      --muted: #9aa9bf;
      --accent: #5ea0ff;
      --ok: #43d17d;
      --bad: #ff6b6b;
      --warn: #f4c15d;
      --shadow: 0 18px 45px rgba(0,0,0,.28);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at top right, #142744 0, var(--bg) 38%);
      color: var(--text);
    }
    .shell { max-width: 1180px; margin: 0 auto; padding: 32px 22px 56px; }
    header { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-bottom:24px; }
    h1 { margin:0; font-size:clamp(1.65rem, 4vw, 2.45rem); letter-spacing:-.03em; }
    .subtitle { margin:.45rem 0 0; color:var(--muted); }
    .refresh {
      border:1px solid var(--border); background:var(--panel); color:var(--text);
      border-radius:10px; padding:.72rem 1rem; cursor:pointer; font-weight:650;
    }
    .refresh:hover { border-color:var(--accent); }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:18px; }
    .card {
      background:linear-gradient(180deg,var(--panel),#0f1727);
      border:1px solid var(--border); border-radius:16px; padding:20px;
      box-shadow:var(--shadow);
    }
    .card-head { display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }
    .camera-name { margin:0; font-size:1.25rem; }
    .meta { color:var(--muted); font-size:.9rem; margin-top:.32rem; line-height:1.45; }
    .status {
      display:inline-flex; align-items:center; gap:.45rem; border-radius:999px;
      padding:.38rem .68rem; font-size:.78rem; font-weight:800; text-transform:uppercase;
      letter-spacing:.04em; background:#173425; color:#a7f3c1; white-space:nowrap;
    }
    .status.bad { background:#3d1e24; color:#ffb7b7; }
    .dot { width:.5rem; height:.5rem; border-radius:50%; background:var(--ok); box-shadow:0 0 0 4px rgba(67,209,125,.12); }
    .bad .dot { background:var(--bad); box-shadow:0 0 0 4px rgba(255,107,107,.12); }
    .controls { display:grid; grid-template-columns:repeat(3,62px); grid-template-rows:repeat(3,52px); justify-content:center; gap:8px; margin:24px 0 18px; }
    .ptz {
      border:1px solid var(--border); border-radius:12px; background:var(--panel-2);
      color:var(--text); font-size:1.25rem; font-weight:800; cursor:pointer;
      transition:transform .08s ease,border-color .15s ease,background .15s ease;
    }
    .ptz:hover:not(:disabled) { border-color:var(--accent); background:#203451; }
    .ptz:active:not(:disabled) { transform:scale(.96); }
    .ptz.stop { color:#ffcf75; }
    .ptz:disabled { opacity:.38; cursor:not-allowed; }
    .up{grid-column:2;grid-row:1}.left{grid-column:1;grid-row:2}.stop{grid-column:2;grid-row:2}.right{grid-column:3;grid-row:2}.down{grid-column:2;grid-row:3}
    .caps { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .cap { padding:.38rem .58rem; border-radius:8px; background:#162238; color:#c5d2e5; font-size:.8rem; border:1px solid #263754; }
    .cap.off { opacity:.45; text-decoration:line-through; }
    .error { margin-top:14px; padding:12px; border-radius:10px; background:#381d23; color:#ffc1c1; border:1px solid #66303a; white-space:pre-wrap; word-break:break-word; font-size:.86rem; }
    .empty { color:var(--muted); padding:32px 0; }
    .toast { position:fixed; right:18px; bottom:18px; max-width:min(420px,calc(100vw - 36px)); background:#10192a; border:1px solid var(--border); padding:12px 14px; border-radius:10px; box-shadow:var(--shadow); display:none; }
    .toast.show { display:block; }
    .toast.bad { border-color:#6c3440; color:#ffc4c4; }
    @media (max-width:620px) { header{align-items:stretch;flex-direction:column}.refresh{width:100%}.shell{padding:22px 14px 42px}.grid{grid-template-columns:1fr} }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <h1>ONVIF Camera Control</h1>
        <p class="subtitle">Multi-camera PTZ proxy status and manual movement controls</p>
      </div>
      <button class="refresh" onclick="loadCameras()">Refresh status</button>
    </header>
    <main id="camera-grid" class="grid"><div class="empty">Loading cameras…</div></main>
  </div>
  <div id="toast" class="toast"></div>
  <script>
    const grid = document.getElementById('camera-grid');
    const toast = document.getElementById('toast');

    function esc(value) {
      return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    }

    function notify(message, bad=false) {
      toast.textContent = message;
      toast.className = `toast show${bad ? ' bad' : ''}`;
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => toast.className='toast', 4200);
    }

    async function command(id, action, payload={}) {
      try {
        const response = await fetch(`/api/cameras/${encodeURIComponent(id)}/${action}`, {
          method: 'POST',
          headers: {'content-type':'application/json'},
          body: JSON.stringify(payload)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        notify(`${id}: ${action} accepted`);
      } catch (error) {
        notify(`${id}: ${error.message}`, true);
      } finally {
        await loadCameras();
      }
    }

    function capability(name, enabled) {
      return `<span class="cap${enabled ? '' : ' off'}">${esc(name)}</span>`;
    }

    function card(camera) {
      const available = camera.available !== false;
      const caps = camera.capabilities || {};
      const disabled = !available || !caps.pan_tilt;
      return `<section class="card">
        <div class="card-head">
          <div>
            <h2 class="camera-name">${esc(camera.name)}</h2>
            <div class="meta">${esc(camera.driver)} · ${esc(camera.host)}<br>${esc(camera.listen || '')}</div>
          </div>
          <span class="status${available ? '' : ' bad'}"><span class="dot"></span>${available ? 'Available' : 'Unavailable'}</span>
        </div>
        <div class="controls">
          <button class="ptz up" ${disabled?'disabled':''} aria-label="Tilt up" onclick="command('${esc(camera.id)}','move',{tilt:1})">↑</button>
          <button class="ptz left" ${disabled?'disabled':''} aria-label="Pan left" onclick="command('${esc(camera.id)}','move',{pan:-1})">←</button>
          <button class="ptz stop" ${disabled?'disabled':''} aria-label="Stop" onclick="command('${esc(camera.id)}','stop')">■</button>
          <button class="ptz right" ${disabled?'disabled':''} aria-label="Pan right" onclick="command('${esc(camera.id)}','move',{pan:1})">→</button>
          <button class="ptz down" ${disabled?'disabled':''} aria-label="Tilt down" onclick="command('${esc(camera.id)}','move',{tilt:-1})">↓</button>
        </div>
        <div class="caps">
          ${capability('Pan / tilt', !!caps.pan_tilt)}
          ${capability('Zoom', !!caps.zoom)}
          ${capability('Presets', !!caps.presets)}
          ${capability('Audio', !!caps.audio)}
        </div>
        ${camera.error ? `<div class="error">${esc(camera.error)}</div>` : ''}
      </section>`;
    }

    async function loadCameras() {
      try {
        const response = await fetch('/api/cameras', {cache:'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const cameras = await response.json();
        grid.innerHTML = cameras.length ? cameras.map(card).join('') : '<div class="empty">No cameras configured.</div>';
      } catch (error) {
        grid.innerHTML = `<div class="error">Could not load camera status: ${esc(error.message)}</div>`;
      }
    }

    loadCameras();
    setInterval(loadCameras, 10000);
  </script>
</body>
</html>'''
