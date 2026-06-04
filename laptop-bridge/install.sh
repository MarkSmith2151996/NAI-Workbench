#!/bin/bash
# NAI Workbench — Laptop Bridge MCP Server installer
# Run on the Mac or a Linux laptop as your regular user.
#
# Usage:
#   bash install.sh              # Interactive — prompts for token
#   BRIDGE_TOKEN=xxx bash install.sh  # Non-interactive
set -euo pipefail

BRIDGE_DIR="$HOME/laptop-bridge"
SERVICE_NAME="laptop-bridge"
BRIDGE_HOST="${BRIDGE_HOST:-0.0.0.0}"
BRIDGE_PORT="${BRIDGE_PORT:-8222}"

echo "=== NAI Workbench Laptop Bridge Installer ==="
echo ""

# --- 1. Copy files ---
echo "[1/5] Installing files to $BRIDGE_DIR..."
mkdir -p "$BRIDGE_DIR"
cp -f "$(dirname "$0")/server.py" "$BRIDGE_DIR/server.py"
chmod +x "$BRIDGE_DIR/server.py"
echo "  -> $BRIDGE_DIR/server.py"

# --- 2. Install Python dependencies ---
echo ""
echo "[2/5] Installing Python dependencies..."
pip install --user --quiet mcp uvicorn starlette
echo "  -> mcp, uvicorn, starlette installed"

# --- 3. Generate or use token ---
if [ -z "${BRIDGE_TOKEN:-}" ]; then
    echo ""
    echo "[3/5] Generating auth token..."
    BRIDGE_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo ""
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║  BRIDGE TOKEN (save this — you'll need it on PC):   ║"
    echo "  ║  $BRIDGE_TOKEN  ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo ""
else
    echo ""
    echo "[3/5] Using provided BRIDGE_TOKEN"
fi

# --- 4. Create systemd user service ---
# Linux only: macOS persistence should use a LaunchAgent plist instead.
echo "[4/5] Creating systemd user service..."
mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/${SERVICE_NAME}.service" << UNIT
[Unit]
Description=NAI Workbench Laptop Bridge MCP Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$(which python3) $BRIDGE_DIR/server.py
Restart=on-failure
RestartSec=5
Environment=BRIDGE_TOKEN=$BRIDGE_TOKEN
Environment=BRIDGE_HOST=$BRIDGE_HOST
Environment=BRIDGE_PORT=$BRIDGE_PORT

[Install]
WantedBy=default.target
UNIT

echo "  -> ~/.config/systemd/user/${SERVICE_NAME}.service"

# --- 5. Enable and start ---
echo ""
echo "[5/5] Enabling and starting service..."
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"
sleep 1

if systemctl --user is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "=== SUCCESS ==="
    echo "Laptop Bridge is running on ${BRIDGE_HOST}:${BRIDGE_PORT}"
    echo ""
    echo "To verify:  curl http://${BRIDGE_HOST}:${BRIDGE_PORT}/health -H 'Authorization: Bearer ${BRIDGE_TOKEN}'"
    echo ""
    echo "Add this to your PC's .claude/mcp.json:"
    echo ""
    echo "  \"laptop-bridge\": {"
    echo "    \"type\": \"sse\","
    echo "    \"url\": \"http://${BRIDGE_HOST}:${BRIDGE_PORT}/sse\","
    echo "    \"headers\": {\"Authorization\": \"Bearer ${BRIDGE_TOKEN}\"}"
    echo "  }"
    echo ""
    echo "Then run /mcp in Claude Code to reconnect."
else
    echo ""
    echo "=== FAILED ==="
    echo "Service did not start. Check logs:"
    echo "  journalctl --user -u $SERVICE_NAME -n 20"
    exit 1
fi

# Enable lingering so service runs even when not logged in
loginctl enable-linger "$(whoami)" 2>/dev/null || true
