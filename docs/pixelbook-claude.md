# Pixelbook — Claude Code Workflow

Run Claude Code on a Pixelbook (or any ChromeOS device) against the NAI Workbench PC. The Pixelbook is a thin client — all code, tools, and state live on the PC. You SSH in, work, and pull updates with one command.

---

## Architecture

```
Pixelbook (ChromeOS)
  ├─ Tailscale Android app → VPN tunnel
  ├─ Linux container (Crostini) → terminal, git, ssh, Wave Terminal
  └─ ssh-widget → auto-reconnecting SSH to PC
          │
          ▼
Windows PC (100.95.20.98) → WSL2 Ubuntu
  ├─ Claude Code + 23 MCP tools (custodian, sandbox, agents, Penpot, laptop)
  ├─ All project repos at ~/projects/
  ├─ Admin TUI, Editor, Sandbox, Docker
  └─ Penpot, Komodo, code-server (web services)
```

**Key point**: Claude Code runs on the PC (inside WSL). You SSH into it from the Pixelbook. Your local repo clone is only for config files and the `ssh-widget` script.

---

## First-Time Setup

### 1. Enable Linux (Crostini)

Settings → Advanced → Developers → Turn on Linux development environment.

### 2. Install Tailscale

Install the **Tailscale Android app** from the Play Store and sign into the same account as the PC. The Linux container routes through it automatically.

Verify connectivity:
```bash
ping -c 1 100.95.20.98
```

If that doesn't work, check that Tailscale is connected in the Android app.

### 3. Install Prerequisites

```bash
# Git + SSH (usually pre-installed in Crostini)
sudo apt update && sudo apt install -y git openssh-client curl

# Node.js (for Claude Code)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

# Claude Code
npm install -g @anthropic-ai/claude-code

# Wave Terminal (optional — for sidebar widgets)
wget https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-amd64.deb
sudo dpkg -i waveterm-linux-amd64.deb
```

### 4. Run Device Setup

```bash
git clone https://github.com/MarkSmith2151996/NAI-Workbench.git ~/NAI-Workbench
bash ~/NAI-Workbench/bin/setup-device
```

This will:
- Detect ChromeOS/Crostini automatically
- Test Tailscale connectivity (without needing the CLI)
- Generate an SSH key and pair with the PC
- Install `ssh-widget` to `~/.local/bin/`
- Export `NAI_PC_IP` to your shell RC
- Configure Wave Terminal widgets and connections
- Run a quick connectivity check

### 5. Authenticate Claude Code (One-Time)

Claude Code needs browser auth once, **from the PC directly** (not over SSH):

Either:
- Ask someone at the PC to run `claude` in WSL and complete the browser flow
- Or use code-server (`http://100.95.20.98:9091`) to open a terminal and run `claude`

After that, Claude auth tokens at `/home/dev/.claude/` are reused by all SSH sessions.

---

## Update Wave Terminal

Wave can be janky on ChromeOS/Crostini. Updating often fixes rendering glitches, freezes, and widget issues.

### Quick Update (copy-paste this)

```bash
# Download latest .deb and install over existing
wget -q https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-amd64.deb -O /tmp/wave.deb && \
sudo dpkg -i /tmp/wave.deb && rm /tmp/wave.deb && \
echo "Wave updated — restart it now"
```

Then **fully quit Wave** (right-click tray → Quit, or `pkill -f waveterm`) and reopen it.

### If .deb Doesn't Work — Use AppImage

```bash
# Download latest AppImage
wget -q https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-x86_64.AppImage \
  -O ~/.local/bin/waveterm.AppImage && chmod +x ~/.local/bin/waveterm.AppImage
# Run it
~/.local/bin/waveterm.AppImage
```

### Check Your Version

Inside Wave, press `Ctrl+Shift+I` to open DevTools, then in the Console tab:
```
WOS.getVersion()
```
Or just: `dpkg -l | grep waveterm`

### Wave Jankiness on ChromeOS — Tips

- **Disable GPU acceleration** if you see rendering glitches:
  ```bash
  # Add to your ~/.bashrc or run before launching Wave
  export ELECTRON_DISABLE_GPU=1
  waveterm
  ```
- **Reduce open tabs/widgets** — each widget is its own webview, Crostini has limited RAM
- **Close and reopen Wave** instead of leaving it running for days — memory leaks accumulate
- **Don't use the built-in SSH client** — it's broken with Tailscale. Use `ssh-widget` in a local terminal block instead (this is already how our widgets are configured)
- **If Wave won't start**, clear its cache:
  ```bash
  rm -rf ~/.config/waveterm/Cache ~/.config/waveterm/GPUCache
  ```

---

## Daily Workflow

### Start a Claude Code Session

```bash
# Option A: Direct SSH (simplest)
ssh-widget

# Then on the PC:
cd ~/projects/YOUR_PROJECT
claude
```

```bash
# Option B: Use the Editor widget (picks project, manages sessions)
ssh-widget /home/dev/projects/nai-workbench/bin/editor-session
```

```bash
# Option C: Wave Terminal
# Just click the "Editor" sidebar widget — it uses ssh-widget automatically
```

### The ssh-widget Advantage

All SSH connections go through `ssh-widget`, which:
- **Auto-reconnects** when the connection drops (10s retry, matches watchdog cycle)
- **Detects dead connections** fast (`ServerAliveInterval=15s`)
- **Fails fast** on initial connect (`ConnectTimeout=5s`)
- Shows `[14:32:05] Reconnecting... (attempt 3)` so you know what's happening
- Does **not** retry on clean exit (Ctrl+D, `exit`)

If sshd crashes on the PC, the watchdog restarts it within 10 seconds, and ssh-widget reconnects automatically. You don't need to do anything.

---

## Pulling New Changes

When someone pushes changes to the repo (new scripts, config updates, tool improvements), here's how to pick them up on your Pixelbook.

### Update Your Local Clone

```bash
cd ~/NAI-Workbench && git pull
```

This updates:
- `bin/ssh-widget` — SSH retry wrapper
- `bin/setup-device` — device setup script
- `bin/workbench-check` — connectivity checker
- `config/wave/widgets-laptop.json` — Wave widget definitions
- Any new scripts or config files

### Apply Updated Wave Configs

If widget definitions changed:
```bash
PC_IP="${NAI_PC_IP:-100.95.20.98}"
sed "s/TAILSCALE_IP/${PC_IP}/g" ~/NAI-Workbench/config/wave/widgets-laptop.json \
    > ~/.config/waveterm/widgets.json
```
Then restart Wave Terminal.

### Update the PC-Side Code

The PC has its own clone at `/home/dev/projects/nai-workbench/`. To update it:

```bash
# From your Pixelbook
ssh-widget "cd ~/projects/nai-workbench && git pull"
```

Or from the Editor widget, press `U` to pull latest changes.

### One-Liner: Update Everything

```bash
cd ~/NAI-Workbench && git pull && \
PC_IP="${NAI_PC_IP:-100.95.20.98}" && \
sed "s/TAILSCALE_IP/${PC_IP}/g" config/wave/widgets-laptop.json > ~/.config/waveterm/widgets.json && \
ssh-widget "cd ~/projects/nai-workbench && git pull" && \
echo "Done — restart Wave to pick up widget changes"
```

### One-Liner: Update Everything + Wave

```bash
cd ~/NAI-Workbench && git pull && \
wget -q https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-amd64.deb -O /tmp/wave.deb && \
sudo dpkg -i /tmp/wave.deb && rm /tmp/wave.deb && \
PC_IP="${NAI_PC_IP:-100.95.20.98}" && \
sed "s/TAILSCALE_IP/${PC_IP}/g" config/wave/widgets-laptop.json > ~/.config/waveterm/widgets.json && \
ssh-widget "cd ~/projects/nai-workbench && git pull" && \
echo "All updated — quit and reopen Wave now"
```

---

## What Lives Where

| Location | What | When to Update |
|----------|------|---------------|
| `~/NAI-Workbench/` (Pixelbook) | Local clone — ssh-widget, configs, docs | `git pull` |
| `~/.config/waveterm/widgets.json` (Pixelbook) | Active Wave config (generated from template) | After widget template changes |
| `~/.local/bin/ssh-widget` (Pixelbook) | Symlink → `~/NAI-Workbench/bin/ssh-widget` | Auto-updates with `git pull` |
| `/home/dev/projects/nai-workbench/` (PC) | PC-side code — Admin TUI, MCP server, custodian | `git pull` on PC |
| `/home/dev/.claude/` (PC) | Claude auth tokens, MCP config | Rarely changes |
| `/home/dev/.custodian-venv/` (PC) | Python venv for custodian tools | After dependency changes |

### What Does NOT Need Updating on the Pixelbook

- **Claude Code itself** — runs on the PC, not locally
- **MCP tools** — server runs on the PC
- **Project repos** — live on the PC at `~/projects/`
- **Docker containers** — managed on the PC
- **Custodian DB, fossils, agents** — all PC-side

---

## MCP Tools Available in Claude Sessions

When you SSH in and run `claude` in a project directory, you get 23 MCP tools:

| Category | Tools | What They Do |
|----------|-------|-------------|
| **Custodian** | `list_projects`, `get_project_fossil`, `lookup_symbol`, `get_symbol_context`, `find_related_files`, `get_recent_changes`, `get_detective_insights`, `trigger_custodian`, `request_reindex` | Query project architecture, symbols, and AI analysis |
| **Sandbox** | `sandbox_start/stop/restart/status/logs/test/install/exec` | Run and test projects in Docker containers |
| **Penpot** | `penpot_list_projects`, `penpot_get_page`, `penpot_export_svg` | Access wireframes and designs |
| **Agents** | `agent_list/create/update/delete/run/runs` | Create and run persistent AI agents |
| **Laptop** | `laptop_read/write/edit_file`, `laptop_run_command`, `laptop_glob/grep`, `laptop_list_dir/system_info`, `laptop_download_file` | Remote file access on connected laptops |

### Verify MCP is Working

```bash
ssh-widget
cd ~/projects/nai-workbench
claude
# Then ask: "list my projects"
```

If MCP tools aren't available, register the server:
```bash
claude mcp add-json --scope user custodian \
  '{"command":"/home/dev/.custodian-venv/bin/python3","args":["/home/dev/projects/nai-workbench/custodian/mcp_server.py"],"env":{"PYTHONPATH":"/home/dev/projects/nai-workbench/custodian"}}'
```

---

## Wireframes → Claude Workflow

Draw wireframes in the **Penpot** sidebar widget, then Claude can see them directly via MCP tools. No screenshots or file uploads needed.

### How It Works

1. Open the **Penpot** widget in Wave (or browse to `http://PC_IP:9001`)
2. Log in (`admin@local.dev` / `admin123`)
3. Create or edit a wireframe
4. In your Claude session, ask Claude to look at it:

```
"Look at my wireframe in Penpot for the login page"
"Export the SVG from Penpot and use it to build the component"
```

Claude uses these MCP tools behind the scenes:
- `penpot_list_projects` — lists all your Penpot projects/files
- `penpot_get_page(file_id)` — reads component names, layout frames, text content
- `penpot_export_svg(file_id)` — exports the page as SVG (Claude reads SVG as XML to understand layouts)

### Tips
- Name your Penpot frames and components clearly (e.g., "LoginForm", "Sidebar", "ProductCard") — Claude uses these names to understand your design
- One page per screen/view makes it easiest for Claude to parse
- Claude can read SVG structure (rectangles, text, layout) and translate it into real UI code

---

## Wave Terminal Sidebar Widgets

After setup, Wave shows these sidebar buttons:

| Widget | Type | What It Does |
|--------|------|-------------|
| **Admin** | Terminal (SSH) | Admin TUI — 8 tabs for project management, fossils, agents, sandbox |
| **Editor** | Terminal (SSH) | Project picker → Claude Code with persistent sessions |
| **Sandbox** | Web | Live preview of running sandbox apps (`http://PC_IP:7777`) |
| **Penpot** | Web | Wireframes and design — Claude can read these via MCP |
| **Notes** | Terminal (SSH) | Persistent sticky notes |
| **Terminal** | Terminal (SSH) | Raw shell on the PC |

All terminal widgets use `ssh-widget` and auto-reconnect on disconnect.

---

## Troubleshooting

### Can't reach the PC (`ping` fails)
- Open the Tailscale Android app — is it connected?
- Toggle Tailscale off and on
- Check that the PC shows as online in the Tailscale admin console

### SSH connection refused
- The watchdog auto-restarts sshd within 10 seconds — wait and retry
- If persistent: `ssh-widget "sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd"`

### ssh-widget: command not found
```bash
# Ensure ~/.local/bin is in PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Recreate symlink
ln -sf ~/NAI-Workbench/bin/ssh-widget ~/.local/bin/ssh-widget
```

### Web widgets (Sandbox, Penpot) don't load
- These use direct HTTP to the PC IP — verify with `curl http://100.95.20.98:7777`
- If unreachable after PC reboot, port proxy rules may need updating. The VBS startup script now handles this automatically, but if it didn't run: ask someone at the PC to double-click `config/start-workbench.vbs`

### Claude Code says "no MCP tools"
```bash
# Check MCP registration
ssh-widget "claude mcp list"

# If custodian is missing, re-register
ssh-widget "claude mcp add-json --scope user custodian '{\"command\":\"/home/dev/.custodian-venv/bin/python3\",\"args\":[\"/home/dev/projects/nai-workbench/custodian/mcp_server.py\"],\"env\":{\"PYTHONPATH\":\"/home/dev/projects/nai-workbench/custodian\"}}'"
```

### Wave Terminal won't install on ChromeOS
- Ensure Linux (Crostini) is enabled and updated
- Try the AppImage instead of the .deb:
  ```bash
  wget https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-x86_64.AppImage
  chmod +x waveterm-linux-x86_64.AppImage
  ./waveterm-linux-x86_64.AppImage
  ```

### `git pull` shows conflicts
```bash
cd ~/NAI-Workbench
git stash        # save local changes
git pull         # pull latest
git stash pop    # reapply local changes (resolve conflicts if any)
```

### Everything is broken — full reset
```bash
# Re-run device setup from scratch
cd ~/NAI-Workbench && git pull
bash bin/setup-device
```

---

## Quick Reference

```bash
# Connect to PC
ssh-widget

# Update local repo
cd ~/NAI-Workbench && git pull

# Update PC repo
ssh-widget "cd ~/projects/nai-workbench && git pull"

# Refresh Wave widgets
sed "s/TAILSCALE_IP/${NAI_PC_IP}/g" ~/NAI-Workbench/config/wave/widgets-laptop.json > ~/.config/waveterm/widgets.json

# Check connectivity
bash ~/NAI-Workbench/bin/workbench-check

# Quick connectivity check
bash ~/NAI-Workbench/bin/workbench-check --quick

# Start Claude on a project
ssh-widget "cd ~/projects/MY_PROJECT && claude"

# Use the Editor (project picker + sessions)
ssh-widget /home/dev/projects/nai-workbench/bin/editor-session

# Run the Admin TUI
ssh-widget /home/dev/projects/nai-workbench/bin/admin-session
```
