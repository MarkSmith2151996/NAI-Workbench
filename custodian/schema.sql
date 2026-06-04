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
    last_indexed TEXT,
    task_prefix TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_task_prefix
    ON projects(task_prefix)
    WHERE task_prefix IS NOT NULL;

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
    model TEXT DEFAULT 'openai/gpt-5.4',
    project_id INTEGER REFERENCES projects(id),
    max_turns INTEGER DEFAULT 20,
    tools TEXT,                          -- JSON: allowed tool names
    mcp_servers TEXT,                    -- JSON: MCP server configs
    spec_path TEXT,                      -- YAML spec path relative to the project box /workspace root
    workstation TEXT,                    -- Optional workstation spec name for container-slot execution
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    version INTEGER DEFAULT 1,
    spec TEXT NOT NULL,              -- full YAML spec stored as text
    input_schema TEXT,               -- JSON: parsed input_schema for validation
    trigger_type TEXT DEFAULT 'manual',
    status TEXT DEFAULT 'active',    -- active, disabled, archived
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
    run_name TEXT NOT NULL,
    input TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    current_step TEXT,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    error TEXT,
    stats TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_step_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL,
    iteration_index INTEGER,
    iteration_key TEXT,
    status TEXT DEFAULT 'pending',
    input TEXT,
    output TEXT,
    output_file TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    error TEXT
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
    display_mode TEXT DEFAULT 'terminal', -- web, terminal, gui
    started_at TEXT,
    stopped_at TEXT,
    build_log TEXT                       -- Last build output
);

-- Project Boxes — persistent per-project Docker runtimes
CREATE TABLE IF NOT EXISTS project_boxes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id),
    container_name TEXT NOT NULL,
    image TEXT NOT NULL,
    status TEXT DEFAULT 'provisioning',
    env_vars TEXT DEFAULT '{}',
    ports TEXT DEFAULT '{}',
    tool_server_port INTEGER,
    restart_policy TEXT DEFAULT 'unless-stopped',
    last_healthcheck TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Workstations — warm multi-slot Docker runtimes for agent execution
CREATE TABLE IF NOT EXISTS workstation_specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    services TEXT NOT NULL DEFAULT '[]',
    deps TEXT NOT NULL DEFAULT '[]',
    env_vars TEXT NOT NULL DEFAULT '{}',
    volumes TEXT NOT NULL DEFAULT '[]',
    tool_definitions TEXT NOT NULL DEFAULT '[]',
    image TEXT NOT NULL DEFAULT 'nai-sandbox:latest',
    max_slots INTEGER NOT NULL DEFAULT 10,
    browser_profile TEXT,
    created_by TEXT NOT NULL DEFAULT 'claude',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workstation_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id INTEGER NOT NULL REFERENCES workstation_specs(id),
    container_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'provisioning',
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workstation_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES workstation_instances(id),
    slot_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'free',
    agent_run_id INTEGER,
    working_dir TEXT,
    output_dir TEXT,
    allocated_at TEXT,
    released_at TEXT,
    UNIQUE(instance_id, slot_index)
);

CREATE INDEX IF NOT EXISTS idx_workstation_specs_status ON workstation_specs(status);
CREATE INDEX IF NOT EXISTS idx_workstation_instances_spec ON workstation_instances(spec_id);
CREATE INDEX IF NOT EXISTS idx_workstation_slots_instance_status ON workstation_slots(instance_id, status);

-- Project folders — registered shared output locations discoverable by tools
CREATE TABLE IF NOT EXISTS project_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    category TEXT NOT NULL,
    wsl_path TEXT NOT NULL,
    mac_path TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(project, category)
);

-- Tool registry — durable metadata for MCP wrappers and their source hooks
CREATE TABLE IF NOT EXISTS tool_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    project TEXT NOT NULL,
    description TEXT,
    source_module TEXT NOT NULL,
    source_class TEXT,
    source_method TEXT,
    hook_point TEXT NOT NULL,
    return_type TEXT NOT NULL,
    known_side_effects TEXT,
    wrapper_path TEXT NOT NULL,
    input_schema TEXT,
    output_schema TEXT,
    handler_code TEXT,
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active',
    created_by TEXT DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(tool_name, project)
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_reindex_requests_status ON reindex_requests(status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_builds_project ON alpha_builds(project_id, status);
CREATE INDEX IF NOT EXISTS idx_project_boxes_project ON project_boxes(project_id);

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

-- Session updates — lightweight structured self-reports between deep fossils
CREATE TABLE IF NOT EXISTS session_updates (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    task TEXT,
    files_modified TEXT,
    unexecuted_steps TEXT,
    decisions TEXT,
    unfinished TEXT,
    tokens_used INTEGER,
    source TEXT DEFAULT 'opencode'
);

CREATE INDEX IF NOT EXISTS idx_session_updates_project ON session_updates(project_id, created_at DESC);

-- Task handoff: Claude-designed tasks picked up by OpenCode via ID reference
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ct_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    project TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'open',
    executed_at TEXT,
    execution_notes TEXT,
    produced_files TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_ct_id ON tasks(ct_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status);

-- Lightweight todo capture layer — per-project or system-wide follow-ups
CREATE TABLE IF NOT EXISTS todo_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id TEXT UNIQUE NOT NULL,
    project TEXT,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    promoted_to TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_todo_project ON todo_items(project);
CREATE INDEX IF NOT EXISTS idx_todo_status ON todo_items(status);

-- System-wide updates broadcast: new tools, rules, capabilities
CREATE TABLE IF NOT EXISTS system_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    project TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT DEFAULT 'claude'
);

CREATE INDEX IF NOT EXISTS idx_system_updates_created ON system_updates(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_updates_category ON system_updates(category, created_at DESC);

-- Generic runtime config storage for small system settings like API keys
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Custodian meta-logging — friction points and shipped system changes
CREATE TABLE IF NOT EXISTS friction_points (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    surface_event TEXT NOT NULL,
    project_state_context TEXT NOT NULL,
    chat_session_context TEXT NOT NULL,
    root_cause TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'mitigated', 'resolved', 'wontfix')),
    resolved_by TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS changelog_entries (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    sub_items TEXT,
    resolves_friction TEXT,
    related_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_friction_points_status_created
    ON friction_points(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_changelog_entries_created
    ON changelog_entries(created_at DESC);

-- Ticker config — toggle which indicators appear in the sandbox ticker bar
CREATE TABLE IF NOT EXISTS ticker_config (
    key TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    display_order INTEGER DEFAULT 0,
    label TEXT,
    format TEXT
);

-- Ticker overlay settings — key/value pairs for the Windows overlay appearance
CREATE TABLE IF NOT EXISTS ticker_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

-- Persistent Memory — searchable, tagged memories shared across all Claude sessions
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '[]',          -- JSON array of tag strings
    project_id INTEGER REFERENCES projects(id),  -- NULL = global memory
    source TEXT DEFAULT 'mcp',       -- mcp, auto, migration
    importance INTEGER DEFAULT 5,    -- 1 (low) to 10 (critical)
    access_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC, updated_at DESC);

-- FTS5 full-text search index for memories
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    tags,
    content=memories,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- Sync triggers to keep FTS index in sync with memories table
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;

-- Memory drift flags — passive reports that a stored memory is wrong or stale
CREATE TABLE IF NOT EXISTS memory_flags (
    id TEXT PRIMARY KEY,
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    reason TEXT NOT NULL,
    flagged_in_context TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'wontfix')),
    resolved_by TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_flags_status_created
    ON memory_flags(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_flags_memory_id
    ON memory_flags(memory_id, status);

-- OAuth 2.1 client and token state for Custodian MCP HTTP
CREATE TABLE IF NOT EXISTS oauth_clients (
    id TEXT PRIMARY KEY,
    name TEXT,
    secret TEXT,
    redirect_uris TEXT NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL DEFAULT 'client_secret_post',
    grant_types TEXT NOT NULL,
    response_types TEXT NOT NULL,
    scope TEXT,
    client_uri TEXT,
    logo_uri TEXT,
    contacts TEXT,
    tos_uri TEXT,
    policy_uri TEXT,
    jwks_uri TEXT,
    jwks TEXT,
    software_id TEXT,
    software_version TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    client_id_issued_at INTEGER,
    secret_expires_at INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id TEXT PRIMARY KEY,
    family_id TEXT,
    token_hash TEXT NOT NULL,
    token_type TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    revoked INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code_hash TEXT PRIMARY KEY,
    code_id TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER DEFAULT 1,
    code_challenge TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER NOT NULL,
    used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_approval_sessions (
    id TEXT PRIMARY KEY,
    client_id TEXT REFERENCES oauth_clients(id),
    expires_at INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oauth_clients_name ON oauth_clients(name);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_hash ON oauth_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client_type ON oauth_tokens(client_id, token_type);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_family ON oauth_tokens(family_id);
CREATE INDEX IF NOT EXISTS idx_oauth_auth_codes_expires ON oauth_auth_codes(expires_at);
