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
