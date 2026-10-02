"""账户销户组件验收测试。"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from account_closure import (
    AccountClosureService,
    AccountNotViewableError,
    GRACE_PERIOD,
    InvalidBudgetError,
    InvalidStateError,
    TimeRegressionError,
    UsernameOccupiedError,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Crash(Exception):
    pass


@pytest.fixture()
def env(tmp_path):
    return {
        "db": str(tmp_path / "closure.db"),
        "avatars": str(tmp_path / "avatars"),
    }


def open_service(env, crash_hook=None):
    return AccountClosureService(env["db"], env["avatars"], crash_hook=crash_hook)


def avatar_file(env, instance_id):
    return os.path.join(env["avatars"], instance_id)


# ---------- 建档与查看 ----------

def test_create_and_view(env):
    svc = open_service(env)
    iid = svc.create_account("alice", [b"r1", b"r2"], b"AVATAR", T0)
    view = svc.get_account(iid, T0)
    assert view.username == "alice"
    assert view.records == [b"r1", b"r2"]
    assert view.avatar == b"AVATAR"
    assert view.closing is False and view.deadline is None


def test_username_occupied_while_active_and_in_grace(env):
    svc = open_service(env)
    svc.create_account("alice", [], b"A", T0)
    with pytest.raises(UsernameOccupiedError):
        svc.create_account("alice", [], b"B", T0 + timedelta(days=1))
    iid = svc.create_account("bob", [], b"B", T0 + timedelta(days=1))
    svc.request_closure(iid, T0 + timedelta(days=2))
    with pytest.raises(UsernameOccupiedError):
        svc.create_account("bob", [], b"C", T0 + timedelta(days=3))


def test_time_regression_rejected(env):
    svc = open_service(env)
    svc.create_account("alice", [], b"A", T0 + timedelta(days=1))
    with pytest.raises(TimeRegressionError):
        svc.create_account("bob", [], b"B", T0)


# ---------- 撤销与重新申请 ----------

def test_cancel_then_reapply_resets_deadline(env):
    svc = open_service(env)
    iid = svc.create_account("alice", [b"r1"], b"A", T0)
    first_deadline = svc.request_closure(iid, T0)
    assert first_deadline == T0 + GRACE_PERIOD
    svc.cancel_closure(iid, T0 + timedelta(days=10))
    second_deadline = svc.request_closure(iid, T0 + timedelta(days=20))
    assert second_deadline == T0 + timedelta(days=20) + GRACE_PERIOD

    # 原截止时刻到达：不得触发清理，资料完整
    report = svc.maintain(100, first_deadline + timedelta(days=5))
    assert report.deleted_records == 0 and report.deleted_avatars == 0
    view = svc.get_account(iid, first_deadline + timedelta(days=5))
    assert view.records == [b"r1"] and view.avatar == b"A"

    # 新截止时刻到达：清理生效
    report = svc.maintain(100, second_deadline)
    assert report.deleted_records == 1 and report.deleted_avatars == 1
    assert report.completed_instances == [iid]


def test_repeated_request_does_not_extend_deadline(env):
    svc = open_service(env)
    iid = svc.create_account("alice", [b"r1"], b"A", T0)
    deadline = svc.request_closure(iid, T0)
    assert svc.request_closure(iid, T0 + timedelta(days=29)) == deadline
    report = svc.maintain(10, deadline)
    assert report.completed_instances == [iid]


def test_cancel_after_deadline_rejected(env):
    svc = open_service(env)
    iid = svc.create_account("alice", [], b"A", T0)
    deadline = svc.request_closure(iid, T0)
    with pytest.raises(InvalidStateError):
        svc.cancel_closure(iid, deadline)


def test_view_during_grace_but_not_after_deadline(env):
    svc = open_service(env)
    iid = svc.create_account("alice", [b"r1"], b"A", T0)
    deadline = svc.request_closure(iid, T0)
    view = svc.get_account(iid, deadline - timedelta(seconds=1))
    assert view.closing and view.records == [b"r1"]
    with pytest.raises(AccountNotViewableError):
        svc.get_account(iid, deadline)


# ---------- 到期后复用用户名 + 中断恢复 ----------

@pytest.mark.parametrize("crash_stage", ["records", "avatar_file", "avatar_marked", "purged"])
def test_crash_during_cleanup_then_resume(env, crash_stage):
    svc = open_service(env)
    old_id = svc.create_account("alice", [b"old1", b"old2", b"old3"], b"OLD_AVATAR", T0)
    deadline = svc.request_closure(old_id, T0)

    # 到期后用户名立即可复用，新实例不继承旧数据
    new_id = svc.create_account("alice", [b"new1"], b"NEW_AVATAR", deadline)
    assert new_id != old_id

    # 第一次维护在指定阶段崩溃
    def hook(stage):
        if stage == crash_stage:
            raise Crash(stage)

    crashing = open_service(env, crash_hook=hook)
    with pytest.raises(Crash):
        crashing.maintain(100, deadline + timedelta(days=1))

    # 重启后继续清理：新实例始终完整
    svc2 = open_service(env)
    view = svc2.get_account(new_id, deadline + timedelta(days=1))
    assert view.records == [b"new1"] and view.avatar == b"NEW_AVATAR"

    report = svc2.maintain(100, deadline + timedelta(days=2))
    if crash_stage != "purged":
        # purged 阶段崩溃时清理已提交完成，重开后不再重复报告
        assert old_id in report.completed_instances
    # 旧实例彻底清空
    with pytest.raises(AccountNotViewableError):
        svc2.get_account(old_id, deadline + timedelta(days=2))
    assert not os.path.exists(avatar_file(env, old_id))
    # 新实例不受影响
    view = svc2.get_account(new_id, deadline + timedelta(days=2))
    assert view.records == [b"new1"] and view.avatar == b"NEW_AVATAR"
    assert os.path.exists(avatar_file(env, new_id))


def test_username_reusable_at_deadline_without_cleanup(env):
    svc = open_service(env)
    old_id = svc.create_account("alice", [b"old"], b"OLD", T0)
    deadline = svc.request_closure(old_id, T0)
    # 到达截止时刻即可重新建档，不必等待物理清理
    new_id = svc.create_account("alice", [b"new"], b"NEW", deadline)
    view = svc.get_account(new_id, deadline)
    assert view.records == [b"new"] and not view.closing


# ---------- 预算与跨轮清理 ----------

def test_budget_must_be_positive_int(env):
    svc = open_service(env)
    for bad in (0, -1, 2.5, "10", True):
        with pytest.raises(InvalidBudgetError):
            svc.maintain(bad, T0)


def test_large_account_cleanup_across_rounds(env):
    svc = open_service(env)
    total_records = 4321
    records = [f"rec-{i}".encode() for i in range(total_records)]
    iid = svc.create_account("heavy", records, b"BIG_AVATAR", T0)
    deadline = svc.request_closure(iid, T0)

    # 未到期维护不得删除任何内容
    report = svc.maintain(1000, deadline - timedelta(seconds=1))
    assert report.deleted_records == 0 and report.deleted_avatars == 0

    budget = 1000
    now = deadline
    remaining = set(records)
    avatar_remaining = {b"BIG_AVATAR"}
    rounds = 0
    while True:
        report = svc.maintain(budget, now)
        rounds += 1
        assert report.deleted_records + report.deleted_avatars <= budget
        # 逐轮核对：本轮删除量不超过预算，且删除的是应删集合中的元素
        if report.deleted_records:
            deleted_now = svc_store_remaining(env, iid)
            removed = remaining - set(deleted_now)
            assert len(removed) == report.deleted_records
            remaining = set(deleted_now)
        if report.deleted_avatars:
            assert not os.path.exists(avatar_file(env, iid))
            avatar_remaining = set()
        if report.completed_instances:
            assert report.completed_instances == [iid]
            break
        assert report.deleted_records + report.deleted_avatars > 0 or rounds > 100
        now += timedelta(days=1)
    assert rounds >= (total_records + 1 + budget - 1) // budget
    # 最终保留集合与独立计算的应保留集合对照：应为空
    assert remaining == set() and avatar_remaining == set()
    assert svc_store_remaining(env, iid) == []


def svc_store_remaining(env, instance_id):
    """独立查询：直接读库获取实例剩余资料。"""
    import sqlite3
    conn = sqlite3.connect(env["db"])
    try:
        rows = conn.execute(
            "SELECT payload FROM records WHERE instance_id = ?", (instance_id,)
        ).fetchall()
        return [bytes(r[0]) for r in rows]
    finally:
        conn.close()


def test_budget_never_exceeded_per_round(env):
    svc = open_service(env)
    ids = []
    for i in range(5):
        iid = svc.create_account(f"u{i}", [b"x"] * 300, b"A", T0)
        svc.request_closure(iid, T0)
        ids.append(iid)
    now = T0 + GRACE_PERIOD
    completed = set()
    while len(completed) < len(ids):
        report = svc.maintain(1000, now)
        assert report.deleted_records + report.deleted_avatars <= 1000
        completed.update(report.completed_instances)
        now += timedelta(days=1)


# ---------- 终止条件：保留未到期与已撤销账户 ----------

def test_repeated_maintenance_preserves_unexpired_and_cancelled(env):
    svc = open_service(env)
    doomed = svc.create_account("doomed", [b"d1", b"d2"], b"D", T0)
    svc.request_closure(doomed, T0)

    cancelled = svc.create_account("cancelled", [b"c1"], b"C", T0 + timedelta(days=1))
    svc.request_closure(cancelled, T0 + timedelta(days=1))
    svc.cancel_closure(cancelled, T0 + timedelta(days=2))

    alive = svc.create_account("alive", [b"a1"], b"A", T0 + timedelta(days=2))

    now = T0 + GRACE_PERIOD + timedelta(days=365)
    for _ in range(10):
        svc.maintain(1, now)
        now += timedelta(days=1)

    with pytest.raises(AccountNotViewableError):
        svc.get_account(doomed, now)
    assert svc.get_account(cancelled, now).records == [b"c1"]
    assert svc.get_account(alive, now).records == [b"a1"]
    assert os.path.exists(avatar_file(env, cancelled))
    assert os.path.exists(avatar_file(env, alive))
    # 已撤销账户再次申请，重新计算宽限期
    new_deadline = svc.request_closure(cancelled, now)
    assert new_deadline == now + GRACE_PERIOD
