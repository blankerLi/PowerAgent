"""poweragent/controller/budget.py

`BudgetLedger`（design.md §6.2.3；tasks.md 任务 9.2；需求追溯 R15.7 ~ R15.11）。

## `reserve()` / `Store.open_run()` 的职责划分（设计判断，供审阅）

design.md §6.2.3 给出的签名是：

```python
def reserve(self, units: int, *, run_id: str) -> None:
    # 先占后跑：与 runs 行在同一事务写入 budget_units。
    ...
```


而 `Store.open_run()`（`store/repo.py`，任务 8.3，已落地）已经在**自己的**
`tx()` 事务内把 `budget_units` 与 `runs` 行一起原子写入（`INSERT INTO runs
(..., budget_units, ...) VALUES (...)`），且 `open_run()` 返回新生成的
`run_id`——这意味着调用 `reserve(units, run_id=run_id)` 时，`run_id` 对应的
`runs` 行必然已经存在（`reserve()` 的签名靠 `run_id` 引用一个既有行，而不是
接受 `open_run()` 需要的 `task_id`/`candidate`/`scenario`/`attempt`/
`simulation_key` 那一整套参数）。换言之，「与 runs 行在同一事务写入
`budget_units`」这件事已经由 `open_run()` 独立完成，`reserve()` 没有、也不需要
再对 `runs` 表做第二次写入——两个方法各自持有自己的 `tx()`，不存在把二者的写入
合并进同一个物理事务的接口空间（`Store` 没有暴露"在调用方已开的事务内插入
一行"的方法，也不应该暴露：那会让 `BudgetLedger` 反过来依赖 `Store` 的内部
连接，破坏 §6.9 定的层边界）。

本实现把 `reserve()` 定为**先占检查闸门**：调用方（`controller/run_task.py`，
任务 14.4/14.5，尚未实现）在决定发起真实仿真之前，先算出本次所需的 `units`
（`1`，`require_margin=True` 时加 `extra_engine_starts_per_candidate`），调用
`ledger.reserve(units, run_id=...)` 做一次 `remaining() >= units` 的检查——
不足则抛 `BudgetExhaustedError`、不写任何东西、不调用 `open_run()`、不启动
Simulink（对应 R15.10）；充足则正常返回，调用方随后自行调用
`store.open_run(..., budget_units=units)`，由 `open_run()` 的事务完成
「写 `runs` 行与 `budget_units` 同一事务、事务回滚则两者同时不生效」这条
design.md 字面要求的原子性（R15.7 的原子性保证事实上落在 `open_run()` 里，
`reserve()` 是它的前置门禁而非它的替代实现）。

`run_id` 参数在这个实现里只用于错误消息（"reserve for run_id=X 被拒绝"），
不引用任何数据库行——因为在 `reserve()` 被调用的时点，`open_run()` 尚未被
调用，此时还没有 `run_id` 可引用。调用方在还没有真实 `run_id` 时可传入任意
便于定位问题的字符串（例如即将开的 `(candidate_id, scenario_id, attempt)`
的组合描述）；这与"`reserve()` 与 `open_run()` 谁先谁后"的自然顺序一致：先
`reserve()` 检查，检查通过才 `open_run()` 拿到真正的 `run_id`。

这是本任务两个已落地、不可更改的接口签名（`Store.open_run()` 的既有实现与
`BudgetLedger.reserve()` 的既有签名）之间的一处不完全咬合：字面意义上的
"三者（检查 + 写 runs 行 + 记 budget_units）在同一个事务内完成"无法仅凭
`BudgetLedger` 这一层实现——`open_run()` 早已独立承担了写入部分的原子性。
本实现选择让 `reserve()` 只承担检查职责，是在不改动已落地代码、不给
`Store` 增加"半开事务"这种脏接口的前提下最小的自洽方案。
"""

from __future__ import annotations

from poweragent.store.repo import Store

__all__ = ["BudgetExhaustedError", "BudgetLedger"]


class BudgetExhaustedError(RuntimeError):
    """`reserve()` 发现 `remaining() < units` 时抛出；抛出前不执行任何写入、
    不调用 `store.open_run()`、不启动 Simulink（R15.10）。"""


class BudgetLedger:
    """从 SQLite 完全重建的预算账本（design.md §6.2.3）。

    不在 `__init__` 读数据库、不缓存 `used()`/`remaining()` 的计算结果——
    每次调用都重新查询 `runs` 与 `approvals` 两张表，因此崩溃重启后无需任何
    恢复步骤即可继续使用（R15.11）。
    """

    def __init__(
        self, store: Store, task_id: str, max_starts: int, max_wallclock_s: float
    ) -> None:
        self.store = store
        self.task_id = task_id
        self.max_starts = max_starts
        self.max_wallclock_s = max_wallclock_s

    def used(self) -> int:
        """`SUM(runs.budget_units)`（按 `task_id` 过滤），不排除任何状态的行
        （含 `failed`、`cause='process_restart'`、`cache_hit=1`），无行时取 0
        （R15.9）。"""
        return self.store.sum_budget_units(self.task_id)

    def remaining(self) -> int:
        """`max_starts + SUM(approvals.extra_units WHERE task_id=? AND
        kind='budget_increase' AND decision='approve') − used()`，完全从
        SQLite 重建（R15.11）。"""
        return (
            self.max_starts
            + self.store.sum_approved_budget_increase(self.task_id)
            - self.used()
        )

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def reserve(self, units: int, *, run_id: str) -> None:
        """先占检查闸门：`remaining() < units` 时抛 `BudgetExhaustedError`、
        不写入任何东西；否则正常返回，调用方随后调用
        `store.open_run(..., budget_units=units)` 完成实际的原子写入（见
        模块 docstring 的职责划分说明）。"""
        if self.remaining() < units:
            raise BudgetExhaustedError(
                f"reserve: run_id={run_id!r} needs units={units} but only "
                f"{self.remaining()} remain (task_id={self.task_id!r})"
            )

    def grant(self, extra_units: int, *, approval_id: str) -> None:
        """经 `store.grant_budget()` 把追加额度写入该 `approvals` 行的
        `extra_units` 列；本方法不改变任何内存计数器（R15.11）。

        同一 `approval_id` 至多计入一次由 `remaining()` 的求和方式结构性
        保证：`approval_id` 在 `approvals` 表中对应恰一行，`grant_budget()`
        对同一 `approval_id` 的重复调用只覆盖该行的 `extra_units`、不产生
        第二行，`SUM(approvals.extra_units ...)` 因此不会重复计入同一行两次
        （无需额外的幂等性跟踪代码）。
        """
        self.store.grant_budget(
            task_id=self.task_id, extra_units=extra_units, approval_id=approval_id
        )
