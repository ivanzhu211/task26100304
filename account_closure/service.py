"""对外服务接口：建档、查看、销户申请/撤销、可恢复清理维护。"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from .errors import (
    AccountClosureError,
    AccountNotFoundError,
    AccountNotViewableError,
    InvalidBudgetError,
    InvalidStateError,
    TimeRegressionError,
    UsernameOccupiedError,
)
from .models import AccountView, MaintenanceReport
from .store import STATE_ACTIVE, STATE_CLOSING, STATE_PURGED, Store

GRACE_PERIOD = timedelta(days=30)


class AccountClosureService:
    """由上层直接调用的库入口。各方法串行使用，不支持并发。

    crash_hook 仅供测试注入故障：在关键删除/提交点之后被调用，
    参数为阶段名（"records" / "avatar_file" / "avatar_marked" / "purged"）。
    """

    def __init__(
        self,
        db_path: str,
        avatar_dir: str,
        crash_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._store = Store(db_path)
        self._avatar_dir = Path(avatar_dir)
        self._avatar_dir.mkdir(parents=True, exist_ok=True)
        self._crash_hook = crash_hook

    def close(self) -> None:
        self._store.close()

    # ---- 内部工具 ----
    def _resolve_now(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        last = self._store.get_last_time()
        if last is not None and now < last:
            raise TimeRegressionError(
                f"传入时刻 {now.isoformat()} 早于已见时刻 {last.isoformat()}"
            )
        self._store.set_last_time(now)
        return now

    def _avatar_path(self, instance_id: str) -> Path:
        return self._avatar_dir / instance_id

    def _require_account(self, instance_id: str) -> dict:
        account = self._store.get_account(instance_id)
        if account is None:
            raise AccountNotFoundError(instance_id)
        return account

    def _hook(self, stage: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(stage)

    # ---- 建档 ----
    def create_account(
        self,
        username: str,
        records: List[bytes],
        avatar: bytes,
        now: datetime,
    ) -> str:
        """建档，返回不透明实例标识。用户名被占用时抛 UsernameOccupiedError。"""
        now = self._resolve_now(now)
        if self._store.find_occupying(username, now) is not None:
            raise UsernameOccupiedError(username)
        instance_id = uuid.uuid4().hex
        avatar_path = self._avatar_path(instance_id)
        avatar_path.write_bytes(avatar)
        try:
            self._store.insert_account_with_records(
                instance_id, username, now, records
            )
        except BaseException:
            avatar_path.unlink(missing_ok=True)
            raise
        return instance_id

    # ---- 查看 ----
    def get_account(self, instance_id: str, now: datetime) -> AccountView:
        """查看实例资料。已到期或已清除时抛 AccountNotViewableError。"""
        now = self._resolve_now(now)
        account = self._require_account(instance_id)
        state = account["state"]
        deadline = account["deadline"]
        if state == STATE_PURGED:
            raise AccountNotViewableError(instance_id)
        if state not in (STATE_ACTIVE, STATE_CLOSING):
            raise AccountNotViewableError(instance_id)
        if state == STATE_CLOSING and deadline is not None and deadline <= now:
            raise AccountNotViewableError(instance_id)
        return AccountView(
            instance_id=instance_id,
            username=account["username"],
            records=self._store.list_records(instance_id),
            avatar=self._avatar_path(instance_id).read_bytes(),
            created_at=account["created_at"],
            closing=(state == STATE_CLOSING),
            deadline=deadline,
        )

    # ---- 销户申请 ----
    def request_closure(self, instance_id: str, now: datetime) -> datetime:
        """首次申请进入宽限期并返回截止时刻；重复申请不延长截止时刻。"""
        now = self._resolve_now(now)
        account = self._require_account(instance_id)
        state = account["state"]
        if state == STATE_CLOSING:
            return account["deadline"]
        if state != STATE_ACTIVE:
            raise InvalidStateError(f"实例状态 {state} 不允许申请销户")
        deadline = now + GRACE_PERIOD
        self._store.mark_closing(instance_id, deadline)
        return deadline

    def cancel_closure(self, instance_id: str, now: datetime) -> None:
        """截止前撤销申请，实例及资料保持不变。"""
        now = self._resolve_now(now)
        account = self._require_account(instance_id)
        if account["state"] != STATE_CLOSING:
            raise InvalidStateError("实例不在销户宽限期内，无法撤销")
        if account["deadline"] <= now:
            raise InvalidStateError("已过截止时刻，不可撤销")
        self._store.mark_active(instance_id)

    # ---- 维护清理 ----
    def maintain(self, budget: int, now: datetime) -> MaintenanceReport:
        """按预算清理已到期实例，可在任意位置中断后重开继续。"""
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise InvalidBudgetError("预算必须是正整数")
        now = self._resolve_now(now)
        remaining = budget
        deleted_records = 0
        deleted_avatars = 0
        completed: List[str] = []

        for account in self._store.list_expired(now):
            if remaining <= 0:
                break
            instance_id = account["instance_id"]
            if account["state"] == STATE_CLOSING:
                self._store.mark_expired(instance_id)

            records_done = False
            while remaining > 0:
                count = self._store.delete_records_batch(instance_id, remaining)
                deleted_records += count
                remaining -= count
                self._hook("records")
                if count == 0:
                    records_done = True
                    break
            if not records_done:
                records_done = not self._store.has_records(instance_id)

            avatar_done = account["avatar_deleted"]
            if records_done and not avatar_done and remaining > 0:
                self._avatar_path(instance_id).unlink(missing_ok=True)
                self._hook("avatar_file")
                self._store.set_avatar_deleted(instance_id)
                self._hook("avatar_marked")
                avatar_done = True
                deleted_avatars += 1
                remaining -= 1

            if records_done and avatar_done:
                self._store.mark_purged(instance_id, now)
                self._hook("purged")
                completed.append(instance_id)

        return MaintenanceReport(
            deleted_records=deleted_records,
            deleted_avatars=deleted_avatars,
            completed_instances=completed,
            budget=budget,
        )
