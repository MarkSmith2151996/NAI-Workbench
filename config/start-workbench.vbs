Set WshShell = CreateObject("WScript.Shell")
' 1. Start Docker + code-server (port 9091)
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""sudo systemctl start docker && sudo systemctl start code-server@dev""", 0, True
' 2. Start sshd on port 2222
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd""", 0, True
' 3. Start Komodo dashboard (port 9090)
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""docker compose -p komodo -f /home/dev/komodo/compose.yaml --env-file /home/dev/komodo/compose.env up -d""", 0, True
' 4. Start Penpot (port 9001) — containers have restart:unless-stopped
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d""", 0, True
' 5. Launch Wave Terminal (saved workspace handles pane layout)
WshShell.Run """" & WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\waveterm\Wave.exe""", 0, False
' NOTE: Tailscale runs as a Windows service (installed separately), not managed here.
' WSL ports (2222, 9001, 9090, 9091) auto-forward to Windows and are reachable via Tailscale IP.
