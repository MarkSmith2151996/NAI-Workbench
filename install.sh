#!/usr/bin/env bash
set -euo pipefail
echo "=== NAI Workbench Installer ==="
if [ ! -f /proc/version ] || ! grep -qi microsoft /proc/version; then
  echo "ERROR: Must run inside WSL2"; exit 1
fi
SCRIPT_DIR=""
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "[1/9] System packages..."
sudo apt update -qq
sudo DEBIAN_FRONTEND=noninteractive apt install -y build-essential curl wget git jq fzf ripgrep fd-find tmux unzip ca-certificates gnupg lsb-release python3 python3-pip python3-venv

echo "[2/9] Node.js 22..."
if ! command -v node &>/dev/null; then
  curl -fsSL -o "$TMPDIR/setup_node.sh" https://deb.nodesource.com/setup_22.x
  sudo -E bash "$TMPDIR/setup_node.sh"
  sudo DEBIAN_FRONTEND=noninteractive apt install -y nodejs
fi

echo "[3/9] Docker..."
if ! command -v docker &>/dev/null; then
  curl -fsSL -o "$TMPDIR/get-docker.sh" https://get.docker.com
  sh "$TMPDIR/get-docker.sh"
  sudo usermod -aG docker "dev"
  sudo systemctl enable docker
fi

echo "[4/9] code-server..."
if ! command -v code-server &>/dev/null; then
  curl -fsSL -o "$TMPDIR/install-code-server.sh" https://code-server.dev/install.sh
  sh "$TMPDIR/install-code-server.sh"
fi

echo "[5/9] Claude Code + MCP..."
sudo npm install -g @anthropic-ai/claude-code repomix @modelcontextprotocol/server-memory @modelcontextprotocol/server-filesystem

echo "[6/9] Security tools..."
curl -fsSL -o "$TMPDIR/install-trivy.sh" https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh
sudo sh "$TMPDIR/install-trivy.sh" -b /usr/local/bin
pip install --user --break-system-packages semgrep 2>/dev/null || true
sudo ln -sf "/home/dev/.local/bin/pysemgrep" /usr/local/bin/pysemgrep 2>/dev/null || true
sudo ln -sf "/home/dev/.local/bin/semgrep" /usr/local/bin/semgrep 2>/dev/null || true

echo "[7/9] Configuring..."
mkdir -p ~/.config/code-server
cp "/config/code-server.yaml" ~/.config/code-server/config.yaml
cp "/config/tmux.conf" ~/.tmux.conf
mkdir -p ~/.claude
cp "/config/mcp.json" ~/.claude/.mcp.json
mkdir -p ~/projects
sudo systemctl start docker
sudo systemctl enable --now "code-server@dev"

echo "[8/9] Tailscale..."
if ! command -v tailscale &>/dev/null; then
  curl -fsSL -o "$TMPDIR/install-tailscale.sh" https://tailscale.com/install.sh
  sh "$TMPDIR/install-tailscale.sh"
fi
sudo mkdir -p /var/lib/tailscale /run/tailscale

echo "[9/9] OpenSSH server..."
sudo DEBIAN_FRONTEND=noninteractive apt install -y openssh-server
sudo mkdir -p /etc/ssh/sshd_config.d /run/sshd
cat <<'SSHEOF' | sudo tee /etc/ssh/sshd_config.d/workbench.conf > /dev/null
Port 2222
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
SSHEOF
mkdir -p /home/dev/.ssh
chmod 700 /home/dev/.ssh
touch /home/dev/.ssh/authorized_keys
chmod 600 /home/dev/.ssh/authorized_keys

echo "=== Done! Open http://localhost:9090 ==="
echo "  - Add your laptop's SSH public key to ~/.ssh/authorized_keys"
echo "  - Run 'sudo tailscale up' to authenticate with Tailscale"
