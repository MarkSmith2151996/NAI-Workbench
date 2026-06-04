## Prerequisites

- Python 3.10+ (native Windows Python, NOT WSL)
- `pip install mcp uvicorn starlette psutil`

## Run manually

```bat
set BRIDGE_TOKEN=<token>
python server.py
```

## Run as Windows service (optional)

Use NSSM or Task Scheduler to run on boot:

```bat
nssm install windows-bridge "C:\Python312\python.exe" "C:\path\to\server.py"
nssm set windows-bridge AppEnvironmentExtra BRIDGE_TOKEN=<token>
```
