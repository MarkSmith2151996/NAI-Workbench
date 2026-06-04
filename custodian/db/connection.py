from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custodian.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db_connection():
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def save_point(task_id: str) -> str:
    backup = f"{DB_PATH}.pre-{task_id}"
    shutil.copy2(DB_PATH, backup)
    return backup


def restore_save_point(task_id: str) -> bool:
    backup = f"{DB_PATH}.pre-{task_id}"
    if not os.path.exists(backup):
        return False
    shutil.copy2(backup, DB_PATH)
    return True
