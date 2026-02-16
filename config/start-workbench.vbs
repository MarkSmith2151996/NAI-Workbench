Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "wsl -d Ubuntu-24.04 -- bash -c ""sudo systemctl start docker && sudo systemctl start code-server@dev""", 0, False
