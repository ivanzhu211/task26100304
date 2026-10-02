"""SQLite 持久层：只负责 schema 与原子读写，不含业务规则。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    instance_id TEXT PRIMARY KEY,
    username TEXT,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL,
    deadline TEXT,
    avatar_deleted INTEGER NOT NULL DEFAULT 0,
    purged_at TEXT
);
CREATE TABLE IF NOT EXISTS records (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_instance ON records(instance_id);
"""

STATE_ACTIVE = "active"
STATE_CLOSING = "closing"
STATE_EXPIRED = "expired"
STATE_PURGED = "purged"


def _to_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _from_text(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Store:
    """对 SQLite 的薄封装。所有写操作以短事务提交，保证可恢复。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 元信息（时刻单调性） ----
    def get_last_time(self) -> Optional[datetime]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'last_time'"
        ).fetchone()
        return _from_text(row[0]) if row else None

    def set_last_time(self, moment: datetime) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_time', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_to_text(moment),),
        )

    # ---- 账户控制记录 ----
    def insert_account(self, instance_id: str, username: str, created_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO accounts(instance_id, username, created_at, state) "
            "VALUES(?, ?, ?, ?)",
            (instance_id, username, _to_text(created_at), STATE_ACTIVE),
        )

    def get_account(self, instance_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT instance_id, username, created_at, state, deadline, "
            "avatar_deleted, purged_at FROM accounts WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        return self._row_to_account(row) if row else None

    def find_occupying(self, username: str, now: datetime) -> Optional[dict]:
        """查找仍占用该用户名的实例：活跃，或宽限期未到的销户中实例。"""
        row = self._conn.execute(
            "SELECT instance_id, username, created_at, state, deadline, "
            "avatar_deleted, purged_at FROM accounts "
            "WHERE username = ? AND (state = ? OR (state = ? AND deadline > ?))",
            (username, STATE_ACTIVE, STATE_CLOSING, _to_text(now)),
        ).fetchone()
        return self._row_to_account(row) if row else None

    def mark_closing(self, instance_id: str, deadline: datetime) -> None:
        self._conn.execute(
            "UPDATE accounts SET state = ?, deadline = ? WHERE instance_id = ?",
            (STATE_CLOSING, _to_text(deadline), instance_id),
        )

    def mark_active(self, instance_id: str) -> None:
        self._conn.execute(
            "UPDATE accounts SET state = ?, deadline = NULL WHERE instance_id = ?",
            (STATE_ACTIVE, instance_id),
        )

    def list_expired(self, now: datetime) -> List[dict]:
        """到达截止时刻的实例：销户中且截止时刻不晚于 now，或已在清理中的。"""
        rows = self._conn.execute(
            "SELECT instance_id, username, created_at, state, deadline, "
            "avatar_deleted, purged_at FROM accounts "
            "WHERE state = ? OR (state = ? AND deadline <= ?) "
            "ORDER BY deadline, instance_id",
            (STATE_EXPIRED, STATE_CLOSING, _to_text(now)),
        ).fetchall()
        return [self._row_to_account(r) for r in rows]

    def mark_expired(self, instance_id: str) -> None:
        self._conn.execute(
            "UPDATE accounts SET state = ? WHERE instance_id = ?",
            (STATE_EXPIRED, instance_id),
        )

    def mark_purged(self, instance_id: str, purged_at: datetime) -> None:
        """清除含用户名的控制记录，仅保留不透明标识与完成状态。"""
        self._conn.execute(
            "UPDATE accounts SET state = ?, username = NULL, deadline = NULL, "
            "purged_at = ? WHERE instance_id = ?",
            (STATE_PURGED, _to_text(purged_at), instance_id),
        )

    # ---- 所属资料记录 ----
    def insert_records(self, instance_id: str, payloads: List[bytes]) -> None:
        self._conn.executemany(
            "INSERT INTO records(instance_id, payload) VALUES(?, ?)",
            [(instance_id, sqlite3.Binary(p)) for p in payloads],
        )

    def list_records(self, instance_id: str) -> List[bytes]:
        rows = self._conn.execute(
            "SELECT payload FROM records WHERE instance_id = ? ORDER BY rowid",
            (instance_id,),
        ).fetchall()
        return [bytes(r[0]) for r in rows]

    def delete_records_batch(self, instance_id: str, limit: int) -> int:
        """删除至多 limit 条记录并立即提交，返回实际删除条数。"""
        cur = self._conn.execute(
            "DELETE FROM records WHERE rowid IN "
            "(SELECT rowid FROM records WHERE instance_id = ? LIMIT ?)",
            (instance_id, limit),
        )
        return cur.rowcount

    # ---- 头像标记 ----
    def set_avatar_deleted(self, instance_id: str) -> None:
        self._conn.execute(
            "UPDATE accounts SET avatar_deleted = 1 WHERE instance_id = ?",
            (instance_id,),
        )

    def insert_account_with_records(
        self, instance_id: str, username: str, created_at: datetime, payloads
    ) -> None:
        """建档：控制记录与所属资料在同一事务提交。"""
        conn = self._conn
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO accounts(instance_id, username, created_at, state) "
                "VALUES(?, ?, ?, ?)",
                (instance_id, username, _to_text(created_at), STATE_ACTIVE),
            )
            conn.executemany(
                "INSERT INTO records(instance_id, payload) VALUES(?, ?)",
                ((instance_id, sqlite3.Binary(p)) for p in payloads),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def has_records(self, instance_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM records WHERE instance_id = ? LIMIT 1",
            (instance_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_account(row: tuple) -> dict:
        return {
            "instance_id": row[0],
            "username": row[1],
            "created_at": _from_text(row[2]),
            "state": row[3],
            "deadline": _from_text(row[4]) if row[4] else None,
            "avatar_deleted": bool(row[5]),
            "purged_at": _from_text(row[6]) if row[6] else None,
        }
