# Remote Device Setup — Connect Any Device to the NAI Workbench

Set up a new device (Pixelbook, laptop, second PC, etc.) to access the NAI Workbench running on the Windows PC via Tailscale VPN + SSH + Wave Terminal.

The PC is the single source of truth — remote devices are thin clients that SSH into it.

## Network Info

- **PC Tailscale IP**: `100.95.20.98`
- **SSH port**: `2222` (external) → `127.0.0.1:2223` (WSL sshd)
- **Penpot login**: `admin@local.dev` / `admin123`
- **GitHub repo**: `https://github.com/MarkSmith2151996/NAI-Workbench.git`

## What You Get

After setup, the device has:
- **Wave Terminal** with sidebar widgets (Admin TUI, Editor, Penpot, Sandbox, Terminal)
- **Claude Code** with full MCP tool access (23 tools — fossils, sandbox, agents, Penpot, laptop bridge)
- **`workbench-check`** script for connectivity diagnostics
- Access to all web services (Penpot :9001, Komodo :9090, code-server :9091, Sandbox :7777)

---

## Step 1 — Install Prerequisites

### Tailscale

Tailscale creates a private VPN tunnel to the PC. Install for your platform:

**ChromeOS (Pixelbook):**
```bash
# Option A: Android app (easiest)
# Install "Tailscale" from the Google Play Store

# Option B: Linux container (if Crostini/Linux is enabled)
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up
```

**Arch Linux:**
```bash
sudo pacman -S tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up
```

**Debian/Ubuntu:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up
```

**macOS:**
```bash
# Install from Mac App Store or:
brew install tailscale
```

Verify the PC is reachable:
```bash
ping 100.95.20.98
# Should respond
```

### Node.js (for Claude Code)

```bash
# Arch
sudo pacman -S nodejs npm

# Debian/Ubuntu
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

# ChromeOS Linux container
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

# macOS
brew install node
```

### Claude Code

```bash
npm install -g @anthropic-ai/claude-code
```

Authenticate (run once — needs a browser):
```bash
claude
# Follow the browser auth flow
```

### Wave Terminal

```bash
# Arch (AUR)
yay -S waveterm-bin

# Debian/Ubuntu — download .deb from https://www.waveterm.dev/download
wget https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-amd64.deb
sudo dpkg -i waveterm-linux-amd64.deb

# ChromeOS Linux container — same as Debian
wget https://github.com/wavetermdev/waveterm/releases/latest/download/waveterm-linux-amd64.deb
sudo dpkg -i waveterm-linux-amd64.deb

# macOS — download .dmg from https://www.waveterm.dev/download
```

---

## Step 2 — SSH Key Setup

Generate a key if you don't have one:
```bash
ssh-keygen -t ed25519 -C "$(hostname)"
```

Display your public key:
```bash
cat ~/.ssh/id_ed25519.pub
```

Add it to the PC. Pick one method:

**Option A — From the PC** (paste in WSL terminal or have Claude Code do it):
```bash
echo "PASTE_YOUR_PUBLIC_KEY_HERE" >> /home/dev/.ssh/authorized_keys
```

**Option B — If you already have SSH access to the PC from another device:**
```bash
ssh -p 2222 dev@100.95.20.98 'cat >> ~/.ssh/authorized_keys' < ~/.ssh/id_ed25519.pub
```

Test the connection:
```bash
ssh -p 2222 dev@100.95.20.98 echo "Connected!"
# Should print "Connected!" with no password prompt
```

---

## Step 3 — Clone Repo and Configure

```bash
# Clone the workbench repo
git clone https://github.com/MarkSmith2151996/NAI-Workbench.git ~/NAI-Workbench

# Install Wave widget configs
PC_IP="100.95.20.98"
mkdir -p ~/.config/waveterm

sed "s/TAILSCALE_IP/${PC_IP}/g" ~/NAI-Workbench/config/wave/widgets-laptop.json \
    > ~/.config/waveterm/widgets.json

sed "s/TAILSCALE_IP/${PC_IP}/g" ~/NAI-Workbench/config/wave/connections-laptop.json \
    > ~/.config/waveterm/connections.json

# Make scripts executable
chmod +x ~/NAI-Workbench/bin/*

# Add workbench-check to PATH
mkdir -p ~/.local/bin
ln -sf ~/NAI-Workbench/bin/workbench-check ~/.local/bin/workbench-check
```

---

## Step 4 — Register Custodian MCP Server (One-Time)

This gives Claude Code access to 23 MCP tools (fossils, sandbox, agents, Penpot, etc.):

```bash
# SSH into the PC
ssh -p 2222 dev@100.95.20.98

# Register custodian MCP at user scope
claude mcp add-json --scope user custodian \
  '{"command":"/home/dev/.custodian-venv/bin/python3","args":["/home/dev/projects/nai-workbench/custodian/mcp_server.py"],"env":{"PYTHONPATH":"/home/dev/projects/nai-workbench/custodian"}}'

# Verify
claude mcp list
# Should show "custodian" with 23 tools
```

---

## Step 5 — Verify Everything

### Run the connectivity checker:
```bash
bash ~/NAI-Workbench/bin/workbench-check
```

Expected output:
```
[1/5] Tailscale      ✓ UP
[2/5] Port checks    ✓ SSH, Sandbox, Penpot, Komodo, code-server
[3/5] Watchdog       ✓ healthy (sshd ok, docker ok)
[4/5] SSH            ✓ login successful
[5/5] Summary        All checks passed.
```

### Launch Wave Terminal:
```bash
waveterm
```

You should see sidebar widgets:

| Widget | Type | What it does |
|--------|------|-------------|
| **Admin** | SSH terminal | Admin TUI — 8 tabs (Projects, Custodian, Fossils, Detective, Status, Editor, Agent Factory, Alpha Builds) |
| **Editor** | SSH terminal | Project picker → Claude CLI with MCP tools |
| **Penpot** | Web (9001) | Design tool (wireframes) |
| **Sandbox** | Web (7777) | Live sandbox preview + status ticker |
| **Notes** | SSH terminal | Persistent sticky notes |
| **Terminal** | SSH terminal | Raw shell on the PC |

### Test Claude Code with MCP:
```bash
ssh -p 2222 dev@100.95.20.98
cd ~/projects/nai-workbench
claude
# Then try: "list my projects" or "list agents"
```

---

## Step 6 — Penpot Login

Open the Penpot widget or browse to `http://100.95.20.98:9001`:
- **Email**: `admin@local.dev`
- **Password**: `admin123`

---

## Troubleshooting

### Can't ping the PC via Tailscale
- Check both machines on same tailnet: `tailscale status`
- PC: verify Tailscale tray icon shows "Connected"
- Re-authenticate: `sudo tailscale up`

### SSH connection refused
- The watchdog daemon auto-restarts sshd within 10 seconds
- If it doesn't recover, run on the PC (PowerShell):
  ```
  wsl -d Ubuntu-24.04 -u root bash -c "mkdir -p /run/sshd && /usr/sbin/sshd"
  ```
- Check watchdog status: `curl http://100.95.20.98:7777/api/health`

### SSH asks for password
- Verify pubkey is in `/home/dev/.ssh/authorized_keys` on the PC
- Check permissions: `chmod 700 /home/dev/.ssh && chmod 600 /home/dev/.ssh/authorized_keys`

### Web widgets won't load
- Test from the device: `curl http://100.95.20.98:9001`
- If unreachable, Windows Firewall may be blocking:
  ```powershell
  # PowerShell as Admin on PC:
  New-NetFirewallRule -DisplayName "NAI Workbench" -Direction Inbound -LocalPort 2222,7777,9001,9090,9091 -Protocol TCP -Action Allow
  ```

### Wave widgets show TAILSCALE_IP placeholder
- The sed command in Step 3 didn't work. Re-run:
  ```bash
  sed "s/TAILSCALE_IP/100.95.20.98/g" ~/NAI-Workbench/config/wave/widgets-laptop.json > ~/.config/waveterm/widgets.json
  ```

### ChromeOS Linux container can't reach Tailscale
- If using the Android Tailscale app, the Linux container should route through it automatically
- If not, install Tailscale inside the Linux container directly
- Check: `curl -s http://100.95.20.98:7777/api/health`

### Services not running after PC reboot
- The watchdog auto-recovers sshd and Docker
- For web services, manually trigger on the PC: double-click `config/start-workbench.vbs`
