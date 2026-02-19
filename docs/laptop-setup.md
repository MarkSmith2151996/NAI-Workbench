# Laptop Setup — Arch Linux Remote Access

Connect your Arch Linux laptop to the NAI Workbench running on your Windows PC via Tailscale VPN + SSH. The PC is the single source of truth; the laptop is a thin client.

## Prerequisites

- Windows PC running the NAI Workbench (install.sh already run)
- **Tailscale installed on Windows** (not WSL) — https://tailscale.com/download/windows
- Arch Linux laptop with internet access
- Both machines signed into the same Tailscale account

## 1. Install Wave Terminal

```bash
# AUR (recommended)
yay -S waveterm-bin

# Or download AppImage from https://www.waveterm.dev/download
```

## 2. Install and Start Tailscale

```bash
yay -S tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up
# Follow the browser auth link — sign into the same account as the PC
```

## 3. Get the PC's Tailscale IP

On the **Windows PC**, open PowerShell:

```powershell
tailscale ip -4
# Returns something like 100.87.241.23
```

Or check the Tailscale system tray icon → "My devices" to find the IP.

Note this IP — you'll need it for all configs below.

## 4. Set Up SSH Key Auth

On the **laptop**:

```bash
# Generate a key if you don't have one
ssh-keygen -t ed25519 -C "laptop"

# Display your public key
cat ~/.ssh/id_ed25519.pub
```

On the **PC** (in WSL), add the laptop's public key:

```bash
echo "ssh-ed25519 AAAA... laptop" >> /home/dev/.ssh/authorized_keys
```

Test the connection from the laptop:

```bash
ssh -p 2222 dev@100.87.241.23
# Should connect without a password prompt
```

## 5. Copy and Configure Wave Configs

From the **laptop**, copy the configs from the PC via SCP:

```bash
PC_IP="100.87.241.23"  # Replace with actual Tailscale IP

# Create Wave config directory
mkdir -p ~/.config/waveterm

# Copy laptop-specific configs
scp -P 2222 dev@${PC_IP}:/home/dev/projects/nai-workbench/config/wave/widgets-laptop.json /tmp/widgets.json
scp -P 2222 dev@${PC_IP}:/home/dev/projects/nai-workbench/config/wave/connections-laptop.json /tmp/connections.json

# Replace TAILSCALE_IP placeholder with actual IP
sed -i "s/TAILSCALE_IP/${PC_IP}/g" /tmp/widgets.json
sed -i "s/TAILSCALE_IP/${PC_IP}/g" /tmp/connections.json

# Install into Wave config
cp /tmp/widgets.json ~/.config/waveterm/widgets.json
cp /tmp/connections.json ~/.config/waveterm/connections.json
```

Alternatively, clone the repo and sed locally:

```bash
git clone https://github.com/MarkSmith2151996/NAI-Workbench.git /tmp/nai-workbench
sed "s/TAILSCALE_IP/${PC_IP}/g" /tmp/nai-workbench/config/wave/widgets-laptop.json > ~/.config/waveterm/widgets.json
sed "s/TAILSCALE_IP/${PC_IP}/g" /tmp/nai-workbench/config/wave/connections-laptop.json > ~/.config/waveterm/connections.json
```

## 6. Launch Wave and Verify

```bash
waveterm
```

All 8 sidebar widgets should work:
- **WSL** — SSH terminal to the PC
- **Dashboard** — Textual TUI dashboard (runs on PC)
- **Claude** — Project picker → Claude CLI (runs on PC)
- **Test** — Test pipeline (runs on PC)
- **Whiteboard** — Penpot design tool (web UI from PC)
- **Komodo** — Docker/system dashboard (web UI from PC)
- **Import** — Clone repos into PC's ~/projects
- **VS Code** — code-server (web UI from PC)

## 7. Penpot Login

Penpot account is already created on the PC. Open `http://100.87.241.23:9001` from the laptop and log in with:
- Email: `admin@local.dev`
- Password: `admin123`

## Troubleshooting

### SSH connection refused
- Verify sshd is running on the PC: `wsl -d Ubuntu-24.04 -- bash -c "ps aux | grep sshd"`
- Check it's listening on 2222: `wsl -d Ubuntu-24.04 -- bash -c "ss -tlnp | grep 2222"`
- Re-start sshd: `wsl -d Ubuntu-24.04 -- bash -c "sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd"`

### Tailscale not routing
- Check both machines are on the same tailnet: `tailscale status` (on both machines)
- On the PC, check via PowerShell: `tailscale status`
- Verify Tailscale Windows service is running: check system tray icon
- Re-authenticate if needed: `tailscale up` (PowerShell as admin)

### Penpot shows blank page
- Check all 5 containers are running on PC: `docker compose -p penpot ps`
- Check backend logs: `docker logs penpot-penpot-backend-1`
- Ensure compose.env has real values (not CHANGE_ME placeholders)

### SSH auth fails (password prompt)
- Verify your pubkey is in `/home/dev/.ssh/authorized_keys` on the PC
- Check permissions: `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys` (on PC in WSL)
- Check sshd config: `cat /etc/ssh/sshd_config.d/workbench.conf`

### Wave widgets not loading
- Verify `sed` replaced all `TAILSCALE_IP` instances: `grep TAILSCALE_IP ~/.config/waveterm/widgets.json`
- Check the PC's Tailscale IP hasn't changed: `tailscale ip -4` (PowerShell on PC)
- Restart Wave after config changes

### WSL ports not reachable via Tailscale IP
- WSL2 auto-forwards ports to Windows. Verify locally on the PC first: `curl http://localhost:9001`
- If localhost works but Tailscale IP doesn't, check Windows Firewall — allow inbound on ports 2222, 9001, 9090, 9091
