"""账户销户服务：建档、查看、销户申请/撤销与预算化维护清理。"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import storage
from .models import (
    AccountError,
    AccountExpiredError,
    AccountNotFoundError,
    AccountView,
    MaintenanceReport,
    UsernameOccupiedError,
)

GRACE_PERIOD = timedelta(hours=30 * 24)
_DELETE_BATCH = 1000
_INSERT_BATCH = 10000


def _to_ts(now: datetime) -> float:
    if now.tzinfo is None:
        raise ValueError("now 必须携带时区（UTC 时刻）")
    return now.astimezone(timezone.utc).timestamp()


def _from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class AccountService:
    """由上层调用的可恢复销户组件。所有操作串行执行。"""

    def __init__(
        self,
        db_path: str | Path,
        avatar_dir: str | Path,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._avatar_dir = Path(avatar_dir)
        self._avatar_dir.mkdir(parents=True, exist_ok=True)
        self._conn = storage.connect(self._db_path)
        # 测试用故障注入点：在维护关键位置以标签回调，便于模拟进程中断。
        self._fault_hook = fault_hook

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AccountService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 内部工具 ----

    def _hook(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    def _now_ts(self, now: datetime) -> float:
        """保证时刻不倒退：返回不早于历史最大值的单调时刻。"""
        ts = _to_ts(now)
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='last_now'"
        ).fetchone()
        if row is not None and row[0] > ts:
            ts = row[0]
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_now', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ts,),
        )
        return ts

    def _get_row(self, instance_id: str) -> sqlite3.Row | tuple:
        row = self._conn.execute(
            "SELECT instance_id, username, created_at, deletion_requested_at,"
            " deletion_deadline, avatar_deleted FROM accounts"
            " WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            raise AccountNotFoundError(instance_id)
        return row

    def _avatar_path(self, instance_id: str) -> Path:
        # 头像以不透明实例标识命名，与用户名解耦，复用用户名不会冲突。
        return self._avatar_dir / f"{instance_id}.avatar"

    # ---- 建档与查看 ----

    def create_account(
        self,
        username: str,
        records: Iterable[bytes],
        avatar: bytes,
        now: datetime,
    ) -> str:
        """建档，返回不透明实例标识。用户名被未到期实例占用时抛错。"""
        now_ts = self._now_ts(now)
        occupied = self._conn.execute(
            "SELECT 1 FROM accounts WHERE username=? AND"
            " (deletion_deadline IS NULL OR deletion_deadline > ?) LIMIT 1",
            (username, now_ts),
        ).fetchone()
        if occupied is not None:
            raise UsernameOccupiedError(username)

        instance_id = uuid.uuid4().hex
        avatar_path = self._avatar_path(instance_id)
        # 先写头像文件：若崩溃，孤儿文件不被任何实例引用，无害。
        with open(avatar_path, "wb") as fh:
            fh.write(avatar)
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO accounts(instance_id, username, created_at)"
                    " VALUES(?, ?, ?)",
                    (instance_id, username, now_ts),
                )
                it = iter(records)
                while True:
                    chunk = list(islice(it, _INSERT_BATCH))
                    if not chunk:
                        break
                    self._conn.executemany(
                        "INSERT INTO records(instance_id, payload) VALUES(?, ?)",
                        ((instance_id, bytes(p)) for p in chunk),
                    )
        except BaseException:
            self._conn.rollback()
            avatar_path.unlink(missing_ok=True)
            raise
        return instance_id

    def get_account(self, instance_id: str, now: datetime) -> AccountView:
        """查看实例。已过截止时刻或已清理时抛 AccountExpiredError。"""
        now_ts = self._now_ts(now)
        self._conn.commit()
        row = self._get_row(instance_id)
        deadline = row[4]
        if deadline is not None and now_ts >= deadline:
            raise AccountExpiredError(instance_id)

        conn = self._conn

        def _iter_records() -> Iterable[bytes]:
            cur = conn.execute(
                "SELECT payload FROM records WHERE instance_id=? ORDER BY rowid",
                (instance_id,),
            )
            for (payload,) in cur:
                yield bytes(payload)

        avatar_path = self._avatar_path(instance_id)
        return AccountView(
            instance_id=row[0],
            username=row[1],
            created_at=_from_ts(row[2]),
            deletion_deadline=None if deadline is None else _from_ts(deadline),
            records=iter(_iter_records()),
            avatar_path=str(avatar_path) if avatar_path.exists() else None,
        )

    # ---- 销户申请与撤销 ----

    def request_deletion(self, instance_id: str, now: datetime) -> datetime:
        """首次申请进入宽限期并返回截止时刻；重复申请不延长截止。"""
        now_ts = self._now_ts(now)
        row = self._get_row(instance_id)
        deadline = row[4]
        if deadline is not None:
            if now_ts >= deadline:
                raise AccountExpiredError(instance_id)
            self._conn.commit()
            return _from_ts(deadline)  # 重复申请：截止时刻不变
        deadline = now_ts + GRACE_PERIOD.total_seconds()
        with self._conn:
            self._conn.execute(
                "UPDATE accounts SET deletion_requested_at=?,"
                " deletion_deadline=? WHERE instance_id=?",
                (now_ts, deadline, instance_id),
            )
        return _from_ts(deadline)

    def cancel_deletion(self, instance_id: str, now: datetime) -> None:
        """截止前撤销申请，实例及资料保持不变。"""
        now_ts = self._now_ts(now)
        row = self._get_row(instance_id)
        deadline = row[4]
        if deadline is None:
            raise AccountError("实例没有待撤销的销户申请")
        if now_ts >= deadline:
            raise AccountExpiredError(instance_id)
        with self._conn:
            self._conn.execute(
                "UPDATE accounts SET deletion_requested_at=NULL,"
                " deletion_deadline=NULL WHERE instance_id=?",
                (instance_id,),
            )

    # ---- 维护清理 ----

    def maintenance(self, budget: int, now: datetime) -> MaintenanceReport:
        """按预算清理已到期实例；可跨多次调用，崩溃后可续清。"""
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ValueError("预算必须为正整数")
        now_ts = self._now_ts(now)
        self._conn.commit()
        remaining = budget
        records_deleted = 0
        avatars_deleted = 0
        completed = 0

        while remaining > 0:
            row = self._conn.execute(
                "SELECT instance_id, avatar_deleted FROM accounts"
                " WHERE deletion_deadline IS NOT NULL AND deletion_deadline <= ?"
                " ORDER BY deletion_deadline, instance_id LIMIT 1",
                (now_ts,),
            ).fetchone()
            if row is None:
                break
            instance_id, avatar_deleted = row
            # 1. 分批删除所属资料记录（计入预算），每批一个事务。
            while remaining > 0:
                batch = min(remaining, _DELETE_BATCH)
                cur = self._conn.execute(
                    "DELETE FROM records WHERE rowid IN"
                    " (SELECT rowid FROM records WHERE instance_id=? LIMIT ?)",
                    (instance_id, batch),
                )
                count = cur.rowcount
                if count == 0:
                    self._conn.rollback()
                    break
                self._hook("before_record_commit")
                self._conn.commit()
                self._hook("after_record_commit")
                records_deleted += count
                remaining -= count
                if count < batch:
                    break
            # 预算耗尽且资料未删完：实例留待下次维护。
            left = self._conn.execute(
                "SELECT 1 FROM records WHERE instance_id=? LIMIT 1",
                (instance_id,),
            ).fetchone()
            if left is not None:
                break  # remaining 必为 0，实例留待下次维护
            # 2. 删除头像文件（计入预算）。先删文件再置标志：
            #    崩溃后重试发现文件不存在，只补标志，幂等。
            if not avatar_deleted:
                path = self._avatar_path(instance_id)
                if path.exists():
                    if remaining <= 0:
                        break
                    self._hook("before_avatar_delete")
                    path.unlink()
                    self._hook("after_avatar_delete")
                    avatars_deleted += 1
                    remaining -= 1
                with self._conn:
                    self._conn.execute(
                        "UPDATE accounts SET avatar_deleted=1"
                        " WHERE instance_id=?",
                        (instance_id,),
                    )
                self._hook("after_avatar_commit")
            # 3. 资料与头像均已清除：删除含用户名的控制记录，
            #    写入不含个人信息的完成日志（控制状态更新不计预算）。
            self._hook("before_finalize_commit")
            with self._conn:
                self._conn.execute(
                    "DELETE FROM accounts WHERE instance_id=?", (instance_id,)
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO cleanup_log(instance_id, completed_at)"
                    " VALUES(?, ?)",
                    (instance_id, now_ts),
                )
            self._hook("after_finalize_commit")
            completed += 1

        return MaintenanceReport(
            records_deleted=records_deleted,
            avatars_deleted=avatars_deleted,
            instances_completed=completed,
        )

    def is_cleaned(self, instance_id: str) -> bool:
        """实例是否已清理完成（含个人信息控制记录均已清除）。"""
        row = self._conn.execute(
            "SELECT 1 FROM cleanup_log WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        return row is not None
