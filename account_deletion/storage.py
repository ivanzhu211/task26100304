"""SQLite 存储层：建表与连接管理。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    instance_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at REAL NOT NULL,
    deletion_requested_at REAL,
    deletion_deadline REAL,
    avatar_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);

CREATE TABLE IF NOT EXISTS records (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_instance ON records(instance_id);

CREATE TABLE IF NOT EXISTS cleanup_log (
    instance_id TEXT PRIMARY KEY,
    completed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value REAL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库并确保表结构存在。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
