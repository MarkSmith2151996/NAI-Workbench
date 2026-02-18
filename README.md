# NAI Workbench

A persistent development environment powered by **Wave Terminal** + **Textual TUI dashboard**.

## What This Does

- **Wave Terminal** provides native split panes — terminal, web widgets, and Claude CLI side by side
- **Textual TUI Dashboard** shows real-time service health, Docker containers, system metrics, and project status
- **Claude CLI** runs natively in a terminal pane with full MCP tool access (no webui wrapper needed)
- **MCP servers** give Claude smart, on-demand codebase context without burning tokens
- **Security lanes** block vulnerable code before it gets committed
- **Docker containers** isolate each project

## Quick Start

1. Install Wave Terminal: `winget install CommandLine.Wave`
2. Copy `config/wave/*.json` to `%APPDATA%\waveterm\config\`
3. Open Wave Terminal — use sidebar widgets to launch Dashboard, Claude, Excalidraw, etc.

## Architecture

```
Wave Terminal (Windows)
  ├─ Workbench tab: TUI Dashboard + Excalidraw
  ├─ Claude tab: Claude CLI (MCP) + Excalidraw
  └─ Ops tab: WSL terminal + Komodo
```

## Security Pipeline

Pre-commit: Gitleaks + Semgrep + Trivy
Full pipeline: `bin/security-gate <dir> [url]`

## License

MIT
