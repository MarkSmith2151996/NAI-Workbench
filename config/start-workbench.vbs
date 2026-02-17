Set WshShell = CreateObject("WScript.Shell")
' Start Docker + code-server (port 9091)
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""sudo systemctl start docker && sudo systemctl start code-server@dev""", 0, True
' Start Komodo dashboard (port 9090)
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""docker compose -p komodo -f /home/dev/komodo/compose.yaml --env-file /home/dev/komodo/compose.env up -d""", 0, True
' Start claude-code-webui (port 9082) - user systemd service
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""systemctl --user start claude-webui""", 0, False
' Docker containers with --restart unless-stopped auto-start:
'   excalidraw-canvas (9081), nai-portal (9080)
