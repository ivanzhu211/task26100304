"""预算约束与最终保留集对照：大账户跨多轮清理，未到期/已撤销账户完整保留。"""
import sqlite3
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest

from account_deletion import AccountService

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUDGET = 1000


def make_service(tmp_path, fault_hook=None):
    return AccountService(
        tmp_path / "app.db", tmp_path / "avatars", fault_hook=fault_hook
    )


def rec(tag, i):
    return b"%s-rec-%06d" % (tag, i)


def av(tag):
    return b"%s-AVATAR-BYTES" % tag


@pytest.fixture
def mixed(tmp_path):
    """A 已到期（5000 条）；B 正常；C 申请后撤销；D 宽限期未到期。"""
    svc = make_service(tmp_path)
    a = svc.create_account("a", (rec(b"a", i) for i in range(5000)), av(b"a"), T0)
    b = svc.create_account("b", (rec(b"b", i) for i in range(100)), av(b"b"), T0)
    c = svc.create_account("c", (rec(b"c", i) for i in range(50)), av(b"c"), T0)
    d = svc.create_account("d", (rec(b"d", i) for i in range(20)), av(b"d"), T0)
    svc.request_deletion(a, T0)
    svc.request_deletion(c, T0)
    svc.cancel_deletion(c, T0 + timedelta(days=1))
    svc.request_deletion(d, T0 + timedelta(days=10))
    svc.close()
    ids = {"a": a, "b": b, "c": c, "d": d}
    return ids


def count_records(db_path, instance_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM records WHERE instance_id=?", (instance_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_budget_never_exceeded_and_final_retained_set(tmp_path, mixed):
    ids = mixed
    now = T0 + timedelta(days=31)  # A 到期；D 的截止在 T0+40 天，未到期
    svc = make_service(tmp_path)

    rounds = []
    while True:
        before = count_records(tmp_path / "app.db", ids["a"])
        report = svc.maintenance(BUDGET, now)
        spent = report.records_deleted + report.avatars_deleted
        assert 0 <= spent <= BUDGET  # 每轮删除量不超预算
        after = count_records(tmp_path / "app.db", ids["a"])
        assert before - after == report.records_deleted  # 报告与实际一致
        rounds.append((report.records_deleted, report.avatars_deleted))
        if spent == 0:
            break

    # 5000 条记录 + 1 个头像，预算 1000：至少 6 轮有实际删除
    assert sum(r for r, _ in rounds) == 5000
    assert sum(a for _, a in rounds) == 1
    assert len([r for r, _ in rounds if r > 0]) >= 5
    assert svc.is_cleaned(ids["a"])
    assert not (tmp_path / "avatars" / f"{ids['a']}.avatar").exists()

    # 独立计算的应保留集合：B/C/D 的全部记录与头像字节
    expected_records = {
        tag: {rec(tag.encode(), i) for i in n}
        for tag, n in (("b", range(100)), ("c", range(50)), ("d", range(20)))
    }
    for tag in ("b", "c", "d"):
        view = svc.get_account(ids[tag], now)
        assert set(view.records) == expected_records[tag]
        with open(view.avatar_path, "rb") as fh:
            assert fh.read() == av(tag.encode())
    svc.close()


def test_single_instance_spans_rounds_without_breaking_budget(tmp_path):
    svc = make_service(tmp_path)
    iid = svc.create_account(
        "big", (rec(b"x", i) for i in range(2500)), av(b"x"), T0
    )
    svc.request_deletion(iid, T0)
    now = T0 + timedelta(days=31)
    seen = []
    while not svc.is_cleaned(iid):
        report = svc.maintenance(BUDGET, now)
        assert report.records_deleted + report.avatars_deleted <= BUDGET
        seen.append((report.records_deleted, report.avatars_deleted))
    assert sum(r for r, _ in seen) == 2500
    assert sum(a for _, a in seen) == 1
    svc.close()


def test_invalid_budget_rejected(tmp_path):
    svc = make_service(tmp_path)
    for bad in (0, -1, 1.5, "1000", True):
        with pytest.raises(ValueError):
            svc.maintenance(bad, T0)
    svc.close()


def test_maintenance_memory_bounded(tmp_path):
    """单账户 10 万条记录，预算 1000：维护峰值内存远低于 64MiB。"""
    svc = make_service(tmp_path)
    iid = svc.create_account(
        "huge", (rec(b"m", i) for i in range(100_000)), av(b"m"), T0
    )
    svc.request_deletion(iid, T0)
    now = T0 + timedelta(days=31)
    tracemalloc.start()
    svc.maintenance(BUDGET, now)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 32 * 1024 * 1024  # 远低于 64MiB 上限
    # 未删完，不得报告完成
    assert not svc.is_cleaned(iid)
    svc.close()
