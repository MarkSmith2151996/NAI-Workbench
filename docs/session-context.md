# NAI Workbench — Session Context

## Purpose
This document captures key decisions, discoveries, and state from build sessions. It serves as a handoff between Claude sessions so context isn't lost. Update this document as the workbench evolves.

## Project Identity
NAI Workbench is a persistent dev environment running on a Windows PC (WSL2 + Docker) accessible from anywhere via Wave Terminal and Tailscale VPN. It bundles Claude AI, a TUI dashboard, Penpot design, Komodo ops, and VS Code into one interface — a portable command center for building all other projects.

## Build History

### Phase 12 — Remote Access + Penpot (Feb 18-19, 2026)
**What was built:**
- Replaced Excalidraw (port 9081) with Penpot (port 9001) — 5-container Docker stack
- Set up Tailscale VPN for laptop access (Windows service, NOT WSL)
- Set up SSH on port 2222 (external) / 2223 (internal) with pubkey-only auth
- Created laptop Wave Terminal configs with `ssh -t` commands (bypasses broken Wave SSH)
- Created operations runbook with every restart command and known issue
- Enhanced project picker (`bin/claude-session`) with GitHub repo browsing

**Key decisions made:**
1. **Tailscale on Windows, not WSL** — WSL Tailscale showed "Active: False", unreliable. Windows service is always-on.
2. **NAT mode, not mirrored networking** — Mirrored (`networkingMode=mirrored` in .wslconfig) causes port conflicts with netsh portproxy and Docker. Reverted to NAT.
3. **Port proxy with localhost exception for sshd** — Port proxy binds 0.0.0.0:PORT which blocks sshd from binding the same port. Solution: sshd listens on 127.0.0.1:2223, proxy forwards 2222→localhost:2223. Other services (Docker) bind to 0.0.0.0 on the WSL NAT IP, no conflict.
4. **Wave `ssh -t` instead of Wave SSH client** — Wave 0.14's built-in SSH (`connection: "ssh://..."`) fails over Tailscale. Laptop widgets use `controller: "cmd"` with `ssh -t -p 2222 dev@100.95.20.98 <command>` in local terminals.
5. **Penpot admin via HTTP API** — `python3 -m app.cli` and PREPL both failed in Penpot 2.13. Created account by temporarily enabling registration, using the HTTP API (prepare-register-profile + register-profile), then disabling registration.
6. **Secure install.sh** — User chose to refactor all `curl | bash` patterns to download-then-execute (Semgrep pre-commit hook caught them). More secure, user's explicit preference.
7. **Git push via WSL** — WSL git uses Windows `gh.exe` as credential helper. Works after configuring: `git config --global credential.helper "/mnt/c/Program Files/GitHub CLI/gh.exe auth git-credential"`

**Problems solved (for future reference):**
- Penpot port: internal nginx is 8080, not 80 → compose mapping 9001:8080
- Penpot exporter crash: needed `env_file: compose.env` for PENPOT_SECRET_KEY
- `/run/sshd` disappears every WSL restart → must mkdir before starting sshd
- SSH host keys missing on fresh installs → `sudo ssh-keygen -A`
- Docker containers lose port bindings → `docker compose up -d --force-recreate`
- CRLF line endings on scripts → `sed -i 's/\r$//'`
- Claude Code auth: must run `claude` once directly on PC (not via SSH) for browser OAuth

## Current State (Feb 19, 2026)

### Services Running
| Service | Port (ext) | Port (int) | Status |
|---------|-----------|-----------|--------|
| sshd | 2222 | 2223 | Running, pubkey auth |
| Penpot | 9001 | 8080 | Running, 5 containers |
| Komodo | 9090 | 9120 | Running, 3 containers |
| code-server | 9091 | 9091 | Running, systemd |
| Tailscale | — | — | Windows service, always on |

### Network
- PC Tailscale: 100.95.20.98
- Laptop Tailscale: 100.79.63.10 (lamanna-arch)
- WSL NAT IP: 172.21.37.202 (may change)
- Windows Firewall: "NAI Workbench" rule allows 2222,9001,9090,9091
- Port proxy: persistent netsh rules

### Laptop Access
- Wave Terminal installed on Arch laptop with `--no-sandbox` flag
- All 8 widgets configured: 5 SSH terminal + 3 web
- SSH terminals use `ssh -t` commands (not Wave SSH client)
- Web widgets (Penpot, Komodo, VS Code) load via Tailscale IP directly

### Pending / TODO
- Claude Code not yet authenticated in WSL (needs browser auth on PC)
- Old excalidraw container still running (`docker rm -f excalidraw-canvas` to clean up)
- VBS startup script needs update: sshd should use port 2223 and ListenAddress 127.0.0.1
- Port proxy for sshd uses 127.0.0.1 (reboot-safe), but 9001/9090/9091 use WSL NAT IP (may need updating after reboot)
- Consider a startup script that auto-detects WSL IP and sets port proxy rules

## Key Files Changed This Session
- `config/penpot/compose.yaml` — Created (5-container Penpot stack)
- `config/penpot/compose.env` — Created (gitignored, has real secrets)
- `config/penpot/compose.env.template` — Created (committed template)
- `config/wave/widgets-laptop.json` — Created, then rewritten (ssh -t approach)
- `config/wave/connections-laptop.json` — Created (may not be needed with ssh -t approach)
- `config/start-workbench.vbs` — Modified (added sshd, Penpot, removed excalidraw)
- `install.sh` — Modified (added SSH setup, refactored curl|bash)
- `bin/claude-session` — Enhanced (GitHub repo browsing, URL cloning)
- `dashboard/dashboard.py` — Modified (Excalidraw→Penpot)
- `CLAUDE.md` — Updated (remote access, Penpot, networking)
- `docs/laptop-setup.md` — Created (hardcoded real IPs)
- `docs/operations.md` — Created (full ops runbook)
- `.gitignore` — Created

## GitHub Commits This Session
1. `c52d84d` — Phase 9-12: Dashboard, test pipeline, Claude picker, remote access, Penpot
2. `220369c` — Move Tailscale from WSL to Windows
3. `7794fcf` — Update laptop setup docs with real Tailscale IPs
4. `e3b2a6d` — Fix laptop widgets: use direct SSH instead of Wave SSH client
5. `9f30127` — Enhance project picker: add GitHub repo browsing and URL cloning

## Evolution Notes
This document should be updated when:
- New services are added to the workbench
- Networking/port configuration changes
- New known issues are discovered and fixed
- Major features are added to any component
- The workbench is used to build a new project (add it to the project list)
