"""崩溃恢复：维护在任意关键点中断后，重开库可续清且不影响新实例。"""
from datetime import datetime, timedelta, timezone

import pytest

from account_deletion import AccountService

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Crash(Exception):
    pass


def make_service(tmp_path, fault_hook=None):
    return AccountService(
        tmp_path / "app.db", tmp_path / "avatars", fault_hook=fault_hook
    )


def setup_expired_pair(tmp_path):
    """旧实例已到期，用户名被新实例复用。返回 (旧id, 新id, 截止时刻)。"""
    svc = make_service(tmp_path)
    old_id = svc.create_account(
        "alice", [b"old-%d" % i for i in range(5)], b"OLD-AVATAR", T0
    )
    deadline = svc.request_deletion(old_id, T0)
    new_id = svc.create_account(
        "alice", [b"new-%d" % i for i in range(3)], b"NEW-AVATAR", deadline
    )
    svc.close()
    return old_id, new_id, deadline


def reopen(tmp_path):
    """模拟进程重启：全新连接与服务实例。"""
    return make_service(tmp_path)


def assert_new_intact(svc, new_id, now):
    view = svc.get_account(new_id, now)
    assert list(view.records) == [b"new-0", b"new-1", b"new-2"]
    with open(view.avatar_path, "rb") as fh:
        assert fh.read() == b"NEW-AVATAR"


def drain(svc, now):
    """反复维护直到没有可清理对象。"""
    total = svc.maintenance(1000, now)
    while True:
        report = svc.maintenance(1000, now)
        if (
            report.records_deleted == 0
            and report.avatars_deleted == 0
            and report.instances_completed == 0
        ):
            break
    return total


@pytest.mark.parametrize(
    "crash_point",
    [
        "before_record_commit",   # 记录删除后、提交前
        "after_record_commit",    # 记录提交后
        "before_avatar_delete",   # 头像文件删除前
        "after_avatar_delete",    # 头像文件删除后、标志提交前
        "after_avatar_commit",    # 头像标志提交后
        "before_finalize_commit", # 控制记录删除提交前
        "after_finalize_commit",  # 控制记录提交后
    ],
)
def test_crash_at_each_point_then_resume(tmp_path, crash_point):
    old_id, new_id, deadline = setup_expired_pair(tmp_path)

    def hook(point):
        if point == crash_point:
            raise Crash(point)

    svc = make_service(tmp_path, fault_hook=hook)
    with pytest.raises(Crash):
        svc.maintenance(1000, deadline + timedelta(days=1))
    svc.close()

    # 重启：新实例始终完整
    svc = reopen(tmp_path)
    now = deadline + timedelta(days=1)
    assert_new_intact(svc, new_id, now)
    # 继续维护：旧实例最终清空，不提前报告完成
    drain(svc, now)
    assert svc.is_cleaned(old_id)
    assert not svc.is_cleaned(new_id)
    assert_new_intact(svc, new_id, now)
    # 旧实例头像文件已不存在
    assert not (tmp_path / "avatars" / f"{old_id}.avatar").exists()
    svc.close()


def test_repeated_crashes_still_converge(tmp_path):
    old_id, new_id, deadline = setup_expired_pair(tmp_path)
    now = deadline + timedelta(days=1)
    points = [
        "before_record_commit",
        "after_avatar_delete",
        "before_finalize_commit",
    ]
    for point in points:
        def hook(p, point=point):
            if p == point:
                raise Crash(p)

        svc = make_service(tmp_path, fault_hook=hook)
        with pytest.raises(Crash):
            svc.maintenance(1000, now)
        svc.close()
    svc = reopen(tmp_path)
    drain(svc, now)
    assert svc.is_cleaned(old_id)
    assert_new_intact(svc, new_id, now)
    svc.close()


def test_old_cleanup_never_touches_reused_username_instance(tmp_path):
    old_id, new_id, deadline = setup_expired_pair(tmp_path)
    svc = reopen(tmp_path)
    now = deadline + timedelta(days=1)
    drain(svc, now)
    # 新实例不继承旧实例的数据与销户状态
    view = svc.get_account(new_id, now)
    assert view.deletion_deadline is None
    assert_new_intact(svc, new_id, now)
    svc.close()
