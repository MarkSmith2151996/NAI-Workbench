-- Sticky Notes
CREATE TABLE IF NOT EXISTS sticky_notes (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    color TEXT DEFAULT 'yellow',
    done INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Custodian System Schema
-- Maintains project fossils (compressed indexes) for Claude Opus sessions

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    stack TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_indexed TEXT
);

CREATE TABLE IF NOT EXISTS fossils (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    version INTEGER NOT NULL,
    file_tree TEXT,           -- JSON: [{path, description, lines}]
    architecture TEXT,        -- Sonnet's architecture summary
    recent_changes TEXT,      -- Last N commits summarized
    known_issues TEXT,        -- TODOs, bugs, tech debt
    dependencies TEXT,        -- JSON: [{name, version, purpose}]
    summary TEXT,             -- One-paragraph distillation
    prompt_used TEXT          -- The custodian prompt that generated this fossil
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    fossil_id INTEGER NOT NULL REFERENCES fossils(id),
    file_path TEXT NOT NULL,
    line_number INTEGER,
    type TEXT NOT NULL,       -- function, class, component, route, hook, store, type
    name TEXT NOT NULL,
    signature TEXT,           -- params, return type
    description TEXT,         -- Sonnet's one-line summary
    relationships TEXT        -- JSON: {calls: [], called_by: [], depends_on: []}
);

CREATE TABLE IF NOT EXISTS detective_insights (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,       -- NULL for cross-project insights
    fossil_id INTEGER REFERENCES fossils(id),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    insight_type TEXT NOT NULL, -- coupling, growth, pattern, regression, prompt_refinement
    content TEXT NOT NULL,
    model_used TEXT,           -- sonnet or opus
    projects_involved TEXT     -- JSON array of project names
);

CREATE TABLE IF NOT EXISTS custodian_prompts (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id), -- NULL = default prompt
    prompt TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT,           -- detective, manual, initial
    notes TEXT                 -- Why this prompt was created/refined
);

CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY,
    tool_name TEXT,
    project_name TEXT,
    query_params TEXT,         -- JSON of params passed
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fossils_project ON fossils(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_symbols_project ON symbols(project_id, name);
CREATE INDEX IF NOT EXISTS idx_symbols_type ON symbols(project_id, type);
CREATE INDEX IF NOT EXISTS idx_insights_project ON detective_insights(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_log_tool ON query_log(tool_name, timestamp DESC);

-- Editor sessions — persistent Claude sessions per project
CREATE TABLE IF NOT EXISTS editor_sessions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    session_id TEXT NOT NULL,
    summary TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    last_active TEXT DEFAULT (datetime('now')),
    device TEXT,
    status TEXT DEFAULT 'active'
);

-- Sandbox state — tracks background dev server / test processes
CREATE TABLE IF NOT EXISTS sandbox_state (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    command TEXT,
    pid INTEGER,
    port INTEGER,
    status TEXT DEFAULT 'stopped',
    log_path TEXT,
    preview_type TEXT,     -- 'web' or 'terminal'
    tmux_session TEXT,     -- tmux session name for terminal apps
    preview_url TEXT       -- URL where the preview is accessible (ttyd or direct)
);

CREATE INDEX IF NOT EXISTS idx_editor_sessions_project ON editor_sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_sandbox_state_project ON sandbox_state(project_id);

-- Agent Factory — AI agents powered by Claude Agent SDK
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    model TEXT DEFAULT 'sonnet',
    project_id INTEGER REFERENCES projects(id),
    max_turns INTEGER DEFAULT 20,
    tools TEXT,                          -- JSON: allowed tool names
    mcp_servers TEXT,                    -- JSON: MCP server configs
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    steps TEXT NOT NULL,                 -- JSON: [{agent_id, input_mapping, condition}]
    schedule TEXT,                       -- cron expression (null = manual)
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER REFERENCES agents(id),
    pipeline_id INTEGER REFERENCES pipelines(id),
    pipeline_step INTEGER,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT DEFAULT 'running',       -- running, completed, failed, cancelled
    input TEXT,
    output TEXT,
    tokens_used INTEGER,
    error TEXT,
    triggered_by TEXT                    -- manual, schedule, pipeline
);

CREATE TABLE IF NOT EXISTS reindex_requests (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
    requested_by TEXT,
    reason TEXT,
    status TEXT DEFAULT 'pending',       -- pending, approved, denied, completed
    resolved_at TEXT
);

-- Alpha Builds — Docker container-based project sandboxes
CREATE TABLE IF NOT EXISTS alpha_builds (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    container_id TEXT,                   -- Docker container ID
    container_name TEXT,                 -- Human-readable name
    image TEXT,                          -- Docker image used
    status TEXT DEFAULT 'stopped',       -- building, running, stopped, failed
    ports TEXT,                          -- JSON: {host_port: container_port}
    command TEXT,                        -- Startup command
    started_at TEXT,
    stopped_at TEXT,
    build_log TEXT                       -- Last build output
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_reindex_requests_status ON reindex_requests(status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_builds_project ON alpha_builds(project_id, status);

-- Indexing runs — tracks custodian indexing pipeline executions
CREATE TABLE IF NOT EXISTS indexing_runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    status TEXT DEFAULT 'running',      -- running, completed, failed
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    error TEXT,
    log_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_indexing_runs_project ON indexing_runs(project_id, started_at DESC);

-- Ticker config — toggle which indicators appear in the sandbox ticker bar
CREATE TABLE IF NOT EXISTS ticker_config (
    key TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1
);

-- Device pairing — remote devices connected to this Workbench
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    hostname TEXT,
    tailscale_ip TEXT,
    ssh_pubkey TEXT,
    ssh_fingerprint TEXT,
    paired_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT,
    status TEXT DEFAULT 'paired'    -- paired, revoked
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    used_by_device_id INTEGER REFERENCES devices(id),
    status TEXT DEFAULT 'pending'   -- pending, used, expired
);
