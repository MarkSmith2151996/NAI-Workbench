#!/usr/bin/env bash
set -euo pipefail
echo "=== NAI Workbench Installer ==="
if [ ! -f /proc/version ] || ! grep -qi microsoft /proc/version; then
  echo "ERROR: Must run inside WSL2"; exit 1
fi
SCRIPT_DIR=""
echo "[1/7] System packages..."
sudo apt update -qq
sudo DEBIAN_FRONTEND=noninteractive apt install -y build-essential curl wget git jq fzf ripgrep fd-find tmux unzip ca-certificates gnupg lsb-release python3 python3-pip python3-venv
echo "[2/7] Node.js 22..."
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo DEBIAN_FRONTEND=noninteractive apt install -y nodejs
fi
echo "[3/7] Docker..."
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "dev"
  sudo systemctl enable docker
fi
echo "[4/7] code-server..."
if ! command -v code-server &>/dev/null; then
  curl -fsSL https://code-server.dev/install.sh | sh
fi
echo "[5/7] Claude Code + MCP..."
sudo npm install -g @anthropic-ai/claude-code repomix @modelcontextprotocol/server-memory @modelcontextprotocol/server-filesystem
echo "[6/7] Security tools..."
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sudo sh -s -- -b /usr/local/bin
pip install --user --break-system-packages semgrep 2>/dev/null || true
sudo ln -sf "/home/dev/.local/bin/pysemgrep" /usr/local/bin/pysemgrep 2>/dev/null || true
sudo ln -sf "/home/dev/.local/bin/semgrep" /usr/local/bin/semgrep 2>/dev/null || true
echo "[7/7] Configuring..."
mkdir -p ~/.config/code-server
cp "/config/code-server.yaml" ~/.config/code-server/config.yaml
cp "/config/tmux.conf" ~/.tmux.conf
mkdir -p ~/.claude
cp "/config/mcp.json" ~/.claude/.mcp.json
mkdir -p ~/projects
sudo systemctl start docker
sudo systemctl enable --now "code-server@dev"
echo "=== Done! Open http://localhost:9090 ==="
