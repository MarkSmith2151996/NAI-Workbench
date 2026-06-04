#!/usr/bin/env python3
"""Sandbox Preview Router — serves the sandbox widget page on port 7777.

Can run standalone (systemd service) or as a fallback thread in mcp_server.py.
Serves a tabbed widget (Terminal / Preview / Split) with live log streaming.
When idle, shows a workbench status dashboard (indexing, agents, shared files).
Rewrites localhost URLs to match the client's host (for Tailscale/remote access).
"""

import json
import os
import secrets
import string
import subprocess
import sqlite3
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
SHARED_DIR = os.path.expanduser("~/.workbench/shared")
ROUTER_PORT = 7777

ROUTER_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Sandbox</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #1e1e2e; color: #cdd6f4; font-family: 'JetBrains Mono', 'Fira Code', monospace; overflow: hidden; padding-top: 26px; }

/* Tab bar */
.tab-bar {
  display: none; height: 32px; background: #181825; border-bottom: 1px solid #313244;
  align-items: center; padding: 0 8px; gap: 2px; user-select: none;
}
.tab-bar.visible { display: flex; }
.tab-btn {
  padding: 4px 14px; font-size: 12px; font-family: inherit; cursor: pointer;
  background: transparent; color: #6c7086; border: none; border-radius: 4px 4px 0 0;
  transition: all 0.15s;
}
.tab-btn:hover { color: #cdd6f4; background: #313244; }
.tab-btn.active { color: #cdd6f4; background: #1e1e2e; border-bottom: 2px solid #f9e2af; }
.tab-project {
  margin-left: auto; font-size: 11px; color: #585b70; padding: 0 8px;
}

/* Status bar */
.status-bar {
  display: none; height: 22px; background: #181825; border-bottom: 1px solid #313244;
  align-items: center; padding: 0 10px; font-size: 11px; color: #a6adc8;
  gap: 8px;
}
.status-bar.visible { display: flex; }
.status-dot { font-size: 8px; }
.status-dot.green { color: #a6e3a1; }
.status-dot.yellow { color: #f9e2af; }
.status-dot.red { color: #f38ba8; }

/* Content area */
.content { height: calc(100vh - 26px); position: relative; }
.content.with-tabs { height: calc(100vh - 80px); }

/* Log viewer */
.log-viewer {
  width: 100%; height: 100%; overflow-y: auto; padding: 8px 12px;
  font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  background: #1e1e2e; color: #cdd6f4; display: none;
}
.log-viewer.visible { display: block; }
.log-viewer::-webkit-scrollbar { width: 8px; }
.log-viewer::-webkit-scrollbar-track { background: #181825; }
.log-viewer::-webkit-scrollbar-thumb { background: #45475a; border-radius: 4px; }
.log-line-error { color: #f38ba8; }
.log-line-warn { color: #f9e2af; }
.log-empty { color: #585b70; font-style: italic; text-align: center; margin-top: 40px; }

/* Preview iframe */
iframe {
  width: 100%; height: 100%; border: none; display: none; background: #1e1e2e;
}
iframe.visible { display: block; }

/* Split view */
.split-view { display: none; width: 100%; height: 100%; }
.split-view.visible { display: flex; }
.split-left { width: 50%; height: 100%; overflow-y: auto; padding: 8px 12px;
  font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  background: #1e1e2e; color: #cdd6f4; border-right: 1px solid #313244; }
.split-right { width: 50%; height: 100%; }
.split-right iframe { width: 100%; height: 100%; border: none; display: block; }

/* Idle screen */
.idle-screen {
  display: flex; flex-direction: column; justify-content: center; align-items: center;
  height: calc(100vh - 26px); user-select: none;
}
.idle-logo { color: #313244; font-size: 48px; font-weight: bold; letter-spacing: 6px; }
.idle-sub { color: #45475a; font-size: 11px; margin-top: 8px; }

/* Scrolling ticker bar */
.ticker-bar {
  position: fixed; top: 0; left: 0; right: 0; height: 26px;
  background: #c2410c; border-bottom: 1px solid #ea580c;
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
.ticker-item { display: inline-flex; align-items: center; gap: 5px; padding: 0 12px; }
.ticker-sep { color: rgba(255,255,255,0.3); padding: 0 2px; font-size: 12px; }
.ticker-item .dot { font-size: 8px; }
.dot-green { color: #fff; opacity: 1; }
.dot-yellow { color: #fef08a; opacity: 1; }
.dot-blue { color: #bfdbfe; opacity: 0.9; }
.dot-dim { color: rgba(255,255,255,0.5); }
.ticker-bar.flash-event { animation: ticker-flash 2s ease-out; }
@keyframes ticker-flash {
  0%   { background: #22c55e; border-color: #16a34a; }
  50%  { background: #22c55e; border-color: #16a34a; }
  100% { background: #c2410c; border-color: #ea580c; }
}
</style>
</head><body>

<!-- Scrolling ticker — fixed at top -->
<div id="ticker" class="ticker-bar">
  <div id="ticker-track" class="ticker-track"></div>
</div>

<!-- Tab bar (shown when sandbox is running) -->
<div id="tab-bar" class="tab-bar">
  <button class="tab-btn active" data-tab="terminal" onclick="switchTab('terminal')">Terminal</button>
  <button class="tab-btn" data-tab="preview" onclick="switchTab('preview')">Preview</button>
  <button class="tab-btn" data-tab="split" onclick="switchTab('split')">Split</button>
  <span id="tab-project" class="tab-project"></span>
</div>

<!-- Status bar (shown when sandbox is running) -->
<div id="status-bar" class="status-bar">
  <span id="status-dot" class="status-dot green">&#x25CF;</span>
  <span id="status-text">Starting...</span>
</div>

<!-- Content area -->
<div id="content" class="content">
  <!-- Idle screen -->
  <div id="idle" class="idle-screen">
    <div class="idle-logo">SANDBOX</div>
    <div class="idle-sub">waiting for sandbox_start()</div>
  </div>

  <!-- Terminal tab: log viewer -->
  <div id="log-viewer" class="log-viewer"></div>

  <!-- Preview tab: iframe -->
  <iframe id="frame" src="about:blank" allow="fullscreen" allowfullscreen></iframe>

  <!-- Split tab -->
  <div id="split-view" class="split-view">
    <div id="split-logs" class="split-left"></div>
    <div class="split-right">
      <iframe id="split-frame" src="about:blank" allow="fullscreen" allowfullscreen></iframe>
    </div>
  </div>
</div>

<script>
let currentUrl = null;
let currentTab = 'terminal';
let lastStatus = 'idle';
let lastDisplayMode = 'terminal';
let autoScrollLog = true;
let lastFossilId = null;
let lastIndexingActive = false;
let lastSandboxActive = false;
let flashTimeout = null;
let tickerConfig = {indexing: 1, sandbox: 1, fossils: 1, shared_files: 1, projects: 1};

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.getElementById('log-viewer').classList.toggle('visible', tab === 'terminal');
  document.getElementById('frame').classList.toggle('visible', tab === 'preview');
  document.getElementById('split-view').classList.toggle('visible', tab === 'split');
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function classifyLine(line) {
  const lower = line.toLowerCase();
  if (lower.includes('error') || lower.includes('traceback') || lower.includes('exception') || lower.includes('failed')) return 'log-line-error';
  if (lower.includes('warn') || lower.includes('deprecat')) return 'log-line-warn';
  return '';
}

function renderLogs(lines) {
  if (!lines || lines.length === 0) {
    return '<div class="log-empty">No output yet...</div>';
  }
  return lines.map(l => {
    const cls = classifyLine(l);
    return cls ? '<span class="' + cls + '">' + escapeHtml(l) + '</span>' : escapeHtml(l);
  }).join('\n');
}

async function fetchLogs() {
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    const html = renderLogs(d.lines || []);

    // Update Terminal tab
    const lv = document.getElementById('log-viewer');
    const wasAtBottom = lv.scrollHeight - lv.scrollTop - lv.clientHeight < 40;
    lv.innerHTML = html;
    if (autoScrollLog && wasAtBottom) lv.scrollTop = lv.scrollHeight;

    // Update Split tab logs
    const sl = document.getElementById('split-logs');
    const slWasAtBottom = sl.scrollHeight - sl.scrollTop - sl.clientHeight < 40;
    sl.innerHTML = html;
    if (slWasAtBottom) sl.scrollTop = sl.scrollHeight;
  } catch(e) {}
}

async function poll() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    if (d.status === 'running') {
      const displayMode = d.display_mode || 'terminal';
      lastDisplayMode = displayMode;

      // Show tab bar + status bar
      document.getElementById('tab-bar').classList.add('visible');
      document.getElementById('status-bar').classList.add('visible');
      document.getElementById('content').classList.add('with-tabs');
      document.getElementById('idle').style.display = 'none';

      // Update project name in tab bar
      document.getElementById('tab-project').textContent = d.project || '';

      // Update status bar
      const dot = document.getElementById('status-dot');
      const statusText = document.getElementById('status-text');
      if (d.app_alive === false) {
        dot.className = 'status-dot red';
        statusText.textContent = 'App exited \u2014 see Terminal';
        if (currentTab === 'preview') switchTab('terminal');
      } else {
        dot.className = 'status-dot green';
        const cmd = d.original_command || d.command || '';
        const port = d.port ? ' on :' + d.port : '';
        statusText.textContent = 'Running: ' + cmd + port;
      }

      // Load preview iframe if URL available
      if (d.preview_url && d.preview_url !== currentUrl) {
        currentUrl = d.preview_url;
        document.getElementById('frame').src = currentUrl;
        document.getElementById('split-frame').src = currentUrl;
      }

      // Auto-select tab on first run based on display mode
      if (lastStatus !== 'running') {
        if (displayMode === 'web' || displayMode === 'gui') {
          switchTab('preview');
        } else {
          switchTab('terminal');
        }
      }

      // Fetch logs for Terminal/Split tabs
      fetchLogs();

      lastStatus = 'running';
    } else {
      // Idle
      if (lastStatus === 'running') {
        currentUrl = null;
        document.getElementById('frame').src = 'about:blank';
        document.getElementById('split-frame').src = 'about:blank';
      }
      document.getElementById('tab-bar').classList.remove('visible');
      document.getElementById('status-bar').classList.remove('visible');
      document.getElementById('content').classList.remove('with-tabs');
      document.getElementById('idle').style.display = 'flex';
      document.getElementById('log-viewer').classList.remove('visible');
      document.getElementById('frame').classList.remove('visible');
      document.getElementById('split-view').classList.remove('visible');
      lastStatus = 'idle';
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

function buildTickerItems(wb, config) {
  const items = [];

  // AI sessions first
  if (wb.ai_sessions && wb.ai_sessions.length > 0) {
    for (const s of wb.ai_sessions) {
      const proj = s.project.length > 20 ? s.project.slice(0,18) + '..' : s.project;
      const dotColor = s.activity === 'thinking' ? 'yellow' : s.activity === 'waiting' || s.activity === 'waiting for user' ? 'dim' : 'green';
      items.push({dot: dotColor, text: proj + ': ' + s.activity});
    }
  }

  if (config.indexing && wb.indexing && wb.indexing.active) {
    items.push({dot: 'yellow', text: 'INDEXING ' + wb.indexing.project + ' ' + wb.indexing.step});
  }
  if (lastIndexingActive && !(wb.indexing && wb.indexing.active)) triggerFlash();
  lastIndexingActive = !!(wb.indexing && wb.indexing.active);

  const sandboxActive = !!(wb.sandbox && wb.sandbox.active);
  if (config.sandbox && sandboxActive) {
    items.push({dot: 'green', text: 'SANDBOX ' + wb.sandbox.project + ':' + wb.sandbox.port});
  }
  if (lastSandboxActive !== sandboxActive) triggerFlash();
  lastSandboxActive = sandboxActive;

  if (config.fossils && wb.fossils && wb.fossils.length > 0) {
    for (const f of wb.fossils.slice(0, 3)) {
      const isNew = lastFossilId !== null && f.id > lastFossilId;
      items.push({dot: isNew ? 'green' : 'dim', text: (isNew ? 'NEW FOSSIL' : 'fossil') + ' ' + f.project + ' (' + f.symbols + ' symbols)'});
      if (isNew) triggerFlash();
    }
    lastFossilId = wb.fossils[0].id;
  } else if (wb.fossils && wb.fossils.length > 0) {
    lastFossilId = wb.fossils[0].id;
  }

  if (config.shared_files && wb.shared_files && wb.shared_files.length > 0) {
    items.push({dot: 'blue', text: 'SHARED ' + wb.shared_files.length + ' files'});
  }
  if (config.projects && wb.projects && wb.projects.length > 0) {
    items.push({dot: 'dim', text: wb.projects.length + ' projects registered'});
  }
  if (wb.watchdog) {
    const wd = wb.watchdog;
    const sshd = wd.sshd || {};
    if (sshd.status === 'ok') items.push({dot: 'green', text: 'SSH ok'});
    else if (sshd.status === 'recovered') items.push({dot: 'yellow', text: 'SSH recovered (' + sshd.recoveries + 'x)'});
    else if (sshd.status) items.push({dot: 'yellow', text: 'SSH ' + sshd.status});
    if (wd._stale) items.push({dot: 'yellow', text: 'watchdog stale'});
  }
  if (items.length === 0) items.push({dot: 'dim', text: 'workbench idle'});
  return items;
}

function triggerFlash() {
  const bar = document.getElementById('ticker');
  bar.classList.remove('flash-event');
  void bar.offsetWidth;
  bar.classList.add('flash-event');
  if (flashTimeout) clearTimeout(flashTimeout);
  flashTimeout = setTimeout(() => bar.classList.remove('flash-event'), 2500);
}

function renderTicker(items) {
  let html = '';
  const all = items.concat(items);
  for (let i = 0; i < all.length; i++) {
    html += '<span class="ticker-item"><span class="dot dot-' + all[i].dot + '">\u25CF</span> ' + all[i].text + '</span>';
    if (i < all.length - 1) html += '<span class="ticker-sep">|</span>';
  }
  document.getElementById('ticker-track').innerHTML = html;
  const track = document.getElementById('ticker-track');
  const halfWidth = track.scrollWidth / 2;
  track.style.animationDuration = Math.max(10, halfWidth / 50) + 's';
}

setInterval(poll, 3000);
poll();
</script>
</body></html>"""


TICKER_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>NAI Ticker</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; background: var(--bg); }
body {
  --bg: #1e1e2e;
  --fg: #cdd6f4;
  display: flex; align-items: center;
  font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
}
.ticker-bar {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: var(--bg);
  overflow: hidden; display: flex; align-items: center;
}
.ticker-track {
  display: flex; align-items: center; white-space: nowrap;
  animation: scroll-left 20s linear infinite;
  font-size: 13px; color: var(--fg); font-weight: 600;
}
.ticker-track:hover { animation-play-state: paused; }
@keyframes scroll-left {
  0%   { transform: translateX(0); }
  100% { transform: translateX(-50%); }
}
.ticker-item { display: inline-flex; align-items: center; gap: 5px; padding: 0 14px; }
.ticker-sep { color: rgba(255,255,255,0.2); padding: 0 2px; font-size: 12px; }
.dot { font-size: 8px; }
.dot-green { color: #a6e3a1; }
.dot-yellow { color: #f9e2af; }
.dot-blue { color: #89b4fa; }
.dot-dim { color: rgba(205,214,244,0.4); }
.ticker-bar.flash-event { animation: ticker-flash 2s ease-out; }
@keyframes ticker-flash {
  0%   { background: #22c55e; }
  50%  { background: #22c55e; }
  100% { background: var(--bg); }
}
</style>
</head><body>
<div id="ticker" class="ticker-bar">
  <div id="ticker-track" class="ticker-track"></div>
</div>
<script>
let lastFossilId = null;
let lastIndexingActive = false;
let lastSandboxActive = false;
let flashTimeout = null;
let tickerConfig = {};
let settings = {};

function triggerFlash() {
  const bar = document.getElementById('ticker');
  bar.classList.remove('flash-event');
  void bar.offsetWidth;
  bar.classList.add('flash-event');
  if (flashTimeout) clearTimeout(flashTimeout);
  flashTimeout = setTimeout(() => bar.classList.remove('flash-event'), 2500);
}

function buildItems(wb, config) {
  const items = [];

  // AI sessions first
  if (wb.ai_sessions && wb.ai_sessions.length > 0) {
    for (const s of wb.ai_sessions) {
      const proj = s.project.length > 20 ? s.project.slice(0,18) + '..' : s.project;
      const dotColor = s.activity === 'thinking' ? 'yellow' : s.activity === 'waiting' || s.activity === 'waiting for user' ? 'dim' : 'green';
      items.push({dot: dotColor, text: proj + ': ' + s.activity});
    }
  }

  if (config.indexing && wb.indexing && wb.indexing.active) {
    items.push({dot: 'yellow', text: 'INDEXING ' + wb.indexing.project + ' ' + wb.indexing.step});
  }
  if (lastIndexingActive && !(wb.indexing && wb.indexing.active)) triggerFlash();
  lastIndexingActive = !!(wb.indexing && wb.indexing.active);

  const sbActive = !!(wb.sandbox && wb.sandbox.active);
  if (config.sandbox && sbActive) {
    items.push({dot: 'green', text: 'SANDBOX ' + wb.sandbox.project + ':' + wb.sandbox.port});
  }
  if (lastSandboxActive !== sbActive) triggerFlash();
  lastSandboxActive = sbActive;

  if (config.fossils && wb.fossils && wb.fossils.length > 0) {
    for (const f of wb.fossils.slice(0, 3)) {
      const isNew = lastFossilId !== null && f.id > lastFossilId;
      items.push({dot: isNew ? 'green' : 'dim', text: (isNew ? 'NEW FOSSIL' : 'fossil') + ' ' + f.project + ' (' + f.symbols + ' symbols)'});
      if (isNew) triggerFlash();
    }
    lastFossilId = wb.fossils[0].id;
  } else if (wb.fossils && wb.fossils.length > 0) {
    lastFossilId = wb.fossils[0].id;
  }

  if (config.shared_files && wb.shared_files && wb.shared_files.length > 0) {
    items.push({dot: 'blue', text: 'SHARED ' + wb.shared_files.length + ' files'});
  }
  if (config.projects && wb.projects && wb.projects.length > 0) {
    items.push({dot: 'dim', text: wb.projects.length + ' projects registered'});
  }
  if (config.watchdog && wb.watchdog) {
    const wd = wb.watchdog;
    const sshd = wd.sshd || {};
    if (sshd.status === 'ok') items.push({dot: 'green', text: 'SSH ok'});
    else if (sshd.status === 'recovered') items.push({dot: 'yellow', text: 'SSH recovered (' + sshd.recoveries + 'x)'});
    else if (sshd.status) items.push({dot: 'yellow', text: 'SSH ' + sshd.status});
    if (wd._stale) items.push({dot: 'yellow', text: 'watchdog stale'});
  }
  if (items.length === 0) items.push({dot: 'dim', text: 'workbench idle'});
  return items;
}

function renderTicker(items) {
  const all = items.concat(items);
  let html = '';
  for (let i = 0; i < all.length; i++) {
    html += '<span class="ticker-item"><span class="dot dot-' + all[i].dot + '">\u25CF</span> ' + all[i].text + '</span>';
    if (i < all.length - 1) html += '<span class="ticker-sep">|</span>';
  }
  const track = document.getElementById('ticker-track');
  track.innerHTML = html;
  const halfWidth = track.scrollWidth / 2;
  const speed = parseInt(settings.scroll_speed) || 50;
  track.style.animationDuration = Math.max(10, halfWidth / speed) + 's';
}

function applySettings(s) {
  settings = s;
  const root = document.documentElement;
  if (s.bg_color) root.style.setProperty('--bg', s.bg_color);
  if (s.text_color) root.style.setProperty('--fg', s.text_color);
  if (s.bar_height) document.body.style.height = s.bar_height + 'px';
}

async function poll() {
  try {
    const [cfgRes, wbRes, setRes] = await Promise.all([
      fetch('/api/ticker-config'),
      fetch('/api/workbench'),
      fetch('/api/ticker-settings')
    ]);
    tickerConfig = await cfgRes.json();
    const wb = await wbRes.json();
    const s = await setRes.json();
    applySettings(s);
    renderTicker(buildItems(wb, tickerConfig));
  } catch(e) {}
}

poll();
setInterval(poll, (parseInt(settings.poll_interval) || 3) * 1000);
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
    Verifies the container is actually alive — auto-corrects stale DB state.
    Returns display_mode from DB for the widget to pick the right default tab."""
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
            display_mode = data.get("display_mode") or "terminal"

            # Determine original command (strip ttyd/novnc wrappers for display)
            command = data.get("command") or f"docker: {container}"
            original_command = command
            if "ttyd " in command and "tmux attach" in command:
                # Extract the inner command from tmux setup
                import re
                m = re.search(r"2>/dev/null; (.+?) 2>&1", command)
                if m:
                    original_command = m.group(1)
            elif command.startswith("novnc-wrap "):
                parts = command.split(" ", 2)
                if len(parts) >= 3:
                    original_command = parts[2]

            # Check if the app process is still running inside the container
            app_alive = True
            try:
                ps_check = subprocess.run(
                    ["docker", "exec", container, "bash", "-c",
                     "pgrep -f 'sandbox|ttyd|novnc|python|node|npm' >/dev/null 2>&1"],
                    capture_output=True, text=True, timeout=5,
                )
                app_alive = ps_check.returncode == 0
            except Exception:
                pass

            # Get last log line
            last_log = ""
            try:
                log_check = subprocess.run(
                    ["docker", "exec", container, "tail", "-1", "/tmp/sandbox.log"],
                    capture_output=True, text=True, timeout=3,
                )
                if log_check.returncode == 0:
                    last_log = log_check.stdout.strip()
            except Exception:
                pass

            conn.close()
            return {
                "status": "running",
                "project_name": data["project_name"],
                "command": command,
                "original_command": original_command,
                "port": port,
                "display_mode": display_mode,
                "preview_type": "web" if port else "terminal",
                "preview_url": f"http://localhost:{port}" if port else None,
                "container": container,
                "app_alive": app_alive,
                "last_log": last_log,
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


def _get_container_logs(container_name=None, lines=100):
    """Get log lines from the sandbox container.

    Reads /tmp/sandbox.log first (app output), falls back to docker logs.
    """
    if not container_name:
        state = _get_sandbox_state()
        if not state:
            return []
        container_name = state.get("container")
        if not container_name:
            return []

    # Try /tmp/sandbox.log first (has the actual app output)
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "tail", "-n", str(lines), "/tmp/sandbox.log"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")
    except Exception:
        pass

    # Also check wrapper log
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "tail", "-n", str(lines), "/tmp/sandbox_wrapper.log"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")
    except Exception:
        pass

    # Fallback to docker logs
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container_name],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout or result.stderr or ""
        if output.strip():
            return output.strip().split("\n")
    except Exception:
        pass

    return []


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

    # Active AI coding sessions
    ai_sessions = _get_ai_sessions()
    result["ai_sessions"] = ai_sessions
    result["claude_sessions"] = ai_sessions

    return result


def _run_pgrep(process_name):
    """Return a list of PIDs for a process name."""
    try:
        proc = subprocess.run(
            ["pgrep", "-x", process_name], capture_output=True, text=True, timeout=3
        )
        if proc.returncode != 0:
            return []
        return [p.strip() for p in proc.stdout.strip().split("\n") if p.strip()]
    except Exception:
        return []


def _get_ai_sessions():
    """Detect active Claude/OpenCode sessions and summarize their activity."""
    return _get_claude_sessions() + _get_opencode_sessions()


def _get_claude_sessions():
    """Detect running Claude Code sessions and summarize their activity."""
    sessions = []
    try:
        pids = _run_pgrep("claude")
        if not pids:
            return sessions

        # Map session IDs to debug files (most recently modified = active)
        debug_dir = os.path.expanduser("~/.claude/debug")
        debug_files = {}
        if os.path.isdir(debug_dir):
            for fname in os.listdir(debug_dir):
                if fname.endswith(".txt"):
                    fpath = os.path.join(debug_dir, fname)
                    mtime = os.path.getmtime(fpath)
                    debug_files[fpath] = mtime

        # Sort debug files by mtime descending
        active_debug = sorted(debug_files.items(), key=lambda x: x[1], reverse=True)

        # Match PIDs to debug files by finding PID references in recent log lines
        pid_to_debug = {}
        for dpath, mtime in active_debug[:10]:  # only check recent files
            try:
                # Read last 2KB of the file to find PID references
                fsize = os.path.getsize(dpath)
                with open(dpath, "r", errors="replace") as f:
                    f.seek(max(0, fsize - 4096))
                    tail = f.read()
                for pid in pids:
                    if pid not in pid_to_debug and f".tmp.{pid}." in tail:
                        pid_to_debug[pid] = dpath
            except Exception:
                pass

        # If we couldn't match all PIDs, assign remaining debug files by recency
        unmatched_pids = [p for p in pids if p not in pid_to_debug]
        unmatched_debug = [d for d, _ in active_debug if d not in pid_to_debug.values()]
        for pid, dpath in zip(unmatched_pids, unmatched_debug):
            pid_to_debug[pid] = dpath

        for pid in pids:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                project = os.path.basename(cwd)
            except OSError:
                project = "?"

            activity = "active"
            debug_path = pid_to_debug.get(pid)
            if debug_path:
                activity = _parse_claude_activity(debug_path)

            sessions.append({
                "pid": int(pid),
                "project": project,
                "activity": activity,
            })
    except Exception:
        pass
    return sessions


def _get_opencode_sessions():
    """Detect running OpenCode sessions and summarize their activity."""
    sessions = []
    try:
        pids = _run_pgrep("opencode")
        if not pids:
            return sessions

        log_dir = os.path.expanduser("~/.local/share/opencode/log")
        active_logs = []
        if os.path.isdir(log_dir):
            for fname in os.listdir(log_dir):
                if fname.endswith(".log"):
                    fpath = os.path.join(log_dir, fname)
                    try:
                        active_logs.append((fpath, os.path.getmtime(fpath)))
                    except OSError:
                        pass
        active_logs.sort(key=lambda item: item[1], reverse=True)

        unmatched_logs = [path for path, _mtime in active_logs]
        for pid in pids:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                project = os.path.basename(cwd)
            except OSError:
                cwd = ""
                project = "?"

            matched_log = None
            for log_path in list(unmatched_logs[:10]):
                try:
                    with open(log_path, "r", errors="replace") as f:
                        snippet = f.read(2048)
                    if cwd and f"directory={cwd}" in snippet:
                        matched_log = log_path
                        unmatched_logs.remove(log_path)
                        break
                except Exception:
                    continue

            if matched_log is None and unmatched_logs:
                matched_log = unmatched_logs.pop(0)

            activity = "active"
            if matched_log:
                activity = _parse_opencode_activity(matched_log)

            sessions.append({
                "pid": int(pid),
                "project": project,
                "activity": activity,
            })
    except Exception:
        pass
    return sessions


def _parse_claude_activity(debug_path):
    """Parse the tail of a Claude debug file to determine current activity."""
    try:
        fsize = os.path.getsize(debug_path)
        with open(debug_path, "r", errors="replace") as f:
            f.seek(max(0, fsize - 3000))
            tail = f.read()

        lines = tail.strip().split("\n")

        # Walk backwards to find the most recent meaningful activity
        tool_map = {
            "Bash": "running cmd",
            "Read": "reading",
            "Edit": "editing",
            "Write": "writing",
            "Grep": "searching",
            "Glob": "finding files",
            "Task": "delegating",
            "WebFetch": "fetching web",
            "WebSearch": "web search",
            "AskUserQuestion": "waiting for user",
            "mcp__custodian": "using MCP",
        }

        for line in reversed(lines):
            # Tool use detection
            if "PreToolUse with query:" in line:
                tool = line.split("PreToolUse with query:")[-1].strip()
                for key, label in tool_map.items():
                    if tool.startswith(key):
                        return label
                return f"using {tool}"

            # Streaming = actively generating
            if "Stream started" in line:
                return "thinking"

            # Autocompact
            if "autocompact:" in line:
                return "thinking"

            # Waiting for input
            if "Waiting for user input" in line or "waitForPermission" in line:
                return "waiting"

        return "active"
    except Exception:
        return "active"


def _parse_opencode_activity(log_path):
    """Best-effort parse of an OpenCode log tail for current activity."""
    try:
        fsize = os.path.getsize(log_path)
        with open(log_path, "r", errors="replace") as f:
            f.seek(max(0, fsize - 4000))
            tail = f.read()

        lines = tail.strip().split("\n")
        for line in reversed(lines):
            if "dangerously-skip-permissions" in line or "permission requested" in line:
                return "waiting"
            if "service=server method=POST" in line or "path=/session/chat" in line:
                return "thinking"
            if "service=mcp" in line:
                return "using MCP"
            if "service=file.watcher" in line or "service=vcs" in line:
                return "active"
        return "active"
    except Exception:
        return "active"


WATCHDOG_HEALTH_FILE = "/tmp/watchdog-health.json"


def _read_watchdog_health():
    """Read watchdog health file. Returns None if watchdog not running."""
    try:
        if os.path.isfile(WATCHDOG_HEALTH_FILE):
            with open(WATCHDOG_HEALTH_FILE, "r") as f:
                data = json.load(f)
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

    if watchdog:
        sshd = watchdog.get("sshd", {})
        sshd_status = sshd.get("status", "unknown")
        services["sshd"] = sshd_status
        if sshd_status not in ("ok", "recovered"):
            overall = "unhealthy"
    else:
        services["sshd"] = "unknown"

    if watchdog:
        docker = watchdog.get("docker", {})
        docker_status = docker.get("status", "unknown")
        services["docker"] = docker_status
        if docker_status not in ("ok", "recovered"):
            overall = "unhealthy"
    else:
        services["docker"] = "unknown"

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


def _get_ticker_settings():
    """Get overlay settings from ticker_settings table."""
    defaults = {
        "scroll_speed": "50", "opacity": "85", "bar_height": "28",
        "bg_color": "#1e1e2e", "text_color": "#cdd6f4",
        "position": "top", "poll_interval": "3",
    }
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ticker_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        rows = conn.execute("SELECT key, value FROM ticker_settings").fetchall()
        conn.close()
        if rows:
            for r in rows:
                defaults[r["key"]] = r["value"]
    except Exception:
        pass
    return defaults


def _save_ticker_settings(data):
    """Save overlay settings to ticker_settings table."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ticker_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for k, v in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO ticker_settings (key, value) VALUES (?, ?)",
                (str(k), str(v)),
            )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


AUTHORIZED_KEYS_PATH = os.path.expanduser("~/.ssh/authorized_keys")
SETUP_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "setup-device")


def _generate_pairing_code():
    """Generate a pairing code (NAI-XXXX) and store it with 10-min expiry."""
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace("O", "").replace("0", "").replace("I", "").replace("1", "").replace("L", "")
    suffix = "".join(secrets.choice(chars) for _ in range(4))
    code = f"NAI-{suffix}"
    expires = (datetime.utcnow() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pairing_codes "
            "(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, "
            "used_by_device_id INTEGER, status TEXT DEFAULT 'pending')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS devices "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, hostname TEXT, "
            "tailscale_ip TEXT, ssh_pubkey TEXT, ssh_fingerprint TEXT, "
            "paired_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT, "
            "status TEXT DEFAULT 'paired')"
        )
        conn.execute(
            "UPDATE pairing_codes SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at < datetime('now')"
        )
        conn.execute(
            "INSERT INTO pairing_codes (code, expires_at) VALUES (?, ?)",
            (code, expires),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return None, str(e)
    return code, expires


def _validate_and_pair(data):
    """Validate a pairing code and register the device."""
    code = data.get("code", "").strip().upper()
    name = data.get("name", "").strip()
    hostname = data.get("hostname", "").strip()
    ssh_pubkey = data.get("ssh_pubkey", "").strip()
    tailscale_ip = data.get("tailscale_ip", "").strip()

    if not code or not name or not ssh_pubkey:
        return {"error": "Missing required fields: code, name, ssh_pubkey"}, 400

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM pairing_codes WHERE code = ?", (code,)
        ).fetchone()

        if not row:
            conn.close()
            return {"error": "Invalid pairing code"}, 403

        if row["status"] != "pending":
            conn.close()
            return {"error": f"Code already {row['status']}"}, 403

        if row["expires_at"] < datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"):
            conn.execute(
                "UPDATE pairing_codes SET status = 'expired' WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            conn.close()
            return {"error": "Code expired"}, 403

        fingerprint = ""
        try:
            proc = subprocess.run(
                ["ssh-keygen", "-lf", "-"],
                input=ssh_pubkey, capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                fingerprint = proc.stdout.strip().split()[1] if proc.stdout.strip() else ""
        except Exception:
            pass

        cur = conn.execute(
            "INSERT INTO devices (name, hostname, tailscale_ip, ssh_pubkey, ssh_fingerprint) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, hostname, tailscale_ip, ssh_pubkey, fingerprint),
        )
        device_id = cur.lastrowid

        conn.execute(
            "UPDATE pairing_codes SET status = 'used', used_by_device_id = ? WHERE id = ?",
            (device_id, row["id"]),
        )
        conn.commit()
        conn.close()

        os.makedirs(os.path.dirname(AUTHORIZED_KEYS_PATH), exist_ok=True)
        with open(AUTHORIZED_KEYS_PATH, "a") as f:
            key_line = ssh_pubkey if ssh_pubkey.endswith("\n") else ssh_pubkey + "\n"
            if not key_line.rstrip().endswith(name):
                key_line = key_line.rstrip() + f" # paired-device:{name}\n"
            f.write(key_line)

        return {"status": "paired", "device_id": device_id, "fingerprint": fingerprint}, 200

    except Exception as e:
        return {"error": str(e)}, 500


def _get_devices():
    """Get all devices from the DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS devices "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, hostname TEXT, "
            "tailscale_ip TEXT, ssh_pubkey TEXT, ssh_fingerprint TEXT, "
            "paired_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT, "
            "status TEXT DEFAULT 'paired')"
        )
        rows = conn.execute(
            "SELECT id, name, hostname, tailscale_ip, ssh_fingerprint, paired_at, last_seen, status "
            "FROM devices ORDER BY paired_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_tailscale_ip():
    """Get this machine's Tailscale IP."""
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return "100.95.20.98"  # fallback default


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

    def _send_json(self, data, status=200):
        body = json.dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/status':
            state = _get_sandbox_state()
            if state:
                self._send_json({
                    "status": "running",
                    "preview_url": self._rewrite_url(state.get("preview_url")),
                    "project": state.get("project_name"),
                    "command": state.get("command"),
                    "original_command": state.get("original_command"),
                    "port": state.get("port"),
                    "display_mode": state.get("display_mode", "terminal"),
                    "preview_type": state.get("preview_type"),
                    "container": state.get("container"),
                    "app_alive": state.get("app_alive", True),
                    "last_log": state.get("last_log", ""),
                })
            else:
                self._send_json({"status": "idle", "preview_url": None})

        elif path == '/api/logs':
            # Parse ?lines=N from query string
            qs = parse_qs(parsed.query)
            lines = int(qs.get("lines", ["100"])[0])
            lines = min(lines, 500)  # cap at 500
            state = _get_sandbox_state()
            container = state.get("container") if state else None
            log_lines = _get_container_logs(container, lines)
            self._send_json({"lines": log_lines, "count": len(log_lines)})

        elif path == '/api/workbench':
            self._send_json(_get_workbench_status())

        elif path == '/api/ticker-config':
            self._send_json(_get_ticker_config())

        elif path == '/api/ticker-settings':
            self._send_json(_get_ticker_settings())

        elif path == '/api/health':
            self._send_json(_get_health_summary())

        elif path == '/api/devices':
            self._send_json(_get_devices())

        elif path == '/ticker':
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(TICKER_HTML.encode())

        elif path == '/setup':
            pc_ip = _get_tailscale_ip()
            try:
                with open(SETUP_SCRIPT_PATH, "r") as f:
                    script = f.read()
                script = script.replace("__PC_TAILSCALE_IP__", pc_ip)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(script.encode())
            except FileNotFoundError:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Setup script not found")

        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(ROUTER_HTML.encode())

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        if self.path == '/api/pair/generate':
            code, expires = _generate_pairing_code()
            if code:
                self._send_json({"code": code, "expires_at": expires})
            else:
                self._send_json({"error": expires}, 500)

        elif self.path == '/api/pair':
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            result, status = _validate_and_pair(data)
            self._send_json(result, status)

        elif self.path == '/api/ticker-settings':
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            if _save_ticker_settings(data):
                self._send_json({"status": "saved"})
            else:
                self._send_json({"error": "Failed to save"}, 500)

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logging


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

def run_router(port=ROUTER_PORT):
    server = ReusableHTTPServer(("0.0.0.0", port), SandboxRouterHandler)
    print(f"Sandbox router running on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    run_router()
