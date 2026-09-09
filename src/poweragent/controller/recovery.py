"""poweragent/controller/recovery.py

`reap_orphan_runs()` 与 `rebuild_state()`（design.md §6.2.5 / §8.1 / §8.8；
tasks.md 任务 14.6；需求追溯 R16.12, R16.13；属性 CP-7）。

## `run_task.py`（任务 14.5）接线检查：本文件未修改 `controller/run_task.py`

任务描述要求检查 `controller/run_task.py` 是否已经为 `reap_orphan_runs()`
留了一个"文档化的缺口"（说明将来由本文件补上），如果有就把调用接上。经核对
`controller/run_task.py` 当前实际内容：该文件只落地了任务 14.4 的
`run_tier()`（外加 `TierResult` / 两个内部辅助函数），**不存在** `run_task()`
主循环函数本身——`tasks.md` 把该文件标注为任务 14.5 已完成，但截至本文件
撰写时，`run_task()` 这个顶层函数在 `controller/run_task.py` 中并不存在
（`cli.py` 的 `from poweragent.controller.run_task import run_task` 因此仍会
以 `ImportError` 失败，与 `cli.py` 自身文档中"`run_task()` 尚未落地"的分支
描述一致）。`design.md` §8.1 的 `reap_orphan_runs(store)` 调用点是 `run_task()`
函数体的第一行——没有 `run_task()` 函数体，就没有可以接入这一行调用的位置。

因此本文件**不**修改 `controller/run_task.py`：既没有发现"已文档化但未接线
的缺口"（那种情况下会有一句类似"`reap_orphan_runs()` 尚未接入，见任务 14.6"
的说明，实际没有），也没有可供插入调用的函数体（补一整个 `run_task()` 主循环
是任务 14.5 的范围，不是本任务 14.6 的范围，14.6 的文件清单只有
`controller/recovery.py`）。补齐 `run_task()` 主循环、并在其第一行调用
`reap_orphan_runs(store)`，留给任务 14.5 的后续完善工作。

## `reap_orphan_runs()`：单条 UPDATE 语句替代伪代码的逐行 FOR 循环

design.md §8.8 的伪代码先 `SELECT run_id ... WHERE status='running'`，再对每
一行单独执行一次 `UPDATE ... WHERE run_id = run_id`。本实现用一条
`UPDATE runs SET ... WHERE status='running'` 替代这个逐行循环——两者在同一
`BEGIN IMMEDIATE` 事务内产生完全相同的最终数据库状态（把全部 `status=
'running'` 行原子地转为同样的终态六列取值），`cursor.rowcount` 给出的受
影响行数与伪代码 `len(rows)` 是同一个数字。SQLite 的批量 `UPDATE ... WHERE`
本身就是单一原子语句，逐行循环并不比单条语句更"正确"，只是伪代码惯常的
表达方式；本实现选择更简单的单语句形式，行为完全等价，且不需要在 Python
侧维护一个中间的 `run_id` 列表。

`budget_units` 列不在 `SET` 子句中，因此保持原值不变（"预算宁可多计不可少
计"，design.md §8.8「budget_units 保留」）。`WHERE status='running'` 保证
`done`/`failed` 行不被本语句触及（它们的 `status` 不等于 `'running'`，不满足
`WHERE` 条件）。

不作心跳判活：本函数不读取、不比较任何时间戳或进程存活标记，`status=
'running'` 本身就是判定依据——单进程串行下进程启动即意味着上一进程已终止，
残留的 `running` 行永远是孤儿（design.md §6.2.5/§8.8、requirements.md
R16.12）。因此本函数**无条件执行**：调用方（未来的 `run_task()`）不应该、
也不需要为它加任何 `if resume:` 之类的门控——这正是任务描述与 `design.md`
L16-4 裁定要求移除的既有 `resume` 门控。

时间戳格式与 `store/repo.py::_now_iso()` 完全一致（UTC ISO 8601 秒级，
`"%Y-%m-%dT%H:%M:%SZ"`）——本文件不 import 该函数（名称加下划线前缀，是
`store.repo` 模块内部实现细节，未出现在其 `__all__` 导出列表中），而是在本
文件内本地复刻同一格式的实现，保持两处产出的时间戳字符串逐字符可比较、可
落入同一个 `ended_at` 列而不产生格式漂移。

## `rebuild_state()`：`RebuiltState`——design.md §6.6 `SearchState` 七字段中
可从 SQLite（+ 两个已落地的下游模块）单独重建的子集，非完整 `SearchState`

design.md §6.6 的字面签名是 `rebuild_state(store: Store, task_id: str) ->
SearchState`。`SearchState` 本身是 `agent/propose.py`（任务 23.1，M4 里程碑）
的契约类型，尚未落地；`tasks.md`「优先序判断标准」一节明确「M4 之前不写
`agent/`」——任务 14.6（M1 里程碑）不能依赖一个还不存在的数据类的确切形状。
这与 `controller/stop.py` 处理同一个 `SearchState` 契约缺口时给出的理由完全
一致（该文件顶部"`SearchState` / `BestRecord`：本文件的最小化本地占位定义"
一节）。

`SearchState` 的七个字段逐一盘点其可重建性：

| 字段 | 来源 | 本函数是否重建 | 理由 |
| --- | --- | --- | --- |
| `current_best` | SQLite（经 `eval.aggregate.rank()`，任务 12.3，已落地） | **是** | 哪个候选当前最优完全落在数据库事实表里，`rank()` 已实现 §5.4 worst-case 聚合 + 三段定序排序，本函数只需调用并取第一名 |
| `remaining_budget` | SQLite（经 `controller.budget.BudgetLedger`，任务 9.2，已落地） | **是** | `BudgetLedger.remaining()` 自身即是"完全从 SQLite 重建"的既定实现（该类 docstring："不在 `__init__` 读数据库、不缓存……因此崩溃重启后无需任何恢复步骤即可继续使用"） |
| `legal_domain` | `constraints.yaml`（`design_space.variables`），不在数据库中 | 否 | 这是配置投影，不是"隐藏业务状态"——R16.13 的用词是"完全从 SQLite 重建"，意在避免 `run_task()` 在内存里累积一份不可恢复的进度状态；配置文件每次进程启动都会被重新加载，不存在"崩溃后丢失"的风险，不属于本函数要解决的问题。调用方应直接从已加载的 `constraints_cfg.design_space.variables` 取值，合并进最终的 `SearchState`，不经过本函数 |
| `hard_constraints` | `constraints.yaml`（`hard_constraints.*.value`），不在数据库中 | 否 | 同上 |
| `tested_candidates` | 设计要求"已压缩为特征，无采样点"，压缩函数是 `agent.prompt.compress_waveform()`（任务 23.2，尚未落地） | 否 | 即便本函数查出 `candidates` 表的全部 `candidate_id`（`store/repo.py` 当前也未提供这样一个只读方法，本任务不新增），也无法产出设计要求的"已压缩特征"形状——压缩算法（超调/下冲/恢复时间/纹波幅值/振荡频次/发散标志六元组）是任务 23.2 的职责，本函数不代为实现一份简化版压缩逻辑（那会产生两份不一致的"压缩"定义） |
| `failed_regions` | 字段形状要求"参数边界 + failure_type + 样本数 + run_id 列表"，`bounds` 是把多个失败点聚类为区域的结果 | 否 | 「哪些失败点该归为同一区域、边界怎么划」是聚类判断，design.md 全文只给出 `FailedRegion` 的字段形状、未给出聚类算法本身；这属于 `agent/propose.py`（任务 23.1）构造 prompt 上下文时的职责，不是可以从 SQLite 直接投影出的确定性查询 |
| `evidence` | `EvidenceTriple`（`retrieval/ingest.py`，任务 25.1，M4 里程碑，尚未落地） | 否 | 模块不存在，无从构造；design.md §8.1 主循环里 `state.evidence ← retrieve(state)` 本身是在 `rebuild_state()` 返回之后单独赋值的一步（伪代码两行分开写），不是 `rebuild_state()` 自身职责 |

因此本函数返回一个刻意收窄的本地类型 `RebuiltState`（只含 `current_best` /
`remaining_budget` 两个字段），不是字面意义上的 `SearchState`。任务描述给出
的两个选项（"返回部分 dataclass 且标注占位字段" / "定义更窄的临时返回类型"）
中，本文件选择第二个：不在返回类型里放 `tested_candidates=()` /
`failed_regions=()` / `evidence=None` 这类总是取固定值、没有任何调用方会
读取的占位字段（那是死重量，违反"最小代码"），而是直接不定义这些字段，把
"目前只能重建这两项"这一事实体现在类型形状本身上。待 `agent/propose.py`
（任务 23.1）落地真正的 7 字段 `SearchState` 后，`run_task()` 的接线者应把
`rebuild_state()` 的返回值、`constraints_cfg` 投影的 `legal_domain`/
`hard_constraints`、以及任务 23.1/23.2/25.1 各自补齐的其余三项四部分合并，
构造真正的 `SearchState` 实例；本函数不需要因此改动签名或实现——它已经
完整地做完了它能做的那部分。

## `rebuild_state()` 签名对 design.md 字面签名的必要扩展（kwonly 新参数）

design.md 字面签名 `rebuild_state(store, task_id)` 无法计算 `current_best`
（需要 `metrics_cfg.objective.primary` 判定主目标与聚合方式，`eval.aggregate.
rank()` 的必填参数）与 `remaining_budget`（需要 `Budget.max_engine_starts`/
`max_wallclock_hours` 构造 `BudgetLedger`）——这与本代码库对 `sim.simulate()`
的 `frozen_fingerprint`、`controller.stop.should_stop()` 的 `budget_exhausted`
等既有必要扩展是同一类偏离（签名给定、真正所需输入未给定，用显式 kwonly
参数补齐，不做隐式全局读取或反向推断）。本函数新增两个必填 kwonly 参数：

- `metrics_cfg: MetricsConfig`：转发给 `eval.aggregate.rank()`。
- `budget: Budget`：即 `task_cfg.budget`（`config.schema.Budget`），提供
  `max_engine_starts` 与 `max_wallclock_hours` 构造 `BudgetLedger`。
  `BudgetLedger.__init__` 还接受 `max_wallclock_s`（当前未被 `used()`/
  `remaining()`/`exhausted()` 任何方法读取，见 `controller/budget.py`），本
  函数按其字面单位换算传入（`max_wallclock_hours × 3600`），不额外发明语义。

## 与 `controller.stop.SearchState` 的关系：两个独立的、各自最小化的本地
占位定义，不互相依赖

`controller/stop.py` 已经为同一个 `design.md` §6.6 `SearchState` 契约定义了
一个本地占位版本（只含 `current_best`，供 `should_stop()` 读取）。本文件
**不**导入或复用那整个类，也不给它加字段——`RebuiltState` 需要的字段集合
（`current_best` + `remaining_budget`）与 `controller.stop.SearchState` 的
字段集合（只有 `current_best`）不同，把两者合并成一个共享类需要往 `stop.py`
这个已经落地、已过流程的文件里加字段，属于对另一个已完成任务的返工，不在
本任务（14.6，文件清单只有 `controller/recovery.py`）范围内。两个模块各自
只定义自己实际读取的字段子集，是"最小化本地占位"这一既定模式在两处的独立
应用，不是重复劳动——它们本来就是同一个未来 `SearchState` 的两个不同投影，
各自模块只关心自己需要的那一部分。

`current_best` 字段的类型 `BestRecord`（`candidate_id` + `value`）已经在
`controller/stop.py` 定义。本文件直接 `from poweragent.controller.stop
import BestRecord` 复用该类型，不重新定义一份字段相同的副本——这是纯粹的
类型复用（import），不涉及修改 `stop.py` 的任何代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from poweragent.config.schema import Budget, MetricsConfig
from poweragent.controller.budget import BudgetLedger
from poweragent.controller.stop import BestRecord
from poweragent.eval.aggregate import rank
from poweragent.store.repo import Store

__all__ = ["RebuiltState", "reap_orphan_runs", "rebuild_state"]


def _now_iso() -> str:
    """UTC ISO 8601 秒级时间戳，格式与 `store/repo.py::_now_iso()` 逐字符
    相同（`"%Y-%m-%dT%H:%M:%SZ"`）；本文件不 import 该私有函数（下划线前缀，
    未出现在 `store.repo.__all__` 中），而是本地复刻同一格式，保持两处产出
    的时间戳字符串可直接比较、可落入同一列而不产生格式漂移。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reap_orphan_runs(store: Store) -> int:
    """design.md §6.2.5 / §8.8；requirements.md R16.12；属性 CP-7。

    每次进程启动都无条件执行（不由 `resume` 参数门控）：在一个 `BEGIN
    IMMEDIATE` 事务内把全部 `status='running'` 的行置为 `status='failed'`、
    `failure_class='transient_error'`、`cause='process_restart'`、非空
    `ended_at`，保留其 `budget_units`，不修改任何已为 `done` 或 `failed` 的
    行，返回受影响的行数（等价于 design.md §8.8 伪代码的 `len(rows)`）。不作
    心跳判活。实现细节（单条 `UPDATE` 语句替代伪代码逐行 `FOR` 循环）见模块
    docstring 对应小节。
    """
    with store.tx() as conn:
        cur = conn.execute(
            "UPDATE runs SET status='failed', failure_class='transient_error', "
            "cause='process_restart', ended_at=? WHERE status='running'",
            (_now_iso(),),
        )
        count = cur.rowcount
    return count


@dataclass(frozen=True, slots=True)
class RebuiltState:
    """`rebuild_state()` 的返回类型——design.md §6.6 `SearchState` 七字段中
    本函数能从 SQLite（+ 两个已落地的下游模块）单独重建的子集。见模块
    docstring "`rebuild_state()`" 一节的逐字段盘点表；该表列出的另外五个
    字段（`legal_domain` `hard_constraints` `tested_candidates`
    `failed_regions` `evidence`）不在本类型中，理由见同一节。
    """

    current_best: BestRecord | None
    remaining_budget: int


def rebuild_state(
    store: Store,
    task_id: str,
    *,
    metrics_cfg: MetricsConfig,
    budget: Budget,
) -> RebuiltState:
    """design.md §6.6 / §8.1；requirements.md R16.13；属性 CP-7。

    完全从 SQLite（经 `eval.aggregate.rank()` 与 `controller.budget.
    BudgetLedger`，均为已落地模块）重建 `RebuiltState`，不持有任何不可恢复
    的隐藏业务状态。

    `current_best`：调用 `eval.aggregate.rank(store, task_id, metrics_cfg,
    top_n=1)`，命中即取第一名转为 `BestRecord`；`rank()` 返回空列表（无候选
    满足 Evaluation 集完备条件）时取 `None`。

    `remaining_budget`：构造一次性的 `BudgetLedger`（`max_wallclock_s` 按
    `budget.max_wallclock_hours × 3600` 换算）并调用其 `remaining()`——该方法
    自身已是"完全从 SQLite 重建"的既定实现（见 `controller/budget.py`）。

    未覆盖的另外五个 `SearchState` 字段及理由见模块 docstring 与
    `RebuiltState` 类文档。签名相对 design.md 字面签名的必要扩展
    （`metrics_cfg` / `budget` 两个 kwonly 参数）见模块 docstring 对应小节。
    """
    ranked = rank(store, task_id, metrics_cfg, top_n=1)
    current_best = (
        BestRecord(candidate_id=ranked[0].candidate_id, value=ranked[0].worst_case_value)
        if ranked
        else None
    )

    ledger = BudgetLedger(
        store, task_id, budget.max_engine_starts, budget.max_wallclock_hours * 3600.0
    )
    remaining_budget = ledger.remaining()

    return RebuiltState(current_best=current_best, remaining_budget=remaining_budget)
