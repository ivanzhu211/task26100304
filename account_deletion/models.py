"""类型定义：账户视图、维护报告与异常。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional


STATE_ACTIVE = "active"
STATE_GRACE = "grace"
STATE_CLEANED = "cleaned"


@dataclass(frozen=True)
class AccountView:
    """账户实例只读视图。records 为惰性迭代器，避免大账户撑爆内存。"""
    instance_id: str
    username: str
    created_at: datetime
    deletion_deadline: Optional[datetime]
    records: Iterator[bytes]
    avatar_path: Optional[str]


@dataclass(frozen=True)
class MaintenanceReport:
    """单次维护的结果统计。"""
    records_deleted: int
    avatars_deleted: int
    instances_completed: int


class AccountError(Exception):
    """组件基础异常。"""


class AccountNotFoundError(AccountError):
    """实例不存在或已清理。"""


class UsernameOccupiedError(AccountError):
    """用户名仍被未到期实例占用。"""


class AccountExpiredError(AccountError):
    """实例已过宽限截止时刻，不可查看或撤销。"""
