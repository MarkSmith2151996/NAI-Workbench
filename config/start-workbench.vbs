Set WshShell = CreateObject("WScript.Shell")

' 0. Auto-fix port proxy rules with current WSL IP
'    WSL NAT IP can change on every reboot — this ensures all port forwarding works immediately.
'    SSH (2222) always goes to 127.0.0.1:2223 (immune to IP changes).
Dim wslIpRaw, wslIp
Set objExec = WshShell.Exec("wsl -d Ubuntu-24.04 -- hostname -I")
wslIpRaw = Trim(objExec.StdOut.ReadLine())
' hostname -I may return multiple IPs — take the first one
wslIp = Split(wslIpRaw)(0)

If Len(wslIp) > 0 Then
    ' Clear all existing portproxy rules
    WshShell.Run "netsh interface portproxy reset", 0, True
    ' SSH — always via localhost (survives IP changes)
    WshShell.Run "netsh interface portproxy add v4tov4 listenport=2222 listenaddress=0.0.0.0 connectport=2223 connectaddress=127.0.0.1", 0, True
    ' Services — use current WSL IP
    WshShell.Run "netsh interface portproxy add v4tov4 listenport=7777 listenaddress=0.0.0.0 connectport=7777 connectaddress=" & wslIp, 0, True
    WshShell.Run "netsh interface portproxy add v4tov4 listenport=9001 listenaddress=0.0.0.0 connectport=9001 connectaddress=" & wslIp, 0, True
    WshShell.Run "netsh interface portproxy add v4tov4 listenport=9090 listenaddress=0.0.0.0 connectport=9090 connectaddress=" & wslIp, 0, True
    WshShell.Run "netsh interface portproxy add v4tov4 listenport=9091 listenaddress=0.0.0.0 connectport=9091 connectaddress=" & wslIp, 0, True
    WshShell.Run "netsh interface portproxy add v4tov4 listenport=9099 listenaddress=0.0.0.0 connectport=9099 connectaddress=" & wslIp, 0, True
End If

' 1. Start Docker + code-server (port 9091)
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""sudo systemctl start docker && sudo systemctl start code-server@dev""", 0, True
' 2. Start sshd on port 2223
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""sudo mkdir -p /run/sshd && sudo /usr/sbin/sshd""", 0, True
' 3. Start Komodo dashboard (port 9090)
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""docker compose -p komodo -f /home/dev/komodo/compose.yaml --env-file /home/dev/komodo/compose.env up -d""", 0, True
' 4. Start Penpot (port 9001) — containers have restart:unless-stopped
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""docker compose -p penpot -f /home/dev/projects/nai-workbench/config/penpot/compose.yaml --env-file /home/dev/projects/nai-workbench/config/penpot/compose.env up -d""", 0, True
' 5. Launch Wave Terminal (saved workspace handles pane layout)
WshShell.Run """" & WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\waveterm\Wave.exe""", 0, False
' 6. Launch ticker overlay (after Wave so sandbox router is up)
WshShell.Run "pythonw.exe ""C:\Users\Big A\NAI-Workbench\custodian\ticker_overlay.py""", 0, False
' NOTE: Tailscale runs as a Windows service (installed separately), not managed here.
' Port proxy rules are recreated at step 0 above with the current WSL NAT IP.
