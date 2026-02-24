#!/usr/bin/env python3
"""Sandbox Preview Router — serves the sandbox widget page on port 7777.

Auto-started by mcp_server.py as a background thread, but can also run standalone.
Serves an HTML page with an iframe that auto-loads whatever sandbox is running.
Rewrites localhost URLs to match the client's host (for Tailscale/remote access).
"""

import json
import os
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
ROUTER_PORT = 7777

ROUTER_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Sandbox</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #1e1e2e; color: #cdd6f4; font-family: 'JetBrains Mono', 'Fira Code', monospace; }
iframe { width: 100%; height: 100vh; border: none; display: none; }
.idle {
  display: flex; align-items: center; justify-content: center; height: 100vh;
  flex-direction: column; gap: 16px;
}
.idle h1 { color: #22c55e; font-size: 28px; }
.idle p { color: #6c7086; font-size: 14px; }
.idle .dot { display: inline-block; animation: pulse 1.5s infinite; color: #22c55e; }
@keyframes pulse { 0%,100% { opacity: .3; } 50% { opacity: 1; } }
.status-bar {
  position: fixed; bottom: 0; left: 0; right: 0; height: 28px;
  background: #181825; border-top: 1px solid #313244;
  display: flex; align-items: center; padding: 0 12px;
  font-size: 12px; color: #6c7086; z-index: 10;
}
.status-bar .live { color: #22c55e; }
</style>
</head><body>
<div id="idle" class="idle">
  <h1><span class="dot">&#9654;</span> SANDBOX</h1>
  <p>Waiting for sandbox_start()...</p>
  <p style="color:#45475a">Claude will launch programs here automatically</p>
</div>
<iframe id="frame" src="about:blank"></iframe>
<div id="bar" class="status-bar" style="display:none">
  <span class="live">&#9679;</span>&nbsp;
  <span id="bar-text">Loading...</span>
</div>
<script>
let currentUrl = null;
async function poll() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.preview_url && d.status === 'running') {
      if (d.preview_url !== currentUrl) {
        currentUrl = d.preview_url;
        document.getElementById('frame').src = currentUrl;
      }
      document.getElementById('idle').style.display = 'none';
      document.getElementById('frame').style.display = 'block';
      document.getElementById('bar').style.display = 'flex';
      document.getElementById('bar-text').textContent =
        d.project + ' | ' + d.command + (d.port ? ' | port ' + d.port : '') + ' | ' + d.preview_type;
    } else {
      if (currentUrl) {
        currentUrl = null;
        document.getElementById('frame').src = 'about:blank';
      }
      document.getElementById('idle').style.display = 'flex';
      document.getElementById('frame').style.display = 'none';
      document.getElementById('bar').style.display = 'none';
    }
  } catch(e) {}
}
setInterval(poll, 2000);
poll();
</script>
</body></html>"""


def _get_sandbox_state():
    """Query DB for running sandbox."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT ss.*, p.name as project_name
               FROM sandbox_state ss
               JOIN projects p ON p.id = ss.project_id
               WHERE ss.status = 'running'
               ORDER BY ss.id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


class SandboxRouterHandler(BaseHTTPRequestHandler):
    def _rewrite_url(self, url):
        """Replace localhost with the host the client used to reach us."""
        if not url:
            return url
        host_header = self.headers.get("Host", "localhost")
        client_host = host_header.split(":")[0]
        if client_host and client_host not in ("localhost", "127.0.0.1"):
            return url.replace("localhost", client_host).replace("127.0.0.1", client_host)
        return url

    def do_GET(self):
        if self.path == '/api/status':
            state = _get_sandbox_state()
            if state:
                body = json.dumps({
                    "status": "running",
                    "preview_url": self._rewrite_url(state.get("preview_url")),
                    "project": state.get("project_name"),
                    "command": state.get("command"),
                    "port": state.get("port"),
                    "preview_type": state.get("preview_type"),
                })
            else:
                body = json.dumps({"status": "idle", "preview_url": None})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(ROUTER_HTML.encode())

    def log_message(self, format, *args):
        pass  # Suppress request logging


def run_router(port=ROUTER_PORT):
    server = HTTPServer(("0.0.0.0", port), SandboxRouterHandler)
    print(f"Sandbox router running on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    run_router()
