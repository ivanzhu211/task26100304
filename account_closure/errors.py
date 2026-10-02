"""组件异常类型定义。"""


class AccountClosureError(Exception):
    """组件内所有异常的基类。"""


class UsernameOccupiedError(AccountClosureError):
    """用户名当前仍被未到期实例占用。"""


class AccountNotFoundError(AccountClosureError):
    """账户实例不存在。"""


class AccountNotViewableError(AccountClosureError):
    """实例已过截止时刻或已清除，个人资料不可再查看。"""


class InvalidStateError(AccountClosureError):
    """当前实例状态不允许该操作。"""


class InvalidBudgetError(AccountClosureError):
    """维护预算必须是正整数。"""


class TimeRegressionError(AccountClosureError):
    """传入的UTC时刻早于组件已见过的时刻。"""
