# NAI Workbench

A persistent, browser-accessible development environment with security gates.

## What This Does

- **code-server** runs VS Code in your browser. Close your laptop, Claude Code keeps working.
- **MCP servers** give Claude smart, on-demand codebase context without burning tokens.
- **Security lanes** block vulnerable code before it gets committed.
- **Docker containers** isolate each project.

## Quick Start

Inside WSL2 Ubuntu:
    git clone https://github.com/MarkSmith2151996/NAI-Workbench.git
    cd NAI-Workbench
    ./install.sh

Then open http://localhost:9090 in your browser.

## Security Pipeline

Pre-commit hooks run Gitleaks + Semgrep + Trivy on every commit.

Full pipeline before deploy:
    ./bin/security-gate /path/to/project http://localhost:3000

## Repo Structure

    install.sh          One-command setup
    config/             code-server, tmux, MCP, WSL configs
    hooks/              Pre-commit security hook + installer
    bin/                security-gate pipeline
    templates/          Dev container templates - node, python
    docs/               Setup guide, security docs

## License

MIT
