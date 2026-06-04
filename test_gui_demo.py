#!/usr/bin/env python3
"""Rich GUI demo for the sandbox widget — shows what it can actually do."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, datetime, random

COLORS = ["#f38ba8","#a6e3a1","#89b4fa","#f9e2af","#cba6f7","#fab387","#94e2d5","#f5c2e7"]

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Sandbox GUI Demo</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI', sans-serif; overflow: hidden; }

  .header { background: #181825; padding: 12px 20px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #313244; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header .badge { background: #a6e3a1; color: #1e1e2e; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; }

  .main { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px; height: calc(100vh - 52px); }

  .card { background: #181825; border-radius: 12px; padding: 16px; border: 1px solid #313244; overflow: hidden; }
  .card h2 { font-size: 14px; color: #89b4fa; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }

  /* Canvas card */
  #canvas-card canvas { width: 100%; height: calc(100% - 30px); border-radius: 8px; cursor: crosshair; }

  /* Buttons card */
  .btn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .btn { padding: 10px; border: none; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s; color: #1e1e2e; }
  .btn:hover { transform: scale(1.05); filter: brightness(1.2); }
  .btn:active { transform: scale(0.95); }
  .counter { text-align: center; font-size: 48px; font-weight: 800; margin: 16px 0; font-variant-numeric: tabular-nums; }

  /* Stats card */
  .stat-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #313244; }
  .stat-row:last-child { border: none; }
  .stat-label { color: #a6adc8; font-size: 13px; }
  .stat-value { font-weight: 700; font-size: 15px; }
  .bar-bg { width: 120px; height: 8px; background: #313244; border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width 1s ease; }

  /* Log card */
  .log { font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px; height: calc(100% - 30px); overflow-y: auto; background: #11111b; border-radius: 8px; padding: 8px; }
  .log-line { padding: 2px 0; opacity: 0; animation: fadeIn 0.3s forwards; }
  .log-line .ts { color: #585b70; }
  .log-line .msg { color: #a6e3a1; }
  .log-line .warn { color: #f9e2af; }
  .log-line .err { color: #f38ba8; }
  @keyframes fadeIn { to { opacity: 1; } }

  /* Toast */
  .toast { position: fixed; bottom: 20px; right: 20px; background: #313244; padding: 12px 20px; border-radius: 8px; font-size: 13px; transform: translateY(100px); transition: transform 0.3s; border-left: 3px solid #a6e3a1; }
  .toast.show { transform: translateY(0); }
</style>
</head>
<body>
<div class="header">
  <h1>Sandbox GUI Demo</h1>
  <span class="badge">LIVE</span>
  <span style="color:#585b70; margin-left:auto; font-size:12px;" id="clock"></span>
</div>
<div class="main">

  <!-- Drawing Canvas -->
  <div class="card" id="canvas-card">
    <h2>Interactive Canvas (draw here)</h2>
    <canvas id="canvas"></canvas>
  </div>

  <!-- Buttons & Counter -->
  <div class="card">
    <h2>Interactive Controls</h2>
    <div class="counter" id="counter">0</div>
    <div class="btn-grid">
      <button class="btn" style="background:#a6e3a1" onclick="changeCounter(1)">+ Add</button>
      <button class="btn" style="background:#f38ba8" onclick="changeCounter(-1)">- Subtract</button>
      <button class="btn" style="background:#89b4fa" onclick="randomize()">Random</button>
      <button class="btn" style="background:#f9e2af" onclick="resetCounter()">Reset</button>
      <button class="btn" style="background:#cba6f7" onclick="clearCanvas()">Clear Canvas</button>
      <button class="btn" style="background:#fab387" onclick="sendToServer()">Ping Server</button>
    </div>
  </div>

  <!-- Live Stats -->
  <div class="card">
    <h2>Live System Stats</h2>
    <div id="stats"></div>
  </div>

  <!-- Event Log -->
  <div class="card">
    <h2>Event Log</h2>
    <div class="log" id="log"></div>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
// Clock
setInterval(() => {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}, 1000);

// Counter
let count = 0;
function changeCounter(d) { count += d; document.getElementById('counter').textContent = count; addLog('Counter: ' + count); }
function randomize() { count = Math.floor(Math.random() * 1000); document.getElementById('counter').textContent = count; addLog('Randomized to ' + count, 'warn'); }
function resetCounter() { count = 0; document.getElementById('counter').textContent = 0; addLog('Counter reset'); }

// Canvas drawing
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
let drawing = false, hue = 0;

function resizeCanvas() { canvas.width = canvas.offsetWidth; canvas.height = canvas.offsetHeight; ctx.fillStyle = '#11111b'; ctx.fillRect(0,0,canvas.width,canvas.height); }
window.addEventListener('resize', resizeCanvas);
setTimeout(resizeCanvas, 100);

canvas.addEventListener('mousedown', e => { drawing = true; ctx.beginPath(); ctx.moveTo(e.offsetX, e.offsetY); });
canvas.addEventListener('mousemove', e => {
  if (!drawing) return;
  hue = (hue + 2) % 360;
  ctx.strokeStyle = `hsl(${hue}, 80%, 70%)`;
  ctx.lineWidth = 3;
  ctx.lineCap = 'round';
  ctx.lineTo(e.offsetX, e.offsetY);
  ctx.stroke();
});
canvas.addEventListener('mouseup', () => { drawing = false; addLog('Drew on canvas'); });
canvas.addEventListener('mouseleave', () => { drawing = false; });

function clearCanvas() { ctx.fillStyle = '#11111b'; ctx.fillRect(0,0,canvas.width,canvas.height); addLog('Canvas cleared', 'warn'); }

// Server ping
async function sendToServer() {
  try {
    const r = await fetch('/api/ping', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({count, ts: Date.now()})});
    const d = await r.json();
    showToast(d.message);
    addLog('Server: ' + d.message);
  } catch(e) { addLog('Server error: ' + e.message, 'err'); }
}

// Stats
function updateStats() {
  const stats = [
    {label: 'CPU Usage', value: (20 + Math.random()*60).toFixed(1) + '%', pct: 20+Math.random()*60, color: '#89b4fa'},
    {label: 'Memory', value: (40 + Math.random()*30).toFixed(1) + '%', pct: 40+Math.random()*30, color: '#a6e3a1'},
    {label: 'Network I/O', value: (Math.random()*100).toFixed(0) + ' MB/s', pct: Math.random()*100, color: '#f9e2af'},
    {label: 'Disk', value: '67.3%', pct: 67.3, color: '#cba6f7'},
    {label: 'Uptime', value: formatUptime(), pct: 100, color: '#94e2d5'},
  ];
  document.getElementById('stats').innerHTML = stats.map(s => `
    <div class="stat-row">
      <span class="stat-label">${s.label}</span>
      <div class="bar-bg"><div class="bar-fill" style="width:${s.pct}%;background:${s.color}"></div></div>
      <span class="stat-value" style="color:${s.color}">${s.value}</span>
    </div>
  `).join('');
}
let startTime = Date.now();
function formatUptime() { let s = Math.floor((Date.now()-startTime)/1000); let m=Math.floor(s/60); s%=60; return m+'m '+s+'s'; }
setInterval(updateStats, 2000);
updateStats();

// Log
function addLog(msg, type='msg') {
  const log = document.getElementById('log');
  const ts = new Date().toLocaleTimeString();
  log.innerHTML += `<div class="log-line"><span class="ts">${ts}</span> <span class="${type}">${msg}</span></div>`;
  log.scrollTop = log.scrollHeight;
}
addLog('GUI demo initialized');
addLog('Draw on the canvas, click buttons, ping the server!');

// Toast
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"[{datetime.datetime.now():%H:%M:%S}] GET {self.path}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        print(f"[{datetime.datetime.now():%H:%M:%S}] POST {self.path} body={body}", flush=True)

        resp = {"message": f"Pong! Counter={body.get('count',0)}, server_time={datetime.datetime.now():%H:%M:%S}"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())

    def log_message(self, format, *args):
        pass  # suppress default HTTP logs, we print our own

if __name__ == "__main__":
    print("GUI Demo starting on :8080", flush=True)
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
