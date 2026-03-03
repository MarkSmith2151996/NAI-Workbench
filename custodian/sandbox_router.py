#!/usr/bin/env python3
"""Sandbox Preview Router — serves the sandbox widget page on port 7777.

Auto-started by mcp_server.py as a background thread, but can also run standalone.
Serves an HTML page with an iframe that auto-loads whatever sandbox is running.
When idle, shows a live workbench status dashboard (indexing, agents, shared files).
Rewrites localhost URLs to match the client's host (for Tailscale/remote access).
"""

import json
import os
import subprocess
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
SHARED_DIR = os.path.expanduser("~/.workbench/shared")
ROUTER_PORT = 7777

ROUTER_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Sandbox</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #1e1e2e; color: #cdd6f4; font-family: 'JetBrains Mono', 'Fira Code', monospace; overflow: hidden; }
iframe { width: 100%; height: calc(100vh - 26px); border: none; display: none; }

/* Idle screen — minimal centered label */
.idle-screen {
  display: flex; flex-direction: column; justify-content: center; align-items: center;
  height: calc(100vh - 26px); /* leave room for ticker */
  user-select: none;
}
.idle-logo { color: #313244; font-size: 48px; font-weight: bold; letter-spacing: 6px; }
.idle-sub { color: #45475a; font-size: 11px; margin-top: 8px; }

/* Scrolling ticker bar — burnt orange with white text */
.ticker-bar {
  position: fixed; bottom: 0; left: 0; right: 0; height: 26px;
  background: #c2410c; border-top: 1px solid #ea580c;
  overflow: hidden; z-index: 100;
  display: flex; align-items: center;
  transition: background 0.3s ease, border-color 0.3s ease;
}
.ticker-track {
  display: flex; align-items: center; white-space: nowrap;
  animation: scroll-left 20s linear infinite;
  font-size: 12px; color: #fff; font-weight: 600;
}
.ticker-track:hover { animation-play-state: paused; }
@keyframes scroll-left {
  0%   { transform: translateX(0); }
  100% { transform: translateX(-50%); }
}
.ticker-item {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 0 12px;
}
.ticker-sep {
  color: rgba(255,255,255,0.3); padding: 0 2px; font-size: 12px;
}
.ticker-item .dot { font-size: 8px; }
.dot-green { color: #fff; opacity: 1; }
.dot-yellow { color: #fef08a; opacity: 1; }
.dot-blue { color: #bfdbfe; opacity: 0.9; }
.dot-dim { color: rgba(255,255,255,0.5); }

/* Flash effect — entire bar goes green on completion */
.ticker-bar.flash-event {
  animation: ticker-flash 2s ease-out;
}
@keyframes ticker-flash {
  0%   { background: #22c55e; border-color: #16a34a; }
  50%  { background: #22c55e; border-color: #16a34a; }
  100% { background: #c2410c; border-color: #ea580c; }
}

</style>
</head><body>

<!-- Idle screen (shown when sandbox not running) -->
<div id="idle" class="idle-screen">
  <div class="idle-logo">SANDBOX</div>
  <div class="idle-sub">waiting for sandbox_start()</div>
</div>

<!-- Sandbox iframe (shown when sandbox is running) -->
<iframe id="frame" src="about:blank" allow="fullscreen" allowfullscreen></iframe>

<!-- Scrolling ticker (always visible at bottom) -->
<div id="ticker" class="ticker-bar">
  <div id="ticker-track" class="ticker-track"></div>
</div>


<script>
let currentUrl = null;
let lastFossilId = null;
let lastIndexingActive = false;
let lastSandboxActive = false;
let flashTimeout = null;
let tickerConfig = {indexing: 1, sandbox: 1, fossils: 1, shared_files: 1, projects: 1};

function buildTickerItems(wb, config) {
  const items = [];

  // Indexing
  if (config.indexing && wb.indexing && wb.indexing.active) {
    items.push({dot: 'yellow', text: 'INDEXING ' + wb.indexing.project + ' ' + wb.indexing.step});
  }

  // Detect indexing completion → flash
  if (lastIndexingActive && !(wb.indexing && wb.indexing.active)) {
    triggerFlash();
  }
  lastIndexingActive = !!(wb.indexing && wb.indexing.active);

  // Sandbox
  const sandboxActive = !!(wb.sandbox && wb.sandbox.active);
  if (config.sandbox && sandboxActive) {
    items.push({dot: 'green', text: 'SANDBOX ' + wb.sandbox.project + ':' + wb.sandbox.port});
  }

  // Detect sandbox start/stop transitions → flash
  if (lastSandboxActive !== sandboxActive) {
    triggerFlash();
  }
  lastSandboxActive = sandboxActive;

  // Latest fossils
  if (config.fossils && wb.fossils && wb.fossils.length > 0) {
    for (const f of wb.fossils.slice(0, 3)) {
      const isNew = lastFossilId !== null && f.id > lastFossilId;
      const prefix = isNew ? 'NEW FOSSIL' : 'fossil';
      items.push({
        dot: isNew ? 'green' : 'dim',
        text: prefix + ' ' + f.project + ' (' + f.symbols + ' symbols)'
      });
      if (isNew) triggerFlash();
    }
    lastFossilId = wb.fossils[0].id;
  } else if (wb.fossils && wb.fossils.length > 0) {
    // Still track lastFossilId even if fossils display is disabled
    lastFossilId = wb.fossils[0].id;
  }

  // Shared files
  if (config.shared_files && wb.shared_files && wb.shared_files.length > 0) {
    items.push({dot: 'blue', text: 'SHARED ' + wb.shared_files.length + ' files'});
  }

  // Projects count
  if (config.projects && wb.projects && wb.projects.length > 0) {
    items.push({dot: 'dim', text: wb.projects.length + ' projects registered'});
  }

  // Watchdog / SSH status
  if (wb.watchdog) {
    const wd = wb.watchdog;
    const sshd = wd.sshd || {};
    if (sshd.status === 'ok') {
      items.push({dot: 'green', text: 'SSH ok'});
    } else if (sshd.status === 'recovered') {
      items.push({dot: 'yellow', text: 'SSH recovered (' + sshd.recoveries + 'x)'});
    } else if (sshd.status) {
      items.push({dot: 'yellow', text: 'SSH ' + sshd.status});
    }
    if (wd._stale) {
      items.push({dot: 'yellow', text: 'watchdog stale'});
    }
  }

  // Fallback
  if (items.length === 0) {
    items.push({dot: 'dim', text: 'workbench idle'});
  }

  return items;
}

function triggerFlash() {
  const bar = document.getElementById('ticker');
  bar.classList.remove('flash-event');
  void bar.offsetWidth; // force reflow
  bar.classList.add('flash-event');
  if (flashTimeout) clearTimeout(flashTimeout);
  flashTimeout = setTimeout(() => bar.classList.remove('flash-event'), 2500);
}

function renderTicker(items) {
  let html = '';
  const all = items.concat(items); // double for seamless loop
  for (let i = 0; i < all.length; i++) {
    const item = all[i];
    html += '<span class="ticker-item">' +
      '<span class="dot dot-' + item.dot + '">\u25CF</span> ' +
      item.text + '</span>';
    // Add separator between items (but not after the last one in each half)
    if (i < all.length - 1) {
      html += '<span class="ticker-sep">|</span>';
    }
  }
  document.getElementById('ticker-track').innerHTML = html;

  // Adjust animation speed based on content width
  const track = document.getElementById('ticker-track');
  const halfWidth = track.scrollWidth / 2;
  const speed = Math.max(10, halfWidth / 50); // ~50px/sec
  track.style.animationDuration = speed + 's';
}

async function poll() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    if (d.status === 'running') {
      if (d.preview_url && d.preview_url !== currentUrl) {
        currentUrl = d.preview_url;
        document.getElementById('frame').src = currentUrl;
        document.getElementById('frame').style.display = 'block';
      }
      document.getElementById('idle').style.display = 'none';
    } else {
      if (currentUrl) {
        currentUrl = null;
        document.getElementById('frame').src = 'about:blank';
        document.getElementById('frame').style.display = 'none';
      }
      document.getElementById('idle').style.display = 'flex';
    }
    document.getElementById('ticker').style.display = 'flex';
  } catch(e) {}

  // Fetch ticker config + workbench status in parallel
  try {
    const [cfgRes, wbRes] = await Promise.all([
      fetch('/api/ticker-config'),
      fetch('/api/workbench')
    ]);
    tickerConfig = await cfgRes.json();
    const wb = await wbRes.json();
    const items = buildTickerItems(wb, tickerConfig);
    renderTicker(items);
  } catch(e) {}
}

setInterval(poll, 3000);
poll();
</script>
</body></html>"""


def _is_container_alive(container_name):
    """Check if a Docker container is actually running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False


def _mark_stopped(build_id):
    """Mark an alpha_build as stopped in the DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE alpha_builds SET status = 'stopped' WHERE id = ?", (build_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_sandbox_state():
    """Query DB for running sandbox (alpha_builds table, Docker-backed).
    Verifies the container is actually alive — auto-corrects stale DB state."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT ab.*, p.name as project_name, p.stack
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               WHERE ab.status = 'running'
               ORDER BY ab.started_at DESC LIMIT 1"""
        ).fetchone()
        if row:
            data = dict(row)
            container = data.get("container_name", "")

            # Verify container is actually alive
            if not _is_container_alive(container):
                conn.close()
                _mark_stopped(data["id"])
                return None

            ports = json.loads(data["ports"]) if data.get("ports") else {}
            port = list(ports.keys())[0] if ports else None
            conn.close()
            return {
                "status": "running",
                "project_name": data["project_name"],
                "command": data.get("command") or f"docker: {container}",
                "port": port,
                "preview_type": "web" if port else "terminal",
                "preview_url": f"http://localhost:{port}" if port else None,
                "container": container,
            }
        # Fallback: check legacy sandbox_state
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


def _get_workbench_status():
    """Get full workbench status for the dashboard."""
    result = {
        "indexing": {"active": False},
        "sandbox": {"active": False},
        "fossils": [],
        "shared_files": [],
        "projects": [],
    }

    # Check for running indexing processes
    try:
        proc = subprocess.run(
            ["pgrep", "-af", "index_project.sh"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode == 0:
            for line in proc.stdout.strip().split("\n"):
                if "index_project.sh" in line:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if "index_project.sh" in p and i + 1 < len(parts):
                            project = parts[i + 1]
                            # Guess step from temp files
                            step = "running"
                            tmp = "/tmp/custodian"
                            if os.path.isfile(f"{tmp}/fossil-{project}.json"):
                                sz = os.path.getsize(f"{tmp}/fossil-{project}.json")
                                step = "6/6 storing" if sz > 0 else "5/6 sonnet"
                            elif os.path.isfile(f"{tmp}/sonnet-input-{project}.txt"):
                                step = "5/6 sonnet"
                            elif os.path.isfile(f"{tmp}/gitlog-{project}.txt"):
                                step = "4/6 prompt"
                            elif os.path.isfile(f"{tmp}/symbols-{project}.json"):
                                step = "3/6 git"
                            elif os.path.isfile(f"{tmp}/repomix-{project}.txt"):
                                step = "2/6 symbols"
                            else:
                                step = "1/6 repomix"
                            result["indexing"] = {"active": True, "project": project, "step": step}
                            break
    except Exception:
        pass

    # Check sandbox (verify container is actually alive)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT ab.id, ab.container_name, ab.ports, p.name
               FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
               WHERE ab.status = 'running'
               ORDER BY ab.started_at DESC LIMIT 1"""
        ).fetchone()
        if row:
            # Verify container is actually alive before reporting it
            if _is_container_alive(row["container_name"]):
                ports = json.loads(row["ports"]) if row["ports"] else {}
                port = list(ports.keys())[0] if ports else "?"
                result["sandbox"] = {"active": True, "project": row["name"], "port": port}
            else:
                _mark_stopped(row["id"])

        # Latest fossils
        rows = conn.execute(
            """SELECT f.id, p.name as project, f.created_at,
                      (SELECT COUNT(*) FROM symbols s WHERE s.fossil_id = f.id) as symbols
               FROM fossils f JOIN projects p ON p.id = f.project_id
               ORDER BY f.id DESC LIMIT 5"""
        ).fetchall()
        result["fossils"] = [
            {"id": r["id"], "project": r["project"],
             "created_at": r["created_at"], "symbols": r["symbols"]}
            for r in rows
        ]

        # Projects
        rows = conn.execute(
            "SELECT name, stack FROM projects WHERE status = 'active' ORDER BY name"
        ).fetchall()
        result["projects"] = [{"name": r["name"], "stack": r["stack"]} for r in rows]

        conn.close()
    except Exception:
        pass

    # Shared files
    if os.path.isdir(SHARED_DIR):
        try:
            files = [f for f in os.listdir(SHARED_DIR) if not f.startswith(".")]
            result["shared_files"] = sorted(files)[:20]
        except Exception:
            pass

    # Watchdog health
    result["watchdog"] = _read_watchdog_health()

    return result


WATCHDOG_HEALTH_FILE = "/tmp/watchdog-health.json"


def _read_watchdog_health():
    """Read watchdog health file. Returns None if watchdog not running."""
    try:
        if os.path.isfile(WATCHDOG_HEALTH_FILE):
            with open(WATCHDOG_HEALTH_FILE, "r") as f:
                data = json.load(f)
            # Consider stale if older than 30 seconds
            import time as _time
            ts = data.get("timestamp", "")
            try:
                from datetime import datetime
                health_time = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") if "+" in ts or ts.endswith("Z") \
                    else datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
                age = (_time.time() - health_time.timestamp())
                if age > 30:
                    data["_stale"] = True
            except Exception:
                pass
            return data
    except Exception:
        pass
    return None


def _get_health_summary():
    """Build machine-readable health summary for /api/health."""
    watchdog = _read_watchdog_health()
    services = {}
    overall = "healthy"

    if watchdog is None:
        overall = "degraded"
        services["watchdog"] = "not running"
    elif watchdog.get("_stale"):
        overall = "degraded"
        services["watchdog"] = "stale"
    else:
        services["watchdog"] = "ok"

    # sshd
    if watchdog:
        sshd = watchdog.get("sshd", {})
        sshd_status = sshd.get("status", "unknown")
        services["sshd"] = sshd_status
        if sshd_status not in ("ok", "recovered"):
            overall = "unhealthy"
    else:
        services["sshd"] = "unknown"

    # Docker
    if watchdog:
        docker = watchdog.get("docker", {})
        docker_status = docker.get("status", "unknown")
        services["docker"] = docker_status
        if docker_status not in ("ok", "recovered"):
            overall = "unhealthy"
    else:
        services["docker"] = "unknown"

    # Sandbox router itself is obviously ok if we're serving this
    services["sandbox_router"] = "ok"

    return {
        "status": overall,
        "services": services,
        "wsl_ip": watchdog.get("wsl_ip") if watchdog else None,
        "watchdog_uptime": watchdog.get("uptime_seconds") if watchdog else None,
    }


def _get_ticker_config():
    """Get ticker config from DB. Returns dict of key -> 0/1."""
    defaults = {"indexing": 1, "sandbox": 1, "fossils": 1, "shared_files": 1, "projects": 1}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ticker_config (key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)"
        )
        rows = conn.execute("SELECT key, enabled FROM ticker_config").fetchall()
        conn.close()
        if rows:
            config = dict(defaults)
            for r in rows:
                config[r["key"]] = r["enabled"]
            return config
    except Exception:
        pass
    return defaults


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
                    "container": state.get("container"),
                })
            else:
                body = json.dumps({"status": "idle", "preview_url": None})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode())

        elif self.path == '/api/workbench':
            body = json.dumps(_get_workbench_status())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode())

        elif self.path == '/api/ticker-config':
            body = json.dumps(_get_ticker_config())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode())

        elif self.path == '/api/health':
            body = json.dumps(_get_health_summary())
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
