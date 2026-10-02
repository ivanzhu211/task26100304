"""对外暴露的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class AccountView:
    """查看账户实例时返回的只读快照。"""

    instance_id: str
    username: str
    records: List[bytes]
    avatar: bytes
    created_at: datetime
    closing: bool
    deadline: Optional[datetime]


@dataclass(frozen=True)
class MaintenanceReport:
    """一次维护调用的结果统计。"""

    deleted_records: int = 0
    deleted_avatars: int = 0
    completed_instances: List[str] = field(default_factory=list)
    budget: int = 0
