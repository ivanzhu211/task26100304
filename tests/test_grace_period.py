"""宽限期规则：撤销、重新申请、截止时刻语义。"""
from datetime import datetime, timedelta, timezone

import pytest

from account_deletion import (
    AccountExpiredError,
    AccountService,
    UsernameOccupiedError,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_service(tmp_path):
    return AccountService(tmp_path / "app.db", tmp_path / "avatars")


def test_request_sets_deadline_and_repeat_does_not_extend(tmp_path):
    svc = make_service(tmp_path)
    iid = svc.create_account("alice", [b"r1", b"r2"], b"PNG", T0)
    t_req = T0 + timedelta(days=5)
    deadline = svc.request_deletion(iid, t_req)
    assert deadline == t_req + timedelta(days=30)
    # 重复申请不延长截止时刻
    again = svc.request_deletion(iid, t_req + timedelta(days=10))
    assert again == deadline


def test_view_and_username_occupied_during_grace(tmp_path):
    svc = make_service(tmp_path)
    iid = svc.create_account("alice", [b"r1"], b"PNG", T0)
    svc.request_deletion(iid, T0)
    view = svc.get_account(iid, T0 + timedelta(days=29))
    assert list(view.records) == [b"r1"]
    assert view.avatar_path is not None
    with pytest.raises(UsernameOccupiedError):
        svc.create_account("alice", [b"x"], b"Y", T0 + timedelta(days=29))


def test_cancel_then_reapply_recomputes_deadline(tmp_path):
    svc = make_service(tmp_path)
    iid = svc.create_account("alice", [b"r1", b"r2"], b"AVATAR", T0)
    d1 = svc.request_deletion(iid, T0)
    svc.cancel_deletion(iid, T0 + timedelta(days=10))
    # 撤销后资料完整、用户名仍被占用
    view = svc.get_account(iid, T0 + timedelta(days=10))
    assert list(view.records) == [b"r1", b"r2"]
    with pytest.raises(UsernameOccupiedError):
        svc.create_account("alice", [], b"Z", T0 + timedelta(days=10))
    # 再次申请：重新计算宽限期
    t2 = T0 + timedelta(days=20)
    d2 = svc.request_deletion(iid, t2)
    assert d2 == t2 + timedelta(days=30)
    assert d2 != d1
    # 旧截止时刻到达时不得触发清理（旧申请已撤销）
    report = svc.maintenance(100, d1 + timedelta(seconds=1))
    assert report.records_deleted == 0 and report.instances_completed == 0
    assert list(svc.get_account(iid, d1 + timedelta(seconds=1)).records) == [
        b"r1",
        b"r2",
    ]
    # 到达新截止时刻才可清理
    report = svc.maintenance(100, d2)
    assert report.records_deleted == 2
    assert report.avatars_deleted == 1
    assert report.instances_completed == 1
    assert svc.is_cleaned(iid)


def test_expiry_blocks_view_and_cancel_and_frees_username(tmp_path):
    svc = make_service(tmp_path)
    iid = svc.create_account("alice", [b"r1"], b"A", T0)
    deadline = svc.request_deletion(iid, T0)
    with pytest.raises(AccountExpiredError):
        svc.get_account(iid, deadline)
    with pytest.raises(AccountExpiredError):
        svc.cancel_deletion(iid, deadline)
    with pytest.raises(AccountExpiredError):
        svc.request_deletion(iid, deadline)
    # 用户名立即可复用，无需等待物理清理
    new_id = svc.create_account("alice", [b"new"], b"B", deadline)
    assert new_id != iid
    assert list(svc.get_account(new_id, deadline).records) == [b"new"]


def test_time_does_not_go_backwards(tmp_path):
    svc = make_service(tmp_path)
    iid = svc.create_account("alice", [b"r1"], b"A", T0)
    deadline = svc.request_deletion(iid, T0)
    # 时刻推进到截止后，再传入倒退的时刻：仍按已到期处理
    with pytest.raises(AccountExpiredError):
        svc.get_account(iid, deadline)
    with pytest.raises(AccountExpiredError):
        svc.get_account(iid, T0 + timedelta(days=1))


def test_naive_datetime_rejected(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ValueError):
        svc.create_account("alice", [], b"A", datetime(2026, 1, 1))
