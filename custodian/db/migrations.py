from __future__ import annotations

import os
import sqlite3

from custodian.db.connection import DB_PATH
from custodian.oauth_provider import ensure_oauth_schema


def _migration_001_native_extensions(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS native_extensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            base_path TEXT DEFAULT '/',
            protocol TEXT DEFAULT 'http',
            health_endpoint TEXT DEFAULT '/health',
            project TEXT,
            status TEXT DEFAULT 'active',
            last_health_check TEXT,
            last_health_error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_native_ext_name ON native_extensions(name);
        CREATE INDEX IF NOT EXISTS idx_native_ext_project ON native_extensions(project);
        """
    )


def _migration_002_workstations(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
        """
    )
    _ensure_column(conn, "workstation_specs", "tool_definitions", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "agents", "workstation", "TEXT")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def run_migrations() -> None:
    schema_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
    conn = sqlite3.connect(DB_PATH)
    try:
        with open(schema_path, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        _migration_001_native_extensions(conn)
        _migration_002_workstations(conn)
        conn.commit()
    finally:
        conn.close()
    ensure_oauth_schema()


def run_all_migrations() -> None:
    run_migrations()
