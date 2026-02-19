# Laptop Setup — Arch Linux Remote Access

Connect your Arch Linux laptop to the NAI Workbench running on your Windows PC via Tailscale VPN + SSH. The PC is the single source of truth; the laptop is a thin client.

## Current Network Info

- **PC Tailscale IP**: `100.95.20.98`
- **Laptop Tailscale IP**: `100.79.63.10` (lamanna-arch)
- **SSH port**: `2222`
- **Penpot login**: `admin@local.dev` / `admin123`

## Prerequisites

- Windows PC running the NAI Workbench with Tailscale Windows app installed and signed in
- Arch Linux laptop with Tailscale installed and signed into the same account
- Both machines showing as "online" in Tailscale dashboard

## Step 1 — Install Wave Terminal

```bash
# AUR (recommended)
yay -S waveterm-bin

# Or download AppImage from https://www.waveterm.dev/download
```

## Step 2 — Verify Tailscale Connectivity

Tailscale should already be installed on the laptop (`lamanna-arch`). Verify:

```bash
tailscale status
# Should show desktop-q289nhk (the PC) as connected
```

If not running:
```bash
sudo systemctl enable --now tailscaled
sudo tailscale up
```

Test connectivity to the PC:
```bash
ping 100.95.20.98
```

## Step 3 — Set Up SSH Key Auth

Generate a key if you don't have one:
```bash
ssh-keygen -t ed25519 -C "lamanna-arch"
```

Display your public key:
```bash
cat ~/.ssh/id_ed25519.pub
```

Add it to the PC. You can either:

**Option A** — From the PC (in WSL or via Claude Code):
```bash
echo "PASTE_YOUR_PUBLIC_KEY_HERE" >> /home/dev/.ssh/authorized_keys
```

**Option B** — Use Tailscale SSH (if enabled) or have Claude Code on the PC do it.

Test the connection:
```bash
ssh -p 2222 dev@100.95.20.98
# Should connect without a password prompt
```

## Step 4 — Install Wave Configs

Clone the repo and configure:

```bash
PC_IP="100.95.20.98"

git clone https://github.com/MarkSmith2151996/NAI-Workbench.git /tmp/nai-workbench

mkdir -p ~/.config/waveterm

sed "s/TAILSCALE_IP/${PC_IP}/g" /tmp/nai-workbench/config/wave/widgets-laptop.json > ~/.config/waveterm/widgets.json
sed "s/TAILSCALE_IP/${PC_IP}/g" /tmp/nai-workbench/config/wave/connections-laptop.json > ~/.config/waveterm/connections.json

rm -rf /tmp/nai-workbench
```

## Step 5 — Launch Wave and Verify

```bash
waveterm
```

All 8 sidebar widgets should appear and work:

| Widget | Type | What it does |
|--------|------|-------------|
| **WSL** | SSH terminal | Shell on the PC |
| **Dashboard** | SSH terminal | Textual TUI dashboard |
| **Claude** | SSH terminal | Project picker → Claude CLI |
| **Test** | SSH terminal | Test pipeline |
| **Whiteboard** | Web (9001) | Penpot design tool |
| **Komodo** | Web (9090) | Docker/system dashboard |
| **Import** | SSH terminal | Clone repos into ~/projects |
| **VS Code** | Web (9091) | code-server IDE |

## Step 6 — Penpot Login

Open Whiteboard widget or navigate to `http://100.95.20.98:9001`:
- Email: `admin@local.dev`
- Password: `admin123`

## Troubleshooting

### Can't ping the PC via Tailscale
- Check both machines on same tailnet: `tailscale status` on both
- PC: verify Tailscale tray icon shows "Connected"
- Laptop: `sudo tailscale up` to re-authenticate

### SSH connection refused
- sshd may have stopped (WSL kills idle processes). On the PC run:
  ```
  wsl -d Ubuntu-24.04 -- bash -c "sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd"
  ```
- Or reboot and let the VBS startup script handle it

### SSH asks for password (pubkey not working)
- Verify your pubkey is in `/home/dev/.ssh/authorized_keys` on the PC
- Check permissions on PC: `chmod 700 /home/dev/.ssh && chmod 600 /home/dev/.ssh/authorized_keys`

### Web widgets won't load (Penpot, Komodo, VS Code)
- Test locally on PC first: `curl http://localhost:9001` — if that works, the service is up
- If localhost works but `100.95.20.98:9001` doesn't, Windows Firewall is blocking it:
  ```powershell
  # Run in PowerShell as Admin on the PC:
  New-NetFirewallRule -DisplayName "NAI Workbench" -Direction Inbound -LocalPort 2222,9001,9090,9091 -Protocol TCP -Action Allow
  ```

### Wave widgets show wrong IP / TAILSCALE_IP placeholder
- Check: `grep TAILSCALE_IP ~/.config/waveterm/widgets.json`
- If found, the sed didn't work. Re-run Step 4.

### Services not running after PC reboot
- The VBS startup script runs via Task Scheduler on boot
- If services are down, manually trigger: double-click `config/start-workbench.vbs` on the PC
