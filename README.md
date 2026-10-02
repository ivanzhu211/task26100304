# task26100304

## 可恢复的账户销户组件（`account_deletion`）

由上层调用的 Python 库：账户及所属资料记录存 SQLite，头像存本地目录。
客户申请销户后进入 30 天反悔期，到期后由预算化维护分批清除个人资料与头像，
维护可在任意位置被终止，重开同一数据库与目录后可继续清理。

### 快速开始

```python
from datetime import datetime, timezone
from account_deletion import AccountService

svc = AccountService("app.db", "avatars/")
now = datetime.now(timezone.utc)

# 建档：用户名 + 一组资料记录 + 一份头像，返回不透明实例标识
iid = svc.create_account("alice", [b"profile...", b"consent..."], b"<avatar bytes>", now)

# 查看（宽限期内可用；records 为惰性迭代器）
view = svc.get_account(iid, now)
for payload in view.records:
    ...

# 申请销户：返回截止时刻（申请时刻 + 30 个完整 24 小时）
deadline = svc.request_deletion(iid, now)   # 重复申请不延长截止
svc.cancel_deletion(iid, now)               # 截止前可撤销，数据不变

# 维护：正整数预算 = 本轮最多删除的（资料记录数 + 头像文件数）
report = svc.maintenance(budget=1000, now=now)
svc.is_cleaned(iid)   # 控制记录也已清除后才为 True
svc.close()
```

### 语义要点

- 所有操作接收调用方传入的 UTC 时刻（必须带时区），组件保证时刻单调不倒退。
- 到达截止时刻即不可查看、不可撤销，用户名立即可重新建档；
  新实例与旧实例完全隔离（头像按实例标识命名，不按用户名）。
- 清理只处理已到期实例；已撤销的申请不触发清理。
- 单次维护删除量不超预算，单实例可跨多轮清理；控制状态更新不计预算。
- 只有资料、头像与含用户名的控制记录全部清除后，`is_cleaned` 才为真；
  完成日志仅保留不透明实例标识与完成时刻。
- 维护在任意位置中断（进程退出）后，重新构造 `AccountService` 即可续清：
  已删内容不会因重试出错，未删内容不会被跳过。

### 运行测试

```bash
python3 -m pytest tests/ -q
```

### 文件结构

- `account_deletion/models.py` — 视图、报告与异常类型
- `account_deletion/storage.py` — SQLite 表结构
- `account_deletion/service.py` — 建档/查看/申请/撤销/维护
- `tests/` — 宽限期、崩溃恢复、预算与内存测试
