#!/usr/bin/env bash
# Custodian Setup — Create venv, install deps, initialize database
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "=== Custodian Setup ==="
echo ""

# ── Step 1: Create Python venv ────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating Python virtual environment..."
    python -m venv "$VENV_DIR"
    echo "  Created at $VENV_DIR"
else
    echo "Virtual environment already exists at $VENV_DIR"
fi

# Activate
source "$VENV_DIR/Scripts/activate" 2>/dev/null || source "$VENV_DIR/bin/activate"
echo "  Activated venv (Python: $(python --version))"

# ── Step 2: Install dependencies ──────────────────────────────────────
echo ""
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q
echo "  Dependencies installed"

# ── Step 3: Verify tree-sitter ────────────────────────────────────────
echo ""
echo "Verifying tree-sitter..."
python -c "
from tree_sitter_languages import get_parser
p = get_parser('python')
tree = p.parse(b'def hello(): pass')
assert tree.root_node.type == 'module'
print('  tree-sitter: OK (python parser works)')
p = get_parser('typescript')
tree = p.parse(b'function hello(): void {}')
print('  tree-sitter: OK (typescript parser works)')
"

# ── Step 4: Initialize database ───────────────────────────────────────
echo ""
echo "Initializing database..."
python "$SCRIPT_DIR/init_db.py"

# ── Step 5: Make scripts executable ───────────────────────────────────
echo ""
chmod +x "$SCRIPT_DIR/index_project.sh" 2>/dev/null || true
chmod +x "$(dirname "$SCRIPT_DIR")/bin/custodian" 2>/dev/null || true
echo "Scripts made executable"

# ── Step 6: Verify ────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="
python -c "
import sqlite3, os, sys
# Use the same path resolution as init_db.py
db = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), 'custodian.db')
conn = sqlite3.connect(db)
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
projects = conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
prompts = conn.execute('SELECT COUNT(*) FROM custodian_prompts').fetchone()[0]
conn.close()
print(f'  Database: {db}')
print(f'  Tables: {tables}')
print(f'  Projects seeded: {projects}')
print(f'  Prompts seeded: {prompts}')
" "$SCRIPT_DIR/init_db.py"
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Index a project:  bin/custodian index progress-tracker"
echo "  2. Start MCP server: bin/custodian mcp"
echo "  3. Launch admin TUI: bin/custodian admin"
