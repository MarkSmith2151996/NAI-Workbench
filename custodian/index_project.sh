#!/usr/bin/env bash
# Custodian Indexing Pipeline
# Orchestrates: repomix → tree-sitter → git log → Sonnet → SQLite
#
# Usage: index_project.sh <project_name> <project_path>

set -euo pipefail

PROJECT_NAME="$1"
PROJECT_PATH="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMP_DIR="/tmp/custodian"
VENV_DIR="$SCRIPT_DIR/.venv"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[custodian]${NC} $1"; }
success() { echo -e "${GREEN}[custodian]${NC} $1"; }
warn() { echo -e "${YELLOW}[custodian]${NC} $1"; }
error() { echo -e "${RED}[custodian]${NC} $1"; }

# Validate inputs
if [ -z "$PROJECT_NAME" ] || [ -z "$PROJECT_PATH" ]; then
    error "Usage: index_project.sh <project_name> <project_path>"
    exit 1
fi

if [ ! -d "$PROJECT_PATH" ]; then
    error "Project path not found: $PROJECT_PATH"
    exit 1
fi

# Activate venv — detect platform first (same pattern as bin/admin-session)
WSL_VENV="$HOME/.custodian-venv"
if uname -s 2>/dev/null | grep -qi linux; then
    # WSL or native Linux — prefer the WSL-native venv
    if [ -d "$WSL_VENV/bin" ]; then
        source "$WSL_VENV/bin/activate"
    elif [ -d "$VENV_DIR/bin" ]; then
        source "$VENV_DIR/bin/activate"
    fi
else
    # Windows (Git Bash / MSYS)
    if [ -d "$VENV_DIR/Scripts" ]; then
        source "$VENV_DIR/Scripts/activate"
    elif [ -d "$VENV_DIR/bin" ]; then
        source "$VENV_DIR/bin/activate"
    fi
fi

mkdir -p "$TEMP_DIR"

REPOMIX_OUTPUT="$TEMP_DIR/repomix-${PROJECT_NAME}.txt"
SYMBOLS_OUTPUT="$TEMP_DIR/symbols-${PROJECT_NAME}.json"
GIT_LOG_OUTPUT="$TEMP_DIR/gitlog-${PROJECT_NAME}.txt"
GIT_DIFF_OUTPUT="$TEMP_DIR/gitdiff-${PROJECT_NAME}.txt"
SONNET_OUTPUT="$TEMP_DIR/fossil-${PROJECT_NAME}.json"

# ── Step 1: repomix dump ──────────────────────────────────────────────
log "Step 1/6: Running repomix on $PROJECT_NAME..."
if command -v repomix &> /dev/null; then
    repomix --output "$REPOMIX_OUTPUT" "$PROJECT_PATH" 2>/dev/null || {
        warn "repomix failed, falling back to basic file listing"
        find "$PROJECT_PATH" \
            -not -path '*/node_modules/*' \
            -not -path '*/.git/*' \
            -not -path '*/dist/*' \
            -not -path '*/.next/*' \
            -not -path '*/__pycache__/*' \
            -not -path '*/.venv/*' \
            -type f \
            -exec wc -l {} \; 2>/dev/null > "$REPOMIX_OUTPUT" || true
    }
else
    warn "repomix not found, using basic file dump"
    find "$PROJECT_PATH" \
        -not -path '*/node_modules/*' \
        -not -path '*/.git/*' \
        -not -path '*/dist/*' \
        -not -path '*/.next/*' \
        -not -path '*/__pycache__/*' \
        -not -path '*/.venv/*' \
        -type f \
        -exec wc -l {} \; 2>/dev/null > "$REPOMIX_OUTPUT" || true
fi
success "Repomix output: $(wc -c < "$REPOMIX_OUTPUT") bytes"

# ── Step 2: tree-sitter symbol extraction ─────────────────────────────
log "Step 2/6: Extracting symbols with tree-sitter..."
python "$SCRIPT_DIR/parse_symbols.py" "$PROJECT_PATH" > "$SYMBOLS_OUTPUT.full" 2>/dev/null || {
    warn "tree-sitter parsing failed, continuing with empty symbols"
    echo "[]" > "$SYMBOLS_OUTPUT.full"
}
# Filter: drop constants (bloat), keep functions/components/classes/types/hooks/stores, cap at 500
python -c "
import json, sys
with open(sys.argv[1]) as f:
    symbols = json.load(f)
# Prioritize: components, hooks, stores, classes, interfaces first, then functions, then types
priority = {'component': 0, 'hook': 1, 'store': 2, 'class': 3, 'interface': 4, 'function': 5, 'type': 6, 'enum': 7, 'constant': 8}
filtered = [s for s in symbols if s.get('type') != 'constant']
filtered.sort(key=lambda s: priority.get(s.get('type', 'constant'), 9))
filtered = filtered[:500]
with open(sys.argv[2], 'w') as f:
    json.dump(filtered, f, indent=1)
print(f'{len(symbols)} total -> {len(filtered)} kept')
" "$SYMBOLS_OUTPUT.full" "$SYMBOLS_OUTPUT" 2>/dev/null || {
    cp "$SYMBOLS_OUTPUT.full" "$SYMBOLS_OUTPUT"
}
rm -f "$SYMBOLS_OUTPUT.full"
SYMBOL_COUNT=$(python -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$SYMBOLS_OUTPUT" 2>/dev/null || echo "0")
success "Extracted $SYMBOL_COUNT symbols (filtered)"

# ── Step 3: git log ───────────────────────────────────────────────────
log "Step 3/6: Collecting git history..."
if [ -d "$PROJECT_PATH/.git" ]; then
    git -C "$PROJECT_PATH" log --oneline -20 > "$GIT_LOG_OUTPUT" 2>/dev/null || echo "No git history" > "$GIT_LOG_OUTPUT"
    git -C "$PROJECT_PATH" diff --stat HEAD~5..HEAD > "$GIT_DIFF_OUTPUT" 2>/dev/null || echo "No recent diff" > "$GIT_DIFF_OUTPUT"
else
    echo "Not a git repository" > "$GIT_LOG_OUTPUT"
    echo "" > "$GIT_DIFF_OUTPUT"
fi
success "Git history collected"

# ── Step 4: Get custodian prompt ──────────────────────────────────────
log "Step 4/6: Loading custodian prompt..."
# Use init_db.py's directory to find the DB (resolves Windows paths correctly)
CUSTODIAN_PROMPT=$(python -c "
import sqlite3, os, sys
db_dir = os.path.dirname(os.path.abspath(sys.argv[1]))
db = os.path.join(db_dir, 'custodian.db')
conn = sqlite3.connect(db)
row = conn.execute('''
    SELECT prompt FROM custodian_prompts
    WHERE project_id = (SELECT id FROM projects WHERE name = ?)
       OR project_id IS NULL
    ORDER BY project_id DESC, created_at DESC LIMIT 1
''', (sys.argv[2],)).fetchone()
print(row[0] if row else 'Analyze this codebase and produce a JSON fossil.')
conn.close()
" "$SCRIPT_DIR/init_db.py" "$PROJECT_NAME" 2>/dev/null || echo "Analyze this codebase and produce a JSON fossil.")
success "Prompt loaded"

# ── Step 5: Run Sonnet ────────────────────────────────────────────────
log "Step 5/6: Running Sonnet analysis (this may take a minute)..."

# Build the input for Sonnet
SONNET_INPUT="$TEMP_DIR/sonnet-input-${PROJECT_NAME}.txt"
cat > "$SONNET_INPUT" << INPUTEOF
PROJECT: $PROJECT_NAME

=== SYMBOL INDEX ===
$(cat "$SYMBOLS_OUTPUT")

=== RECENT GIT LOG (last 20 commits) ===
$(cat "$GIT_LOG_OUTPUT")

=== RECENT CHANGES (last 5 commits diff stat) ===
$(cat "$GIT_DIFF_OUTPUT")

=== FULL CODEBASE ===
$(cat "$REPOMIX_OUTPUT")
INPUTEOF

# Truncate if too large (Sonnet context limit)
SONNET_INPUT_SIZE=$(wc -c < "$SONNET_INPUT")
MAX_SIZE=300000  # ~300KB, safe for Sonnet
if [ "$SONNET_INPUT_SIZE" -gt "$MAX_SIZE" ]; then
    warn "Input too large ($SONNET_INPUT_SIZE bytes), truncating to ${MAX_SIZE} bytes"
    head -c "$MAX_SIZE" "$SONNET_INPUT" > "$SONNET_INPUT.tmp"
    mv "$SONNET_INPUT.tmp" "$SONNET_INPUT"
fi

# Run Sonnet via Claude CLI (clear env vars to allow nested invocation)
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
SONNET_STDERR="$TEMP_DIR/sonnet-stderr-${PROJECT_NAME}.txt"
claude --model sonnet -p "$CUSTODIAN_PROMPT" < "$SONNET_INPUT" > "$SONNET_OUTPUT" 2>"$SONNET_STDERR"

if [ $? -ne 0 ] || [ ! -s "$SONNET_OUTPUT" ]; then
    error "Sonnet analysis failed"
    if [ -s "$SONNET_STDERR" ]; then
        error "stderr: $(cat "$SONNET_STDERR")"
    fi
    exit 1
fi
success "Sonnet analysis complete: $(wc -c < "$SONNET_OUTPUT") bytes"

# ── Step 6: Store fossil in SQLite ────────────────────────────────────
log "Step 6/6: Storing fossil in database..."
python "$SCRIPT_DIR/store_fossil.py" "$PROJECT_NAME" "$SONNET_OUTPUT"

if [ $? -eq 0 ]; then
    success "✓ Fossil stored successfully for $PROJECT_NAME"
else
    error "Failed to store fossil"
    exit 1
fi

# Cleanup temp files
rm -f "$REPOMIX_OUTPUT" "$SYMBOLS_OUTPUT" "$GIT_LOG_OUTPUT" "$GIT_DIFF_OUTPUT" "$SONNET_INPUT" "$SONNET_OUTPUT" "$SONNET_STDERR"

success "Done! Use 'get_project_fossil(\"$PROJECT_NAME\")' in MCP to access the fossil."
