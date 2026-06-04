#!/usr/bin/env python3
"""Standalone SQLite session registry shared by MCP transports."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_registry.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_registry_db() -> sqlite3.Connection:
    conn = sqlite3.connect(REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def registry_connection():
    conn = get_registry_db()
    try:
        yield conn
    finally:
        conn.close()


def ensure_registry(reset_transport: str | None = None) -> None:
    with registry_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                transport TEXT NOT NULL,
                connected_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                user_agent TEXT,
                remote_addr TEXT,
                status TEXT DEFAULT 'active'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_status_activity ON sessions(status, last_activity_at DESC)"
        )
        if reset_transport:
            conn.execute(
                "UPDATE sessions SET status = 'disconnected' WHERE status = 'active' AND transport = ?",
                (reset_transport,),
            )
        else:
            conn.execute("UPDATE sessions SET status = 'disconnected' WHERE status = 'active'")
        conn.commit()


def register_session(
    session_id: str,
    transport: str,
    user_agent: str | None = None,
    remote_addr: str | None = None,
) -> None:
    timestamp = _now_iso()
    with registry_connection() as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, transport, connected_at, last_activity_at, user_agent, remote_addr, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(session_id) DO UPDATE SET
                transport = excluded.transport,
                connected_at = excluded.connected_at,
                last_activity_at = excluded.last_activity_at,
                user_agent = excluded.user_agent,
                remote_addr = excluded.remote_addr,
                status = 'active'
            """,
            (session_id, transport, timestamp, timestamp, user_agent, remote_addr),
        )
        conn.commit()


def touch_session(session_id: str) -> None:
    with registry_connection() as conn:
        conn.execute(
            "UPDATE sessions SET last_activity_at = ? WHERE session_id = ?",
            (_now_iso(), session_id),
        )
        conn.commit()


def disconnect_session(session_id: str) -> None:
    with registry_connection() as conn:
        conn.execute(
            "UPDATE sessions SET status = 'disconnected', last_activity_at = ? WHERE session_id = ?",
            (_now_iso(), session_id),
        )
        conn.commit()
