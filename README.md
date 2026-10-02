# task26100304

可恢复的账户销户组件（Python + SQLite + 本地文件目录），以库形式供上层调用。

## 功能

- 建档：接收用户名、一组所属资料记录（`bytes` 列表）和一份头像（`bytes`），每次建档形成独立账户实例，返回不透明实例标识。
- 查看：按实例标识读取用户名、全部资料记录和头像。
- 销户申请：首次申请进入 30 天宽限期（30 个完整 24 小时），重复申请不延长截止时刻；截止前可撤销，撤销后再次申请重新计算宽限期。
- 维护清理：调用方给出正整数预算，单次实际删除的资料记录数与头像文件数之和不超过预算；实例可跨多次维护清理；进程在任意位置被杀后，重新打开同一数据库与目录即可继续。

## 使用

```python
from datetime import datetime, timezone
from account_closure import AccountClosureService

svc = AccountClosureService("closure.db", "avatars/")
now = datetime.now(timezone.utc)

# 建档（用户名被活跃或宽限期内实例占用时抛 UsernameOccupiedError）
iid = svc.create_account("alice", [b"profile...", b"consent..."], b"<avatar bytes>", now)

# 查看（到期或清除后抛 AccountNotViewableError）
view = svc.get_account(iid, now)

# 申请销户，返回截止时刻；撤销用 svc.cancel_closure(iid, now)
deadline = svc.request_closure(iid, now)

# 维护：budget 为正整数，返回 MaintenanceReport
report = svc.maintain(budget=1000, now=deadline)
print(report.deleted_records, report.deleted_avatars, report.completed_instances)
```

## 语义要点

- 所有方法接收调用方传入的 UTC 时刻；组件在库内保证时刻单调不倒退，倒退时抛 `TimeRegressionError`。
- 宽限期内可查看、用户名仍被占用；到达截止时刻即不可撤销、不可查看，用户名立即可重新建档，无需等待物理清理。新实例不继承旧实例的任何数据与销户状态。
- 清理只处理已到期的实例；撤销过的申请不触发清理。清理完成（资料、头像、含用户名的控制记录均清除）后仅保留不透明实例标识与完成状态。
- 头像按实例标识命名存放，与用户名解耦，复用用户名的新实例不会被旧实例的清理影响。
- 删除以数据库正常查询不可取得、头像路径不存在为准；已完成的删除重试不报错，未完成的删除不会被永久跳过。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：截止前撤销并重新申请（原截止时刻不提前删除）、到期后复用用户名、清理在文件删除与数据库提交前后各阶段中断后重启恢复、单账户资料量远超预算时逐轮核对删除量并对照最终保留集合。
