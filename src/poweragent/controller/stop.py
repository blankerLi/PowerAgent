"""poweragent/controller/stop.py

`should_stop()`（design.md §6.2.3 / §8.7；tasks.md 任务 14.3；需求追溯
R16.6, R16.7, R16.8, R16.9, R16.10, R16.15）。

## `MARGIN_FAILURE_ESCALATION_THRESHOLD` 的落点：重导出，而非移入

`tasks.md` 任务 14.3 的描述写道：「模块常量 `MARGIN_FAILURE_ESCALATION_
THRESHOLD = 3` 落在本文件……不落配置项」。但该常量的**canonical 定义**已在
任务 11.3（`eval/margin.py`）落地，且该模块的 docstring 专门留了一节解释
暂放原因，并明确给出两个后续选项：「该模块应当从这里 `import` 这个常量……
或者把它移过去并在这里留一个向后兼容的重导出」。

本文件选择**第一个选项**（`from poweragent.eval.margin import
MARGIN_FAILURE_ESCALATION_THRESHOLD` 并在 `__all__` 重导出），而不是把字面量
`= 3` 移到本文件、反过来在 `eval/margin.py` 留重导出。理由：

1. **依赖方向已有既定先例**：`controller/preflight.py`（任务 10.5，已落地）
   已经是 `from poweragent.eval.metrics import
   IMPLEMENTED_TIME_DOMAIN_METRICS`——`controller` 依赖 `eval`，不是反过来。
   把这个常量的字面量移到 `controller/stop.py`、再让 `eval/margin.py`
   `import` 回来，会让 `eval` 这一层反过来依赖 `controller`，与已经建立的
   分层方向相反，且没有任何功能收益（值本身不变，只是换了个文件存放）。
2. **`eval/margin.py` 已经为这个常量写了完整的设计说明**（暂放原因、为什么
   是「个别候选失败」与「提取链路坏了」的分界、为什么不落配置项）。把字面量
   移过来意味着要把那段说明搬到本文件、或者留一个空壳引用去解释「详见
   `eval/margin.py`」，两者都不比「直接从原处 import」更清晰。
3. **本文件对这个常量没有额外的语义要求**——`should_stop()` 本身不使用它
   （它是 `run_task()`/`eval.margin.extract_margin()` 熔断计数器达到阈值后
   才会构造出 `pending_stop_and_ask_human=('stop_and_ask_human',
   'metric_pipeline_error')` 传进来，`should_stop()` 只消费这个已经判定好的
   结果，不自己比较计数器与阈值）。它出现在本文件只是为了满足任务描述里
   「该常量应可从 `controller/stop.py` 导入」这一可见性要求，重导出已经
   足够。

任务描述里「落在本文件」与 `eval/margin.py` docstring 里「或者把它移过去」
两种表述，都是该常量所有者尚未确定最终落点时留下的两个开放选项——本文件
落地时选定第一个选项，不产生第二份独立定义（`eval/margin.py` 的字面量
`= 3` 保持唯一）。

## `should_stop()` 签名对 design.md §6.2.3 的必要扩展

design.md §6.2.3 给出的签名是：

```python
def should_stop(state: SearchState, task_cfg: TaskConfig,
                agent_stop_recommendation: bool) -> StopDecision: ...
```

但 §8.7 的伪代码在函数体内引用了三样 `SearchState`（§6.6，7 字段：
`legal_domain` / `hard_constraints` / `current_best` / `tested_candidates` /
`failed_regions` / `remaining_budget` / `evidence`）里**不存在**的东西：
`ledger.exhausted()`（一个 `BudgetLedger` 方法调用）、`no_improve`（一个
跨轮次的计数器）、`pending_stop_and_ask_human()` / `pending_cause()`（某个
「待处理熔断条件」的查询）。`SearchState` 的真正构造者是 `agent/propose.py`
（任务 23.1，M4 里程碑，`tasks.md` 明确「M4 之前不写 `agent/`」），而本任务
（14.3）排在 M1 里程碑、远早于 23.1——`should_stop()` 不能依赖一个尚未存在
的数据类的确切形状。

这与任务 7.2（`sim/simulate.py`）对 `frozen_fingerprint` 的处理、任务 10.1
对 `io_contract.vout_target_v` 的处理是同一类必要偏离：**用显式的 kwonly
参数补上 `SearchState` 缺的字段，而不是猜测或抢先扩写 `SearchState` 本身**。
本文件对 §6.2.3 签名的扩展：

```python
def should_stop(state: SearchState, task_cfg: TaskConfig,
                 agent_stop_recommendation: bool, *,
                 budget_exhausted: bool,
                 no_improvement_rounds: int,
                 pending_stop_and_ask_human: tuple[str, str] | None = None,
                 ) -> StopDecision: ...
```

- `budget_exhausted: bool`——调用方（`run_task()`，任务 14.4/14.5，尚未落地）
  自己持有 `BudgetLedger` 实例并调用 `ledger.exhausted()`；本函数不接触
  `BudgetLedger`、不做预算查询，只消费调用方已经算好的布尔结果。
- `no_improvement_rounds: int`——`run_task()` 主循环维护的「连续无改善轮数」
  计数器；本函数只做数值比较，不维护、不递增、不复位这个计数器。
- `pending_stop_and_ask_human: tuple[str, str] | None`——调用方持有的「是否
  存在待处理的 `stop_and_ask_human` 条件」；非 `None` 时为 `(reason, cause)`
  二元组，其中 `reason` 恒为字面量 `'stop_and_ask_human'`（与 §8.7 伪代码
  `Stop('stop_and_ask_human', pending_cause())` 逐字对应，元组里带上这个恒定
  字面量只是让调用方在构造这个「待处理条件」时不用另外记一份「这是哪种
  stop」的旁路状态），`cause` 为具体原因（例如
  `'model_mutated_during_optimize'`、`'metric_pipeline_error'`、
  `'margin_extraction_unreliable'`）。默认 `None` 表示当前没有待处理条件。

`state: SearchState` 参数保留在签名中，未被上述扩展取代——本函数确实会读
`state.current_best`（`objective_target` 与 `stop_on_first_feasible` 两条
判定都要用到）。

## `SearchState` / `BestRecord`：本文件的最小化本地占位定义

`design.md` §6.6 给出的 `SearchState` 是 `agent/propose.py`（任务 23.1）的
契约类型，尚未落地；`state.current_best` 引用的 `BestRecord` 类型在
`design.md` 全文未给出字段定义（与 `eval/margin.py` 的 `MarginPoint`、
`eval/metrics.py` 的 `Waveform`、`sim/simulate.py` 的 `ModelInspection` 同一
处境：签名给定、字段形状未给定）。

本文件在此**只**定义 `should_stop()` 自身读取的这一个字段
（`current_best: BestRecord | None`），不提前补全 `legal_domain` /
`hard_constraints` / `tested_candidates` / `failed_regions` /
`remaining_budget` / `evidence` 六个字段——这六个字段没有任何代码路径在本
文件内被读取，补全它们等于替任务 23.1 做一次它本该自己做的字段设计决策
（与 `eval/margin.py` docstring 里「提前创建这个文件……等于替任务 14.3 做了
一个它本该自己做的结构决策」是同一类风险，此处反过来适用于 `SearchState`
与任务 23.1 的关系）。任务 23.1 落地 `agent/propose.py` 的真正 `SearchState`
（7 字段）后，本文件应改为 `from poweragent.agent.propose import
SearchState` 并删除下方本地定义——`should_stop()` 只用得到 `current_best`
这一个字段，真正的 7 字段 `SearchState` 实例传入本函数时天然满足属性访问，
不需要改动 `should_stop()` 的函数体。

`BestRecord` 字段按「当前最优候选」这一用途最小设计：`candidate_id`（哪个
候选）+ `value`（该候选在 `objective.primary` 上的聚合值，即
`design.md` §8.7 伪代码 `state.current_best.value` 与
`task_cfg.objective_target.target_value` 相比较的量）。
"""

from __future__ import annotations

from dataclasses import dataclass

from poweragent.config.schema import TaskConfig
from poweragent.eval.margin import MARGIN_FAILURE_ESCALATION_THRESHOLD

__all__ = [
    "MARGIN_FAILURE_ESCALATION_THRESHOLD",
    "BestRecord",
    "SearchState",
    "StopDecision",
    "should_stop",
]


# ===========================================================================
# BestRecord / SearchState：最小化本地占位定义，见模块 docstring 相应小节。
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BestRecord:
    """当前最优候选的最小摘要（`design.md` §6.6 引用但未给出字段定义，本文件
    按 `should_stop()` 实际用途自行设计并在此登记）。"""

    candidate_id: str
    value: float


@dataclass(frozen=True, slots=True)
class SearchState:
    """`design.md` §6.6 `SearchState`（agent.propose 模块，任务 23.1，尚未
    落地）的最小化本地占位：只含 `should_stop()` 实际读取的
    `current_best` 一个字段，其余六个字段（`legal_domain` /
    `hard_constraints` / `tested_candidates` / `failed_regions` /
    `remaining_budget` / `evidence`）刻意不在此补全，理由见模块 docstring。
    """

    current_best: BestRecord | None


# ===========================================================================
# StopDecision（design.md §6.2.3，逐字段照抄）
# ===========================================================================


@dataclass(frozen=True, slots=True)
class StopDecision:
    stop: bool
    reason: str | None
    cause: str | None


# ===========================================================================
# should_stop()：design.md §8.7 的固定优先顺序，逐条实现
# ===========================================================================


def should_stop(
    state: SearchState,
    task_cfg: TaskConfig,
    agent_stop_recommendation: bool,
    *,
    budget_exhausted: bool,
    no_improvement_rounds: int,
    pending_stop_and_ask_human: tuple[str, str] | None = None,
) -> StopDecision:
    """按 `design.md` §8.7 固定优先顺序判定是否停止（`tasks.md` 任务 14.3）。

    优先顺序（先命中先返回，不存在任何两条同时命中时的次序歧义）：

    1. `budget_exhausted` ⟹ `('budget_exhausted', None)`
    2. `pending_stop_and_ask_human` 非 `None` ⟹ `('stop_and_ask_human', cause)`
    3. `task_cfg.objective_target` 存在且 `state.current_best` 存在且
       `state.current_best.value <= task_cfg.objective_target.target_value`
       ⟹ `('target_reached', None)`——严格 `<=`，**不套用 `tie_tolerance`**
       （`tie_tolerance` 描述测量分辨力，`objective_target` 是使用者写下的
       显式达标线，套容差会让「达标」本身变模糊；需要留余量时由使用者自行
       调低 `target_value`，不是本函数的职责）
    4. `task_cfg.stop.stop_on_first_feasible` 为真且 `state.current_best`
       存在 ⟹ `('target_reached', 'first_feasible')`——`stop_on_first_
       feasible` 默认 `false`（`config.schema.StopConfig`），默认情形下
       出现首个可行候选时不停，继续寻优
    5. `no_improvement_rounds >= task_cfg.stop.no_improvement_rounds`
       ⟹ `('no_improvement', None)`
    6. `agent_stop_recommendation` 为真且 `no_improvement_rounds >= 1`
       ⟹ `('no_improvement', 'agent_recommended')`——该建议为真而
       `no_improvement_rounds == 0` 时本条不命中，落到下一条（继续）
    7. 均不成立 ⟹ `(None, None)`（继续寻优）

    `reason` 字段全函数只产出四个值：`budget_exhausted` / `stop_and_ask_
    human` / `target_reached` / `no_improvement`，不新增第五个取值。
    """

    if budget_exhausted:
        return StopDecision(stop=True, reason="budget_exhausted", cause=None)

    if pending_stop_and_ask_human is not None:
        reason, cause = pending_stop_and_ask_human
        return StopDecision(stop=True, reason=reason, cause=cause)

    current_best = state.current_best
    objective_target = task_cfg.objective_target
    if (
        objective_target is not None
        and current_best is not None
        and current_best.value <= objective_target.target_value
    ):
        return StopDecision(stop=True, reason="target_reached", cause=None)

    if task_cfg.stop.stop_on_first_feasible and current_best is not None:
        return StopDecision(stop=True, reason="target_reached", cause="first_feasible")

    if no_improvement_rounds >= task_cfg.stop.no_improvement_rounds:
        return StopDecision(stop=True, reason="no_improvement", cause=None)

    if agent_stop_recommendation and no_improvement_rounds >= 1:
        return StopDecision(
            stop=True, reason="no_improvement", cause="agent_recommended"
        )

    return StopDecision(stop=False, reason=None, cause=None)
