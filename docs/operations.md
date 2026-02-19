# NAI Workbench — Operations Runbook

This document is the definitive reference for operating, troubleshooting, and recovering the NAI Workbench environment. Written for a future Claude session or human operator who needs to fix something fast.

---

## Quick Reference

| Item | Value |
|------|-------|
| PC Tailscale IP | `100.95.20.98` |
| Laptop Tailscale IP | `100.79.63.10` (lamanna-arch) |
| WSL NAT IP | `172.21.37.202` (may change on reboot) |
| SSH external port | `2222` (from laptop/Tailscale) |
| SSH internal port | `2223` (sshd listens on `127.0.0.1:2223`) |
| Penpot credentials | `admin@local.dev` / `admin123` |
| Windows Firewall rule | "NAI Workbench" — allows TCP `2222,9001,9090,9091` inbound |
| WSL distro | `Ubuntu-24.04` |
| Wave Terminal | Installed at `%LOCALAPPDATA%\Programs\waveterm\Wave.exe` |
| VBS startup script | `config/start-workbench.vbs` (Task Scheduler on boot) |

### Port Proxy Rules (persistent via netsh)

| Listen Address | Listen Port | Connect Address | Connect Port |
|----------------|-------------|-----------------|--------------|
| `0.0.0.0` | `2222` | `127.0.0.1` | `2223` |
| `0.0.0.0` | `9001` | `172.21.37.202` | `9001` |
| `0.0.0.0` | `9090` | `172.21.37.202` | `9090` |
| `0.0.0.0` | `9091` | `172.21.37.202` | `9091` |

---

## Boot Sequence

On PC boot, a VBS script (`config/start-workbench.vbs`) runs via Windows Task Scheduler. It executes these steps in order, each waiting for the previous to finish:

1. **Docker + code-server** — `sudo systemctl start docker && sudo systemctl start code-server@dev` (code-server on port 9091)
2. **sshd on port 2223** — `sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd` (bound to `127.0.0.1:2223`)
3. **Komodo compose up** — `docker compose -p komodo` (port 9090)
4. **Penpot compose up** — `docker compose -p penpot` (port 9001, 5 containers: frontend, backend, exporter, postgres, redis)
5. **Wave Terminal** — launches the saved workspace (non-blocking, runs last)

**Not managed by the VBS script:**
- **Tailscale** runs as a Windows system service — always on, survives reboots, no manual management needed.
- **Port proxy rules** are persistent (`netsh interface portproxy`) — survive reboots.
- **Windows Firewall rule** is persistent — survives reboots.
- **Penpot containers** have `restart: unless-stopped` — Docker auto-starts them, but the VBS `up -d` ensures they exist on first boot or after `docker compose down`.

---

## Networking Architecture (The Reality)

This is the most important section. If something is unreachable from the laptop, understanding this chain is how you debug it.

### The Full Chain

```
Arch Laptop (100.79.63.10)
    │
    │  Tailscale VPN tunnel
    ▼
Windows PC (100.95.20.98)
    │
    │  Windows netsh portproxy (binds 0.0.0.0:PORT)
    ▼
WSL2 Ubuntu (172.21.37.202)
    │
    │  Service binds 0.0.0.0:PORT inside WSL
    ▼
Docker container / systemd service
```

### Why It Works This Way

**WSL2 runs in NAT mode** (NOT mirrored). This means:

- WSL gets its own virtual ethernet adapter with a private NAT IP (currently `172.21.37.202`, but this IP can change on WSL restart/reboot).
- Services inside WSL bind to `0.0.0.0` within the WSL network namespace.
- From Windows, these services are reachable at `172.21.37.202:PORT` — but NOT from outside the machine.
- **Windows `netsh interface portproxy`** bridges the gap: it listens on `0.0.0.0:PORT` on the Windows network stack and forwards TCP connections to the WSL NAT IP.
- **Tailscale** (running as a Windows service) makes the PC reachable at `100.95.20.98`. When the laptop connects to `100.95.20.98:PORT`, the traffic hits Windows, hits the port proxy rule, and gets forwarded to WSL.
- **Windows Firewall** must allow inbound TCP on these ports — the "NAI Workbench" rule handles this.

### The sshd Exception

sshd is special. The port proxy rule for SSH is:

```
0.0.0.0:2222 → 127.0.0.1:2223
```

NOT `172.21.37.202:2222`. Here is why:

- The port proxy binds `0.0.0.0:2222` on the Windows side.
- If sshd also tried to bind `0.0.0.0:2222` inside WSL, and WSL auto-forwarding was active, there would be a port conflict.
- Instead, sshd is configured to listen on `127.0.0.1:2223` (loopback only, non-standard port).
- The port proxy forwards `0.0.0.0:2222 → 127.0.0.1:2223`. Since `127.0.0.1` is shared between Windows and WSL (WSL's loopback is accessible from Windows), this works without needing the WSL NAT IP.
- **Benefit**: The SSH port proxy rule is immune to WSL IP changes.

### Why NOT Mirrored Networking

We tried `networkingMode=mirrored` in `.wslconfig`. Do not use it:

- Mirrored mode makes WSL share the Windows network stack — services bind directly on Windows interfaces.
- This conflicts with `netsh portproxy` rules (both try to bind the same ports on `0.0.0.0`).
- Docker also cannot bind ports properly in mirrored mode.
- We reverted. Leave `.wslconfig` without `networkingMode=mirrored`.

---

## Ports Map

| External Port | Internal Target | Service | Compose Project | Notes |
|---------------|-----------------|---------|-----------------|-------|
| `2222` | `127.0.0.1:2223` | sshd (OpenSSH) | N/A (systemd) | Immune to WSL IP changes |
| `9001` | `172.21.37.202:9001` | Penpot frontend | `penpot` | Maps `9001:8080` (nginx inside container listens on 8080) |
| `9090` | `172.21.37.202:9090` | Komodo dashboard | `komodo` | Docker/system health UI |
| `9091` | `172.21.37.202:9091` | code-server | N/A (systemd) | VS Code in browser |

---

## Restart Commands

### Full restart (after reboot or WSL crash)

Run these in **PowerShell** on the PC:

```powershell
# 1. Docker + code-server
wsl -d Ubuntu-24.04 -- bash -c "sudo systemctl start docker && sudo systemctl start code-server@dev"

# 2. sshd
wsl -d Ubuntu-24.04 -- bash -c "sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd"

# 3. Komodo
wsl -d Ubuntu-24.04 -- bash -c "docker compose -p komodo -f /home/dev/komodo/compose.yaml --env-file /home/dev/komodo/compose.env up -d"

# 4. Penpot
wsl -d Ubuntu-24.04 -- bash -c "docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d"
```

Or just double-click `config/start-workbench.vbs` on the PC — it runs all of the above plus launches Wave.

### Just sshd (most common fix — WSL kills idle processes)

```powershell
wsl -d Ubuntu-24.04 -- bash -c "sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd"
```

This is the single most common thing that breaks. WSL aggressively reclaims idle processes, and sshd is often the victim. If the laptop cannot SSH in, this is the first thing to try.

### Just Docker services (if containers stopped)

```powershell
# Start Docker daemon first
wsl -d Ubuntu-24.04 -- bash -c "sudo systemctl start docker"

# Then bring up compose stacks
wsl -d Ubuntu-24.04 -- bash -c "docker compose -p komodo -f /home/dev/komodo/compose.yaml --env-file /home/dev/komodo/compose.env up -d"
wsl -d Ubuntu-24.04 -- bash -c "docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d"
```

### Just code-server

```powershell
wsl -d Ubuntu-24.04 -- bash -c "sudo systemctl start code-server@dev"
```

### Port proxy rules (if WSL IP changed after reboot)

Run in **PowerShell as Administrator**:

```powershell
# Step 1: Check current WSL IP
wsl -d Ubuntu-24.04 -- bash -c "ip addr show eth0 | grep 'inet '"

# Step 2: Reset and recreate all rules (replace WSL_IP with the actual IP from step 1)
netsh interface portproxy reset
netsh interface portproxy add v4tov4 listenport=2222 listenaddress=0.0.0.0 connectport=2223 connectaddress=127.0.0.1
netsh interface portproxy add v4tov4 listenport=9001 listenaddress=0.0.0.0 connectport=9001 connectaddress=WSL_IP
netsh interface portproxy add v4tov4 listenport=9090 listenaddress=0.0.0.0 connectport=9090 connectaddress=WSL_IP
netsh interface portproxy add v4tov4 listenport=9091 listenaddress=0.0.0.0 connectport=9091 connectaddress=WSL_IP
```

**Note**: The `2222 → 127.0.0.1:2223` rule never needs updating (uses localhost, not WSL IP). The other three rules (`9001`, `9090`, `9091`) point to the WSL NAT IP and need updating if it changes.

### Verify port proxy rules are correct

```powershell
netsh interface portproxy show all
```

---

## Service Health Check

### From the PC (inside WSL)

```bash
# Check all listening ports
ss -tlnp | grep -E '2222|2223|9001|9090|9091'

# Check Docker containers
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# Check systemd services
systemctl is-active docker code-server@dev

# Check sshd process
pgrep -a sshd
```

### From the laptop (or any Tailscale peer)

```bash
# Quick port scan
for port in 2222 9090 9091 9001; do
  timeout 3 bash -c "echo >/dev/tcp/100.95.20.98/$port" 2>/dev/null && echo "Port $port: OPEN" || echo "Port $port: closed"
done
```

### Full status (from within WSL)

```bash
# If the workbench-status script is available:
~/projects/nai-workbench/bin/workbench-status

# Or studio-status for just services:
~/projects/nai-workbench/bin/studio-status
```

---

## Known Issues & Fixes

### /run/sshd disappears

**Symptom**: `ssh: connect to host ... port 2222: Connection refused` from the laptop.

**Cause**: WSL clears `/run` on restart. sshd refuses to start without `/run/sshd` existing.

**Fix**:
```powershell
wsl -d Ubuntu-24.04 -- bash -c "sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd"
```

**Frequency**: Very common. This is the #1 issue you will encounter.

---

### SSH host keys missing

**Symptom**: sshd fails to start even after creating `/run/sshd`. Error in logs about missing host keys.

**Cause**: Fresh WSL install or after a distro reset.

**Fix**:
```bash
sudo ssh-keygen -A
sudo mkdir -p /run/sshd
sudo /usr/sbin/sshd
```

---

### Docker containers running without port mappings

**Symptom**: `docker ps` shows containers running but no port mappings in the PORTS column. Services are unreachable from outside the container network.

**Cause**: Containers were started outside of compose (e.g., manually via `docker run`) or compose was run without the port flags / with a different compose file.

**Fix**:
```bash
docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d --force-recreate
```

The `--force-recreate` flag tears down and recreates containers, picking up the port mappings from the compose file.

---

### WSL IP changes on reboot

**Symptom**: Web services (Penpot, Komodo, code-server) unreachable from laptop, but SSH still works.

**Cause**: The WSL NAT IP (`172.21.x.x`) changed when WSL restarted. Port proxy rules for `9001`, `9090`, `9091` point to the old IP. The SSH rule (`2222 → 127.0.0.1:2223`) is unaffected because it uses localhost.

**Diagnosis**:
```powershell
# Check current WSL IP
wsl -d Ubuntu-24.04 -- bash -c "ip addr show eth0 | grep 'inet '"

# Check what portproxy thinks the IP is
netsh interface portproxy show all
```

**Fix**: See the "Port proxy rules" section under Restart Commands above.

---

### Mirrored networking — DO NOT USE

**Symptom**: Multiple port bind failures, Docker cannot start containers with port mappings, port proxy rules conflict.

**Cause**: `networkingMode=mirrored` in `~/.wslconfig` makes WSL share the Windows network stack. This conflicts with `netsh portproxy` rules because both try to bind the same ports on `0.0.0.0`.

**Fix**: Remove `networkingMode=mirrored` from `C:\Users\Big A\.wslconfig` (or delete the file if that is the only setting). Then restart WSL:
```powershell
wsl --shutdown
wsl -d Ubuntu-24.04
```

**Rule**: Never enable mirrored networking. The NAT + portproxy architecture is the correct setup.

---

### Wave SSH client broken with Tailscale

**Symptom**: Wave Terminal 0.14's built-in SSH connections (`connection: "ssh://..."`) fail over Tailscale tunnels. The `wsh` agent inside the remote session cannot communicate back to `wavesrv` on the laptop.

**Cause**: Wave's agent-based SSH integration does not work reliably over VPN tunnels.

**Fix**: Laptop widgets use `ssh -t` commands in local terminal blocks instead of Wave's native SSH connection type. The laptop Wave config (`config/wave/widgets-laptop.json`) is already set up this way.

---

### Penpot frontend port mismatch

**Symptom**: Penpot unreachable, or returns connection refused on port 9001.

**Cause**: Penpot's internal nginx listens on port **8080**, not port 80. The compose port mapping must be `9001:8080`.

**Verification**: Check `config/penpot/compose.yaml`:
```yaml
ports:
  - 9001:8080
```

If someone changed it to `9001:80`, that is wrong. Fix the compose file and recreate:
```bash
docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d --force-recreate
```

---

### Claude Code auth via SSH

**Symptom**: Running `claude` in an SSH session (from laptop) prompts for browser authentication and fails because there is no browser on the WSL side.

**Cause**: Claude CLI needs browser-based OAuth on first run. SSH sessions do not have access to a browser.

**Fix**: Run `claude` once directly inside WSL on the PC (not via SSH). This can be done from:
- A Wave Terminal pane using a `wsl://Ubuntu-24.04` connection (on the PC)
- A PowerShell window: `wsl -d Ubuntu-24.04 -- bash -c "claude"`

Complete the browser auth flow. Auth tokens are saved to `/home/dev/.claude/` and are reused by all subsequent sessions, including SSH sessions from the laptop.

---

### Penpot database issues / migrations

**Symptom**: Penpot fails to start after image update, backend logs show database migration errors.

**Fix**:
```bash
# Check backend logs
docker logs penpot-penpot-backend-1 --tail 50

# If migration issues, try restarting just the backend
docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml restart penpot-backend

# Nuclear option: wipe database and start fresh (DESTROYS ALL PENPOT DATA)
docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml down -v
docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d
# Then re-login with admin@local.dev / admin123
```

---

### WSL distro won't start / hangs

**Symptom**: `wsl -d Ubuntu-24.04` hangs or returns an error.

**Fix**:
```powershell
# Force shutdown
wsl --shutdown

# Wait 10 seconds, then restart
wsl -d Ubuntu-24.04
```

If that does not work, check Windows Event Viewer for Hyper-V / WSL errors.

---

## Penpot Details

| Component | Image | Purpose |
|-----------|-------|---------|
| `penpot-frontend` | `penpotapp/frontend:latest` | Nginx reverse proxy + SPA (port 8080 internally) |
| `penpot-backend` | `penpotapp/backend:latest` | Clojure API server (port 6060 internally) |
| `penpot-exporter` | `penpotapp/exporter:latest` | SVG/PDF export service |
| `penpot-postgres` | `postgres:15` | PostgreSQL database |
| `penpot-redis` | `redis:7` | Session/cache store |

- **Volumes**: `penpot-assets` (uploaded files), `penpot-db` (PostgreSQL data)
- **Network**: All 5 containers on the `penpot` Docker network
- **Env config**: `config/penpot/compose.env` (copy from `compose.env.template` for fresh installs)
- **Restart policy**: `unless-stopped` on all containers — Docker auto-restarts them
- **Compose file**: `/home/dev/projects/nai-workbench/config/penpot/compose.yaml`

---

## Key File Paths

| Path | Purpose |
|------|---------|
| `/home/dev/projects/nai-workbench/` | Main repo root |
| `/home/dev/projects/nai-workbench/config/start-workbench.vbs` | Windows boot script (Task Scheduler) |
| `/home/dev/projects/nai-workbench/config/penpot/compose.yaml` | Penpot Docker Compose |
| `/home/dev/projects/nai-workbench/config/penpot/compose.env` | Penpot secrets (not in git) |
| `/home/dev/komodo/compose.yaml` | Komodo Docker Compose |
| `/home/dev/komodo/compose.env` | Komodo secrets (not in git) |
| `/home/dev/projects/nai-workbench/config/wave/widgets.json` | Wave config — PC (source of truth) |
| `/home/dev/projects/nai-workbench/config/wave/widgets-laptop.json` | Wave config — laptop template |
| `/home/dev/projects/nai-workbench/config/wave/connections.json` | Wave connections — PC |
| `/home/dev/projects/nai-workbench/config/wave/connections-laptop.json` | Wave connections — laptop template |
| `/home/dev/projects/nai-workbench/dashboard/dashboard.py` | Textual TUI dashboard |
| `/home/dev/projects/nai-workbench/bin/` | Utility scripts (workbench-status, studio-status, etc.) |
| `/home/dev/projects/nai-workbench/install.sh` | Full WSL environment installer |
| `/home/dev/.claude/` | Claude CLI auth tokens + MCP config |
| `/home/dev/.config/code-server/config.yaml` | code-server configuration |
| `/etc/ssh/sshd_config.d/workbench.conf` | sshd config (port 2223, pubkey only) |
| `/home/dev/.ssh/authorized_keys` | SSH authorized keys for laptop access |

---

## Installed Software (via install.sh)

The installer (`install.sh`) provisions the WSL environment with:

- **System**: build-essential, curl, wget, git, jq, fzf, ripgrep, fd-find, tmux, unzip
- **Runtime**: Node.js 22 (via NodeSource), Python 3 + pip + venv
- **Docker**: Docker Engine + user added to docker group
- **code-server**: VS Code in browser (port 9091)
- **Claude Code**: `@anthropic-ai/claude-code` (global npm)
- **MCP servers**: repomix, memory, filesystem (global npm)
- **Security**: Trivy (binary), Semgrep (pip)
- **SSH**: OpenSSH server (port 2223, pubkey auth only, no root login)

---

## Emergency Procedures

### Everything is down — full recovery

1. Reboot the Windows PC.
2. Tailscale starts automatically (Windows service).
3. Task Scheduler runs `start-workbench.vbs` on login.
4. Wait 60 seconds for all services to initialize.
5. From the laptop, run the health check:
   ```bash
   for port in 2222 9090 9091 9001; do
     timeout 3 bash -c "echo >/dev/tcp/100.95.20.98/$port" 2>/dev/null && echo "Port $port: OPEN" || echo "Port $port: closed"
   done
   ```
6. If ports 9001/9090/9091 are closed but 2222 is open, the WSL IP probably changed. Update port proxy rules (see above).
7. If 2222 is also closed, sshd did not start. Run the sshd fix from PowerShell on the PC.

### Can access SSH but web services are down

```bash
# SSH in from laptop
ssh -p 2222 dev@100.95.20.98

# Check Docker
sudo systemctl status docker
docker ps

# Restart everything
sudo systemctl start docker
docker compose -p komodo -f /home/dev/komodo/compose.yaml --env-file /home/dev/komodo/compose.env up -d
docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d
sudo systemctl start code-server@dev
```

### Cannot access PC at all

1. Verify Tailscale on the laptop: `tailscale status` — the PC should show as online.
2. If the PC shows offline in Tailscale, the machine is either off or Tailscale service crashed. Need physical access.
3. If the PC shows online but all ports are closed, WSL may not be running. Need physical or remote desktop access to run `wsl -d Ubuntu-24.04` and the startup commands.

---

## Wave Terminal Widget Layout

### PC widgets (local access)

Use `wsl://Ubuntu-24.04` connections and `localhost` URLs.

### Laptop widgets (remote access via Tailscale)

Use `ssh -t` commands in local terminals and `http://100.95.20.98:PORT` for web widgets.

| Widget | Type | Connection |
|--------|------|------------|
| WSL | SSH terminal | `ssh -p 2222 dev@100.95.20.98` |
| Dashboard | SSH terminal | `ssh -t -p 2222 dev@100.95.20.98 launch-dashboard` |
| Claude | SSH terminal | `ssh -t -p 2222 dev@100.95.20.98 claude-session` |
| Test | SSH terminal | `ssh -t -p 2222 dev@100.95.20.98 test-project` |
| Whiteboard | Web widget | `http://100.95.20.98:9001` |
| Komodo | Web widget | `http://100.95.20.98:9090` |
| Import | SSH terminal | `ssh -t -p 2222 dev@100.95.20.98 import-project` |
| VS Code | Web widget | `http://100.95.20.98:9091` |
