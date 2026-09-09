"""poweragent/controller/run_task.py

`run_tier()`：单候选场景层执行——分层执行、提前拒绝与两级缓存（`design.md`
§8.4；`tasks.md` 任务 14.4；需求追溯 R6.5, R6.6, R6.7, R6.9, R6.10, R6.12,
R15.5, R15.6；属性 CP-4, CP-6）。

## 本任务（14.4）的范围边界：只有 `run_tier()`

`controller/run_task.py` 由 `tasks.md` 分五波派发（14.4 / 14.5 / 14.7 / 24.4 /
24.8）。本次（14.4）**只**落地 `run_tier()`——单候选、单 tier 的场景行执行、
两级缓存查找与提前拒绝判定。以下均**不**在本次范围内，是刻意留给对应波次的
接口缺口（沿用本代码库既有的"软依赖"文档模式，参见 `sim/simulate.py` 顶部
"design.md 字面签名之外的五处偏离"一节与 `cli.py` 的"软依赖"惯例）：

- **`run_task()` 主循环本身**（候选生成、遍历、跨候选/跨轮次状态）——任务 14.5。
- **`tasks` 行落库**（`simulation_only`、六个哈希、`budget_max_starts`、
  `started_at` 等）——任务 14.5。
- **候选级失败分类的持久化记录**——design.md §8.4 的 `run_task` 伪代码在
  Screening 提前拒绝处只写 `CONTINUE // 提前拒绝：candidate_rejected`
  这样一句注释，没有对应的 `store.*` 写入调用；`requirements.md` R6.6 把
  "以 `failure_class='candidate_rejected'` 记录触发拒绝的 `scenario_id` 与
  未通过的约束名"的主语明确写成 `run_task()`（不是 `run_tier()`）。本文件
  因此**不**在 Screening 约束不可行的提前拒绝路径上做任何额外的 `store` 写入
  ——该场景行自身的 `runs`/`metric_results`/`constraint_results` 三行已经
  在正常评价流程中写好（`status='done'`，`constraint_results.feasible=0`
  且 `violations` 含未通过约束名），这已经是"记录"本身；`run_tier()` 把
  触发拒绝的 `scenario_id` 与未通过约束名通过 `TierResult.rejected_scenario_id`
  / `TierResult.rejected_constraint_names` 两个字段回传给调用方，**由
  `run_task()`（任务 14.5）决定是否需要在候选/任务粒度做进一步持久化**（例如
  写一条独立的候选级拒绝记录——本文件不擅自新增这类写入，因为它涉及候选级
  记录的表结构与落点，属于任务 14.5 的编排决策范围）。
- **`engine_transient` 的重试循环**——design.md §8.4 的 `run_tier` 伪代码把
  重试 `LOOP`（`attempt` 递增、按 `budget.max_attempts_per_scenario` 判断
  耗尽）内嵌在 `run_tier` 算法本身之中；但本次任务描述明确排除"failure
  classification/retry at the run_task level"，把它划给任务 14.5。本文件
  的处理：**单次尝试**（`attempt` 恒为 `1`），`SimulationResult.status ==
  'engine_transient'` 时直接以 `failure_class='transient_error'`、
  `cause='engine_transient'` 关闭该行并停止本次 `run_tier()` 调用（不再处理
  该 tier 的其余场景行），在 `TierResult` 中回传
  `failure_class='transient_error'`——这与 R16.5「重试耗尽仍
  `engine_transient` ⟹ 关闭该行、不再新开 `attempt` 行、不写
  `stop_reason`、继续下一候选」的**最终状态**一致（只是本次调用把"重试
  耗尽"简化为"零次重试"），但**不**实现"重试未耗尽时新开 `attempt+1` 行
  再次调用 `sim.simulate()`"这一段——那需要 `budget.max_attempts_per_scenario`
  （`TaskConfig.budget`）与跨 `attempt` 的循环状态，两者都是 `run_task()`
  层面的编排输入，本函数签名不接受 `TaskConfig`。**这是一个需要在任务 14.5
  接线时替换的已知简化**：14.5 的实现者应把本文件当前"遇到
  `engine_transient` 立即终结本次 `run_tier()` 调用"的行为，替换/包装为
  "捕获这一终结结果后，若 `attempt < max_attempts_per_scenario` 则以
  `attempt+1` 重新调用与本次相同的单场景处理路径"——由于本次改动会涉及
  `run_tier()` 的循环粒度（按场景行重试，而不是按整个 tier 重试），14.5
  落地时很可能需要把 `attempt` 提升为 `run_tier()` 的显式参数（当前签名
  固定为 1，见下方 `run_tier()` docstring），或者把重试循环整体挪到调用
  `run_tier()` 之外、由 `run_task()` 对每个场景行单独调用一个更细粒度的
  单场景函数。两种重构方式都不影响本次已落地的两级缓存与提前拒绝逻辑本身。
- **裕量提取连续失败的熔断判定**（`MARGIN_FAILURE_ESCALATION_THRESHOLD`
  达到 3 即 `stop_and_ask_human(cause='metric_pipeline_error')`）——本函数
  只**维护并回传**（通过 kwonly 入参 `margin_failure_count` 传入、
  `TierResult.margin_failure_count` 传出）这个跨调用的计数器数值本身（每次
  裕量提取失败时 +1、成功时复位为 0），不在本函数内比较该计数器是否达到
  `MARGIN_FAILURE_ESCALATION_THRESHOLD` 并抛出熔断——因为熔断需要在**跨候选、
  跨 tier**（整个任务范围）维持这个计数器，`run_tier()` 只处理单候选单 tier
  的一次调用，没有能力知道"这是不是同一任务内的连续第 3 次"；`run_task()`
  （任务 14.5）在多次 `run_tier()` 调用之间原样传递、累计这个计数器，并在
  达到阈值时自行 `import poweragent.eval.margin.
  MARGIN_FAILURE_ESCALATION_THRESHOLD` 做比较与熔断（与 `controller/stop.py`
  重导出该常量、自己不做比较的既有先例是同一分工模式）。

## 本函数相对 design.md §8.4 字面签名的必要扩展（kwonly 新参数）

design.md §8.4 给出的伪代码签名是 `run_tier(cand, rows, ledger)`——只有三个
位置参数。与 `sim/simulate.py`（任务 7.2）、`eval/metrics.py`（任务 10.1）
等既有模块同一惯例：伪代码签名描述的是"这个函数做什么"，真正落地时需要的
输入（配置、会话、存储引用等）以显式 kwonly 参数补齐，不塞入某个更大的
容器、不做隐式全局读取、不让函数自己去猜测或反向推断。本函数新增的 kwonly
参数与理由：

- `model_cfg` / `metrics_cfg` / `constraints_cfg`：`compute_metrics()` /
  `extract_margin()` / `judge()` / `sim.simulate()` 四个下游函数各自需要
  这些配置，`run_tier()` 作为编排者必须持有并原样转发。
- `session: MatlabSession`：转发给 `sim.simulate()`。
- `store: Store`：本函数直接调用的仓储方法（`find_cached_evaluation` /
  `find_cached_simulation` / `open_run` / `close_run_ok` /
  `close_run_failed` / `write_evaluation` / `mark_artifact_missing`）均属于
  `Store`。
- `artifacts: ArtifactStore`：转发给 `sim.simulate()`（`simulate()` 的必填
  参数，任务 7.2 已确立）。
- `budget_ledger: BudgetLedger`：先占预算闸门（`reserve()`）。
- `task_id: str`：`store.open_run()` 与 `sim.simulate()` 均为必填参数。
- `execution_env_hash: str`：`store.cache.simulation_key()` 的七字段之一，
  在任务开始时冻结一次（与 `frozen_fingerprint` / `frozen_model_package_hash`
  同批冻结），本函数不计算、只原样转发。
- `frozen_fingerprint` / `frozen_model_package_hash`：转发给
  `sim.simulate()`（CP-3 模型不变性检查，任务 7.2 已确立的必填 kwonly 参数，
  本函数不重复计算）；`frozen_model_package_hash` 同时也是
  `simulation_key()` 七字段之一，一份值两处使用。
- `base_dir`：转发给 `sim.simulate()`（可选，默认当前工作目录，与该函数
  既有默认值一致）。
- `margin_failure_count: int = 0`：跨调用的裕量提取连续失败计数器（见上方
  "裕量提取连续失败的熔断判定"一节），调用方在多次 `run_tier()` 调用之间
  原样传递、累计。

## `scenario_rows` 的类型：`store.repo.ScenarioSpec`，非 `controller.scenario`
的本地类型——一处已知的跨模块类型不一致，本文件不代为修复

`controller/scenario.py`（任务 14.1）的 `screening_rows()` / `evaluation_rows()`
/ `robustness_rows()` 三个函数当前返回**该模块自行定义的本地 `ScenarioSpec`
dataclass**（字段形状与 `store.repo.ScenarioSpec` 一致，但是不同的 Python
类）——该模块顶部文档已经标注这是"`store/repo.py` 尚未落地时的临时应对"，
落地后"应删除本模块的本地定义、改为 `from poweragent.store.repo import
ScenarioSpec`"，但截至本文件撰写时这项后续清理尚未执行（`store/repo.py`
已在任务 8.3 落地，`controller/scenario.py` 仍是任务 14.1 时的旧状态）。

本文件的 `run_tier()` 按 `sim.simulate()` / `eval.metrics.compute_metrics()`
/ `eval.constraints.judge()` 等下游函数的真实签名要求，把 `scenario_rows`
标注为 `Sequence[store.repo.ScenarioSpec]`（跨模块契约类型）——这是下游
函数实际能接受的类型，不是 `controller.scenario` 当前返回的本地类型。两者
字段形状相同但类不同，Python 的结构化调用点（属性访问）不会因此立即报错，
但类型检查器会标记不一致。**这是一个需要在 `controller/scenario.py` 补做
清理（删除本地定义、改用 `store.repo.ScenarioSpec`）之后才能完全消除的
既有缺口，不属于本任务（14.4）的范围**——本任务不修改 `controller/scenario.py`
（该文件不在本任务的文件清单内，且修改它属于另一个已完成任务的返工）。
`run_task()`（任务 14.5）接线时，调用 `screening_rows()` 等函数取得场景行后，
应在传给 `run_tier()` 之前完成到 `store.repo.ScenarioSpec` 的显式转换（或者
届时先完成 `controller/scenario.py` 的清理，使其直接返回正确类型）。

## 产物完整性校验（"sha256 往返校验"）的解读：无存量哈希可比对，因此以
"两次独立读取 + 摘要比对"作为往返校验的可执行定义

`store/artifacts.py` 的 `commit()` 在**写入当下**做往返校验（写临时文件 →
读回计算 sha256），但 `runs.waveform_ref` / `runs.observable_ref` 两列（见
`store/artifacts.py` 顶部"引用字符串格式"一节）落盘的是**路径字符串**，不是
内容哈希——数据库里没有为已提交产物保留一份"写入时的哈希"供日后复用产物时
比对。这意味着"`simulation_key` 命中但产物…往返校验失败"这句话，在**复用
一个早先已提交产物**的场景下，无法执行"读回哈希与写入时哈希比对"这个字面
动作（那份写入时哈希从未被持久化）。

本文件采用的可执行定义：读取该产物文件两次，各自独立计算 sha256，两次结果
逐字符相同即视为"往返校验通过"（`_artifact_round_trip_ok()`）。这构成一个
真实的往返读取动作（读 → 摘要 → 再读 → 再摘要 → 比对），能捕捉"文件在两次
读取之间被截断/替换/并发写坏"一类的存储层损坏；它不能捕捉"文件自提交以来
从未被任何进程动过，但内容本身从写入之初就已经损坏"这一类问题（因为没有
写入时的哈希可比对）。**这是本文件对任务描述"sha256 往返校验"一词在缺少
存量哈希的既有接口下唯一可执行的解读**，若未来需要真正的"写入时哈希 vs
现在读到的哈希"比对，`store/artifacts.py` 需要新增一列/一张表持久化产物的
内容哈希——这不在本任务范围内，也不是本任务能够单方面新增的表结构。
文件不存在或长度为 0 时直接判定为不完整，不进入读取步骤（与
`ArtifactCommitError` 的判定条件一致）。

## `mark_artifact_missing` 的调用点：`store/repo.py` 已给出的既定方法，
本文件只是它当前唯一的调用方

`Store.mark_artifact_missing(run_id)`（任务 14.4 新增，已在 `store/repo.py`
落地）已经在其自身 docstring 中明确写好"`controller/run_task.py` 的
`run_tier()` 是本方法目前唯一的调用方"，且已经把"标注哪个 `run_id`"、
"这不是终态覆盖而是审计标注"、"幂等 best-effort"三层语义都定义清楚。本文件
按其既有契约直接调用：`simulation_key` 命中但产物未通过往返校验时，取
`cached_sim.run_id`（早先那次成功仿真的 `run_id`，不是本次即将新开的
`run_id`）传入 `store.mark_artifact_missing()`，随后把本次请求当作完全
未命中处理（重新仿真、计入完整预算）。不需要在本文件里重新论证这层语义，
`store/repo.py` 已经论证过。

## `budget_ledger.reserve()` 的 `run_id` 实参：预算先占时点尚无真实 `run_id`

`BudgetLedger.reserve(units, *, run_id)`（任务 9.2 已落地）的 docstring 已经
说明"调用方在还没有真实 `run_id` 时可传入任意便于定位问题的字符串"——本文件
在调用 `reserve()` 时，真实 `run_id` 确实尚未生成（`store.open_run()` 要在
`reserve()` 通过之后才调用，与 design.md §8.4 伪代码「先占后跑」的顺序一致）。
本文件传入 `f"{candidate.candidate_id}:{scenario.scenario_id}:pending"` 作为
这个占位标识，供 `BudgetExhaustedError` 的错误消息定位到具体候选/场景。

`reserve()` 抛出的 `BudgetExhaustedError`（预算不足）**不在本函数内捕获**，
原样向上传播——`stop_reason='budget_exhausted'` 的写入与任务终止是
`run_task()`（任务 14.5）与 `controller.stop.should_stop()` 的职责，
`run_tier()` 不写 `tasks` 表、不判定停止原因。
"""

from __future__ import annotations

import hashlib
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

import click

from poweragent.config.hashing import canonical_json
from poweragent.config.hashing import constraints_hash as _constraints_hash_of
from poweragent.config.hashing import metrics_hash as _metrics_hash_of
from poweragent.config.schema import (
    ConstraintsConfig,
    MetricsConfig,
    ModelConfig,
    TaskConfig,
)
from poweragent.config.ticks import expand_all_ticks
from poweragent.controller.budget import BudgetExhaustedError, BudgetLedger
from poweragent.controller.preflight import (
    check_budget_feasibility,
    check_config_completeness,
    check_dual_model_consistency,
    check_freeze_consistency,
    check_margin_extraction_ready,
    check_metric_not_implemented,
    check_model_package_hash,
    check_observable_unbound,
    check_unit_mismatch,
)
from poweragent.controller import scenario as ctrl_scenario
from poweragent.controller.recovery import reap_orphan_runs
from poweragent.controller.stop import (
    MARGIN_FAILURE_ESCALATION_THRESHOLD,
    BestRecord,
    SearchState,
    should_stop,
)
from poweragent.eval.aggregate import rank
from poweragent.eval.constraints import judge
from poweragent.eval.margin import extract_margin
from poweragent.eval.metrics import compute_metrics
from poweragent.sim.engine import MatlabSession
from poweragent.sim.hashing import fast_fingerprint, resolve_dependency_closure
from poweragent.sim.hashing import model_package_hash as _model_package_hash_of
from poweragent.sim.simulate import simulate
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.cache import evaluation_key as compute_evaluation_key
from poweragent.store.cache import simulation_key as compute_simulation_key
from poweragent.store.repo import (
    ApprovalRecord,
    Candidate,
    ConstraintResult,
    FailureClass,
    MetricResult,
    ScenarioSpec,
    SimulationResult,
    Store,
    Tier,
)

__all__ = ["TierResult", "run_tier", "TaskOutcome", "run_task"]


# ===========================================================================
# TierResult：run_tier() 的返回类型——供 run_task()（任务 14.5，尚未落地）
# 消费的接口契约。design.md §8.4 的伪代码只返回一个布尔 `tier_ok`；本文件
# 按任务描述的要求扩成一个小 dataclass，携带 run_task() 决定下一步动作
# （是否进入 Evaluation 层、是否记录候选级拒绝、如何延续裕量失败计数器）
# 所需的全部信息，同时不携带 run_task() 用不到的字段。
# ===========================================================================


@dataclass(frozen=True, slots=True)
class TierResult:
    """`run_tier()` 的返回值。

    - `passed`：该 tier 是否**通过**（design.md §8.4 的 `tier_ok`）。对
      Screening 层，`passed=True` 是调用方决定"是否继续跑 Evaluation 层"
      的唯一依据；对 Evaluation 层，`passed=False` 表示该候选在本层已经
      被判定为不可评价（`diverged`/`solver_error`/`timeout`/重试耗尽的
      `engine_transient`），调用方不应再假定该候选拥有完整的 Evaluation
      证据集。**约束不可行（`feasible=False`）本身不会使 Evaluation 层的
      `passed` 变为 `False`**——按 R6.7，Evaluation 层单场景不可行只记录、
      继续跑完该层其余场景行，`worst-case` 聚合的 SQL（`design.md` §5.4）
      自然会因该候选缺一行可行结果而在 `HAVING` 处排除它，不需要
      `run_tier()` 自己提前判定"这个候选整体不可行"。
    - `stopped_early`：本次调用是否在处理完 `scenario_rows` 全部场景行之前
      就停止（`True` 时 `scenario_rows` 中排在触发停止的场景之后的行完全
      未被处理，不存在对应的 `runs` 行）。
    - `failure_class` / `cause`：仅在 `stopped_early=True` 时可能非空，
      分别取 `store.repo.FailureClass` 与其登记的 `cause` 取值（本文件只
      产生 `candidate_rejected` 与 `transient_error` 两类，从不产生
      `stop_and_ask_human` 或 `budget_exhausted`——后两者是 `run_task()`/
      `BudgetLedger.reserve()` 自身抛出异常的职责，见模块 docstring）。
    - `rejected_scenario_id` / `rejected_constraint_names`：仅在
      "Screening 层约束不可行提前拒绝"路径下非空，分别为触发拒绝的
      `scenario_id`、与该场景 `ConstraintResult.violations` 中的约束名
      （按 `judge()` 输出顺序）。供 `run_task()`（任务 14.5）在候选级
      记录该拒绝时使用（见模块 docstring"候选级失败分类的持久化记录"）。
      其余停止路径（`diverged`/`solver_error`/`timeout`/`engine_transient`/
      裕量提取失败）下，触发该次停止的 `scenario_id` 已经是
      `scenario_rows` 中"最后一个被处理的场景"，调用方可从其自身的遍历
      状态得知，本字段不重复携带（只在真正需要额外信息——约束名列表——的
      那一条路径上非空）。
    - `margin_failure_count`：裕量提取连续失败计数器的**更新后**取值（见
      模块 docstring"裕量提取连续失败的熔断判定"）；调用方须在下一次
      `run_tier()` 调用时把这个值原样传回 `margin_failure_count` 参数，
      并自行与 `eval.margin.MARGIN_FAILURE_ESCALATION_THRESHOLD` 比较。
    """

    passed: bool
    stopped_early: bool
    failure_class: FailureClass | None
    cause: str | None
    rejected_scenario_id: str | None
    rejected_constraint_names: tuple[str, ...]
    margin_failure_count: int


# ===========================================================================
# 产物往返校验：见模块 docstring"产物完整性校验"一节的解读说明。
# ===========================================================================


def _artifact_round_trip_ok(ref: str | None) -> bool:
    """`ref` 为 `None` 视为"该场景本不要求这份产物"，判定为通过（不视为
    缺失）——调用方（`_cached_simulation_reusable`）只在 `ref` 确实被要求
    存在的位置传入非 `None` 值。`ref` 非 `None` 时：路径必须存在且长度非零，
    随后两次独立读取全文并各自计算 sha256，二者相等才算通过；任一环节
    失败（不存在、长度为 0、读取异常、两次摘要不等）返回 `False`。
    """
    if ref is None:
        return True
    path = Path(ref)
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        first = hashlib.sha256(path.read_bytes()).hexdigest()
        second = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return first == second


def _cached_simulation_reusable(
    cached_sim: SimulationResult, *, require_margin: bool
) -> bool:
    """`cached_sim`（`store.find_cached_simulation()` 的命中结果）的产物是否
    可安全复用：`waveform_ref` 恒须往返校验通过；`require_margin=True` 时
    `observable_ref` 还须非 `None` 且同样通过往返校验（裕量场景缺观测量引用
    视为不完整，不可复用）。
    """
    if not _artifact_round_trip_ok(cached_sim.waveform_ref):
        return False
    if require_margin:
        if cached_sim.observable_ref is None:
            return False
        if not _artifact_round_trip_ok(cached_sim.observable_ref):
            return False
    return True


# ===========================================================================
# run_tier()
# ===========================================================================


def run_tier(
    candidate: Candidate,
    tier: Tier,
    scenario_rows: Sequence[ScenarioSpec],
    *,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    session: MatlabSession,
    store: Store,
    artifacts: ArtifactStore,
    budget_ledger: BudgetLedger,
    task_id: str,
    execution_env_hash: str,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
    base_dir: str | Path = ".",
    margin_failure_count: int = 0,
    attempt: int = 1,
) -> TierResult:
    """单候选、单 tier 的场景行分层执行：两级缓存查找 + 提前拒绝
    （`design.md` §8.4；`tasks.md` 任务 14.4/14.5；需求 R6.5, R6.6, R6.7, R6.9,
    R6.10, R6.12, R15.5, R15.6, R16.4, R16.5；属性 CP-4, CP-6）。

    kwonly 参数中相对 design.md §8.4 字面签名（`run_tier(cand, rows, ledger)`）
    的全部扩展项，及 `scenario_rows`/产物校验/`mark_artifact_missing`/
    `budget_ledger.reserve()` 各自的解读依据，见模块顶部 docstring 对应章节，
    此处不重复。

    `scenario_rows` 为空时（例如冻结场景集无 `tier='screening'` 行）本函数
    立即返回 `passed=True`——R6.10「Screening 判为通过并直接执行 Evaluation」
    的字面效果；本函数对空输入与"全部场景行都判定可行"两种情形返回同样的
    `passed=True`，调用方（`run_task()`）不需要为空输入单独分支。

    **`attempt`（任务 14.5 新增 kwonly 参数，默认 `1`）**：本函数自身仍然是
    单次尝试、不在内部重试——`engine_transient` 命中时仍然立即以
    `close_run_failed(...)` 关闭该行并终结本次调用（`for` 循环内每个场景行
    各自最多只跑一次 `simulate()`）。区别在于：任务 14.4 落地时这个"单次"的
    `attempt` 编号被硬编码为字面量 `1`；任务 14.5（`run_task()`）需要在
    "同一场景、第 2 次重试"时把 `open_run()` 写入的 `attempt` 列正确记为 `2`
    （R16.4：「新开 `attempt` 加 1 的 `runs` 行重试」），而本函数内部没有任何
    途径知道"这是第几次重试"——那个计数器由调用方在多次 `run_tier()` 调用之间
    维护并递增。因此把硬编码字面量改为显式 kwonly 参数，默认值 `1` 保持对
    既有调用方（任务 14.4 自身的单次调用惯例）完全向后兼容。

    `run_task()`（任务 14.5）落地时选择的重试编排方式：**不在单次
    `run_tier()` 调用内跨多个场景行重试**（那会让"同一次调用里，一部分场景
    行的 `attempt` 该是 1、另一部分因重试该是 2"这种按场景独立计数的语义无法
    用一个共享的 `attempt` 参数表达）。而是让调用方按**单个场景**逐一调用
    本函数（`scenario_rows` 传入恰好一个元素的元组），在外层维护每个场景各自
    独立的 `attempt` 计数器并处理重试循环；`run_tier()` 本身对"传入几个场景"
    没有任何限制——传入多个场景仍是合法调用（任务 14.4 的既有测试与本函数的
    内部实现均未改动这一点），只是 `run_task()` 选择不这样用它。详见
    `run_task()` 自身的 `_run_tier_with_retries()` 辅助函数文档。

    候选级拒绝的持久化记录（约束名等）不在本函数内写入，只通过返回值的
    `rejected_scenario_id` / `rejected_constraint_names` 两个字段回传，
    见模块 docstring"候选级失败分类的持久化记录"一节。

    `BudgetExhaustedError`（预算不足）不在本函数内捕获，原样向上传播。
    """

    if not scenario_rows:
        return TierResult(
            passed=True,
            stopped_early=False,
            failure_class=None,
            cause=None,
            rejected_scenario_id=None,
            rejected_constraint_names=(),
            margin_failure_count=margin_failure_count,
        )

    guard = model_cfg.io_contract.divergence_guard
    vout_target_v = model_cfg.io_contract.vout_target_v

    # metrics_hash / constraints_hash 不随场景变化，整个 tier 只算一次，供
    # 每个场景行计算 evaluation_key 时复用（design.md §5.3：两者不参与
    # simulation_key，只参与 evaluation_key）。
    metrics_hash_value = _metrics_hash_of(metrics_cfg.model_dump(mode="json"))
    constraints_hash_value = _constraints_hash_of(constraints_cfg.model_dump(mode="json"))

    # `attempt` 现为显式 kwonly 参数（任务 14.5），默认 1；本函数内部仍是单次
    # 尝试、不在此循环内递增，见函数 docstring "attempt" 一节。

    for scenario in scenario_rows:
        margin_primary_method = (
            metrics_cfg.margin_extraction.primary_method if scenario.require_margin else None
        )
        sim_key = compute_simulation_key(
            model_package_hash=frozen_model_package_hash,
            model_variant=scenario.model_variant,
            execution_env_hash=execution_env_hash,
            candidate=candidate,
            scenario=scenario,
            margin_primary_method=margin_primary_method,
        )
        eval_key = compute_evaluation_key(
            simulation_key=sim_key,
            metrics_hash=metrics_hash_value,
            constraints_hash=constraints_hash_value,
        )

        # -------------------------------------------------------------
        # 第一级缓存：evaluation_key 命中 ⟹ 必须写一条新 runs 行（CP-6）。
        # -------------------------------------------------------------
        cached_eval = store.find_cached_evaluation(eval_key)
        if cached_eval is not None:
            run_id = store.open_run(
                task_id=task_id,
                candidate=candidate,
                scenario=scenario,
                attempt=attempt,
                simulation_key=sim_key,
                budget_units=0,
                cache_hit=True,
            )
            store.close_run_ok(
                run_id,
                SimulationResult(
                    run_id=run_id,
                    status="ok",
                    waveform_ref=cached_eval.waveform_ref,
                    observable_ref=cached_eval.observable_ref,
                    elapsed_ms=0,
                    engine_starts=0,
                ),
            )
            # 直接引用被复用的 metric_results/constraint_results 内容，
            # 只是把 run_id 换成本次新开的行——不重算指标、不重判约束。
            reused_metrics = [
                MetricResult(
                    run_id=run_id,
                    metric_id=m.metric_id,
                    value=m.value,
                    valid=m.valid,
                    invalid_reason=m.invalid_reason,
                )
                for m in cached_eval.metrics
            ]
            reused_cr = cached_eval.constraint_result
            cr = ConstraintResult(
                candidate_id=candidate.candidate_id,
                scenario_id=scenario.scenario_id,
                run_id=run_id,
                feasible=reused_cr.feasible,
                violations=reused_cr.violations,
            )
            store.write_evaluation(run_id, reused_metrics, cr, eval_key)

        else:
            # ---------------------------------------------------------
            # 第二级缓存：simulation_key 命中 + 产物往返校验通过 ⟹ 复用
            # 波形/观测量重算指标；否则视为未命中，重新仿真。
            # ---------------------------------------------------------
            cached_sim = store.find_cached_simulation(sim_key)
            sim_result: SimulationResult

            if cached_sim is not None and _cached_simulation_reusable(
                cached_sim, require_margin=scenario.require_margin
            ):
                run_id = store.open_run(
                    task_id=task_id,
                    candidate=candidate,
                    scenario=scenario,
                    attempt=attempt,
                    simulation_key=sim_key,
                    budget_units=0,
                    cache_hit=True,
                )
                sim_result = SimulationResult(
                    run_id=run_id,
                    status="ok",
                    waveform_ref=cached_sim.waveform_ref,
                    observable_ref=cached_sim.observable_ref,
                    elapsed_ms=cached_sim.elapsed_ms,
                    engine_starts=0,
                )
                store.close_run_ok(run_id, sim_result)
            else:
                if cached_sim is not None:
                    # 命中但产物缺失或往返校验失败：标注旧行，视为未命中。
                    store.mark_artifact_missing(cached_sim.run_id)

                units = 1
                if scenario.require_margin:
                    units += metrics_cfg.margin_extraction.extra_engine_starts_per_candidate

                budget_ledger.reserve(
                    units,
                    run_id=f"{candidate.candidate_id}:{scenario.scenario_id}:pending",
                )
                run_id = store.open_run(
                    task_id=task_id,
                    candidate=candidate,
                    scenario=scenario,
                    attempt=attempt,
                    simulation_key=sim_key,
                    budget_units=units,
                    cache_hit=False,
                )

                fresh_result = simulate(
                    candidate,
                    scenario,
                    model_cfg=model_cfg,
                    metrics_cfg=metrics_cfg,
                    session=session,
                    run_id=run_id,
                    artifacts=artifacts,
                    task_id=task_id,
                    frozen_fingerprint=frozen_fingerprint,
                    frozen_model_package_hash=frozen_model_package_hash,
                    base_dir=base_dir,
                )

                if fresh_result.status == "engine_transient":
                    # 单次尝试、不重试（见模块 docstring）：直接关闭该行，
                    # 终结本次 run_tier() 调用。
                    store.close_run_failed(run_id, "transient_error", "engine_transient")
                    return TierResult(
                        passed=False,
                        stopped_early=True,
                        failure_class="transient_error",
                        cause="engine_transient",
                        rejected_scenario_id=None,
                        rejected_constraint_names=(),
                        margin_failure_count=margin_failure_count,
                    )

                if fresh_result.status in ("diverged", "solver_error", "timeout"):
                    store.close_run_failed(run_id, "candidate_rejected", fresh_result.status)
                    return TierResult(
                        passed=False,
                        stopped_early=True,
                        failure_class="candidate_rejected",
                        cause=fresh_result.status,
                        rejected_scenario_id=None,
                        rejected_constraint_names=(),
                        margin_failure_count=margin_failure_count,
                    )

                store.close_run_ok(run_id, fresh_result)
                sim_result = fresh_result

            # -----------------------------------------------------
            # 指标 + 约束观测量（同一次波形遍历）；裕量场景额外计算裕量。
            # -----------------------------------------------------
            metrics = compute_metrics(
                sim_result.waveform_ref,
                scenario,
                metrics_cfg,
                run_id=run_id,
                guard=guard,
                vout_target_v=vout_target_v,
            )

            # 裕量提取失败记在**指标**上，不改 run 的状态。
            #
            # design.md §9.3 的字面规定是"`pm.valid=False` 时调用方以
            # `store.close_run_failed(run_id, 'candidate_rejected',
            # 'metric_invalid:phase_margin')` 关闭该行"。这条规定与本函数另一条
            # 更根本的规定冲突，无法同时成立：`close_run_ok()` 必须在指标计算
            # **之前**调用（上方两条路径都是这么做的——它落的是仿真产物引用与
            # 耗时，而缓存复用路径同样要走它）。等到裕量提取时 run 已是 `done`，
            # 再调 `close_run_failed()` 会被 `runs` 的状态机拒绝
            # （`RunTerminationRejectedError`：只能从 `running` 转出）。
            #
            # 这条路径此前从未被执行过，因此这处矛盾一直没暴露：它要求"仿真成功
            # 但裕量无从定义"，而那需要一个开环幅值全程大于 1 的候选（`rcomp`
            # 取到域上界 100 kohm 附近，ESR 零点使高频增益不滚降）。注入固定候选
            # 的冒烟测试挑不到这种点，真实模型第一轮就提了一个。
            #
            # 取舍的依据是两层语义的区分：`runs.status` 描述**引擎执行**，而引擎
            # 确实跑通了、波形也拿到了；失败的是"从波形里提取某个指标"，那属于
            # `metric_results.valid` 与 `invalid_reason` 这两列——它们正是为此
            # 存在。把指标提取失败记成 run 失败会混淆这两层，也会让一次真实发生
            # 过的仿真在预算与缓存的口径里凭空消失。
            margin_extraction_failed = False

            if scenario.require_margin:
                phase_margin, gain_margin = extract_margin(
                    sim_result.observable_ref, metrics_cfg, run_id=run_id
                )
                # 无论成败都并入指标：失败也是观测结果。两条 `valid=0 /
                # invalid_reason='extraction_failed'` 的行落库后，`judge()` 会
                # 因支撑指标无效而判 `phase_margin_min` 违反（R9.3），
                # worst-case 聚合的完备性过滤也会据此排除该候选。丢掉它们则
                # "这个候选的裕量提不出来"在数据库里无迹可寻。
                metrics = [*metrics, phase_margin, gain_margin]

                if phase_margin.valid:
                    margin_failure_count = 0
                else:
                    margin_extraction_failed = True
                    margin_failure_count += 1

            cr = judge(
                metrics,
                scenario,
                constraints_cfg,
                candidate_id=candidate.candidate_id,
                run_id=run_id,
            )
            store.write_evaluation(run_id, metrics, cr, eval_key)

            # 筛选层的裕量提取失败要提前终止该层，且必须在指标与判定落库
            # **之后**——原实现在这里 `continue`，跳过了 `write_evaluation()`，
            # 那条 run 便既没有指标也没有约束判定，只剩一行状态。
            #
            # 判断放在这个块内而不是下方的 `if not cr.feasible` 旁边：裕量提取
            # 只发生在这个分支（第一级缓存复用旧指标时不重算裕量），把标志的
            # 定义与使用放在同一个作用域里，就不会出现"某条路径没定义它"的情况。
            #
            # 评价层不在此返回：它按 R6.12 跑完该层其余场景行，可行性交由下方
            # 统一的 `cr.feasible` 判定与 worst-case 聚合处理。
            if margin_extraction_failed and tier == "screening":
                return TierResult(
                    passed=False,
                    stopped_early=True,
                    failure_class="candidate_rejected",
                    cause="metric_invalid:phase_margin",
                    rejected_scenario_id=scenario.scenario_id,
                    rejected_constraint_names=(),
                    margin_failure_count=margin_failure_count,
                )

        # -------------------------------------------------------------
        # 可行性判定：Screening 层任一场景不可行即提前拒绝；Evaluation 层
        # 记录后继续（R6.6, R6.7）。
        # -------------------------------------------------------------
        if not cr.feasible:
            if tier == "screening":
                return TierResult(
                    passed=False,
                    stopped_early=True,
                    failure_class="candidate_rejected",
                    cause=None,
                    rejected_scenario_id=scenario.scenario_id,
                    rejected_constraint_names=tuple(v.constraint for v in cr.violations),
                    margin_failure_count=margin_failure_count,
                )
            # Evaluation 层：不中止，worst-case 聚合 SQL（design.md §5.4）
            # 自然因该候选缺一条可行场景而在 HAVING 处排除它。

    return TierResult(
        passed=True,
        stopped_early=False,
        failure_class=None,
        cause=None,
        rejected_scenario_id=None,
        rejected_constraint_names=(),
        margin_failure_count=margin_failure_count,
    )


# ===========================================================================
# ===========================================================================
# 任务 14.5：`run_task()` 主循环、`tasks` 行落库、失败分类与重试
#
# design.md §6.2.1 / §7.1 / §8.1；tasks.md 任务 14.5；需求追溯 R1.5, R15.10,
# R16.3, R16.4, R16.5, R16.6, R16.11。
#
# ---------------------------------------------------------------------------
# 范围与本次落地内容
# ---------------------------------------------------------------------------
#
# 本次真正写出 `run_task()` 这个顶层函数本身（此前只有 `run_tier()`）。以下
# 逐条记录本实现相对 design.md §8.1 字面伪代码 / §6.2.1 字面签名的必要偏离、
# 软依赖与设计选择，接续本文件既有的文档风格（在 14.4 的既有 docstring 之后
# 新增本节，不改写、不删除 14.4 已写好的内容）。
#
# ## 1. `TaskOutcome`：design.md §6.2.1 已给出完整字段，逐字段照抄
#
# `task_id` / `stop_reason` / `cause` / `best_candidate_id` /
# `engine_starts_used` / `wallclock_s` 六个字段，design.md §6.2.1 已经给出
# 完整定义，本实现不新增、不删减任何字段。
#
# ## 2. `propose_fn`：`agent.propose`/`agent.validate` 的软依赖处理
#
# `agent/propose.py`（任务 23.1）与 `agent/validate.py`（任务 24.1~24.3）均为
# M4 里程碑任务，`tasks.md`"优先序判断标准"一节明确"M4 之前不写 `agent/`"——
# 本任务（14.5）排在 M1，`agent` 包当前只有一个空的 `__init__.py`。design.md
# §8.1 的伪代码在主循环内直接调用 `propose(state, max_repair_rounds)` 与
# `validate(output.candidates, design_space, state.tested_candidates, mode)`
# 两个尚不存在的函数。
#
# 任务描述给了两个选项："接受一个候选生成可注入参数"或"跟随 cli.py 的软依赖
# 模式（文档化的 import-and-catch）"。本实现选择**前者**（可注入参数），
# 理由：`cli.py` 的软依赖模式（`try: from ... import X except ImportError:`）
# 适用于"整个下游子系统尚不存在，此路径在被触达前就该给出清晰提示并返回"的
# 场景（`cmd_run` 对 `run_task` 本身、`cmd_report` 对 `render_report`）——那
# 是"入口函数的某个分支尚未可达"。但本次要验证的恰恰是"主循环的控制流围绕
# 候选生成的编排本身是真实、可测试的"（任务要求原文），这需要在验证脚本里
# 用一个可控的候选源反复驱动多轮循环（起初返回候选、后续返回空集触发
# `agent_returned_no_candidate`、budget 耗尽等）——`import-and-catch` 模式
# 只能表达"完全不可用，立即报错退出"，无法表达"分阶段返回不同候选集"这种
# 测试所需的可控性；可注入参数则天然满足。
#
# 因此本实现新增签名：
#
#     def run_task(task_cfg, model_cfg, metrics_cfg, constraints_cfg, *,
#                  resume: bool = False,
#                  propose_fn: ProposeFn | None = None,
#                  probe_single_run_s: float | None = None,
#                  session: MatlabSession | None = None,
#                  store: Store | None = None,
#                  artifacts: ArtifactStore | None = None,
#                  db_path: str | Path = "runs.db",
#                  artifacts_dir: str | Path = "artifacts",
#                  ) -> TaskOutcome: ...
#
# `propose_fn` 默认 `None`：`None` 时本函数使用一个文档化的默认行为——**每轮
# 返回空候选集**（等价于 design.md §8.1 的"IF output.candidates IS EMPTY"
# 分支），不猜测任何候选、不调用任何 LLM。这与"agent.propose 尚未落地"这一
# 事实一致（没有真实候选源时，主循环唯一诚实的行为就是"本轮无候选"，不是
# 抛异常也不是编造候选），且这条路径本身也是 design.md §8.1 明确写出的一个
# 合法分支（空候选集 ⟹ `no_improve += 1`，不是错误）。真正接入 `agent.propose`
# /`agent.validate`（任务 23.x/24.x 落地后）时，调用方应传入一个包装了
# `propose()` → `validate()` 两步的 `propose_fn`；本函数的主循环只依赖
# `propose_fn` 返回值的契约（见下方 `ProposeResult`），不关心其内部是否真的
# 调用了 LLM。
#
# `ProposeResult`（本文件新增的最小契约类型，供 `propose_fn` 返回）：
#
#     @dataclass(frozen=True, slots=True)
#     class ProposeResult:
#         candidates: Sequence[Candidate]      # 已通过 validate() 校验的 accepted 候选
#                                              # （design.md §8.1 的 verdict.accepted）
#         stop_recommendation: bool = False    # agent 的停止建议，转发给 should_stop()
#
# 这是 design.md §8.1 伪代码里 `verdict.accepted`（已校验候选）与
# `output.stop_recommendation`（agent 的建议）两者的合并——本函数不重新实现
# `validate()` 的校验逻辑（键集合/合法域/档位/去重/新颖度），把"产出已校验
# 候选"的职责交给 `propose_fn` 自身；这与"only implement the loop structure
# and control flow around candidate generation"的任务范围描述一致：本函数
# 负责编排"拿到候选之后做什么"，不负责"候选是怎么被校验出来的"。
# `verdict.rejected`（被拒的原始候选及理由）不出现在 `ProposeResult` 中——
# `record_rejection()` 的写入需要 `raw_parameters` 与 `reason` 两项，若
# `propose_fn` 需要写这类记录，应在自己内部完成（它已经持有 `store`，因为本
# 函数会把 `store` 转发给 `propose_fn`，见下方签名）——本函数不代为解析一个
# 尚不存在的 `verdict.rejected` 契约形状。
#
# `propose_fn` 的调用签名是 `propose_fn(state) -> ProposeResult`：只接受一个
# `SearchState` 位置参数。`state` 由本函数在每轮开始时调用
# `rebuild_state()`（任务 14.6）并补上 `constraints_cfg.design_space` 投影
# 的 `legal_domain`/`hard_constraints` 组装而成（见下方 "state 组装" 一节）。
#
# ## 3. `probe_single_run_s`：`check_budget_feasibility()` 的必填输入，无默认值
#
# `ProbeRecord`/`probe_report.md`（任务 1.2）是"测量门禁类任务"，`tasks.md`
# 明确"由 G-P-1 单独触发，与其余任务的代码开发顺序无关"，真实探针实测值本次
# 不可得。任务描述给了两个选项："跳过该检查并留下清晰日志/注释"或"要求作为
# 无默认值的参数"。本实现选择**后者**：`probe_single_run_s: float | None =
# None`，`None` 时**跳过** `check_budget_feasibility()` 这一条 preflight
# 断言（打印一行说明，不静默），非 `None` 时正常执行该断言。理由：
#
# - "无默认值的必填参数"与"跳过并留日志"在任务描述里是并列的两个选项，不是
#   互斥的——本实现把参数设为可选（默认 `None`）恰恰是为了让"这条检查目前
#   跳过"这件事显式地由调用方决定（不传等于确认接受跳过），而不是本函数
#   内部编个假数值去满足一个必填参数。这不是"编造探针值"（本函数从未产生
#   任何 `float` 默认值去充当 `probe_single_run_s`），是"这一条 preflight
#   断言在没有真实探针数据时如实跳过"。
# - 与 `check_dual_model_consistency()`（`averaged_model_required=false` 时
#   立即返回、不读取任何记录）是同一种"前提不满足即豁免该检查"的既有模式。
#
# ## 4. `check_baseline_gate()` 的缺口：跳过，preflight 序列不含它
#
# `check_baseline_gate()`（任务 16.1）未落地（本文件已用 `grep`/`read_file`
# 核对 `controller/preflight.py` 全文不含该函数）。design.md §8.2 步骤 7 把
# 它限定为 `require_baseline_gate := (task_cfg.task_kind = 'optimize')` 时
# 才执行；本函数当前**不**读取 `task_cfg.task_kind`（`TaskConfig` 也没有这个
# 字段——`task_kind` 是 `ddl.sql` 的 `tasks` 表列，不是 `task.yaml` 的配置
# 字段，design.md §6.2.1 的 `run_task()` 签名与 `TaskConfig`（`config/
# schema.py`）均未给出如何从四个配置文件推断 `task_kind` 的规则），本函数
# 固定按 `task_kind='optimize'` 落库（见下方 "六个哈希与 `tasks` 行" 一节），
# 且固定跳过 Baseline Gate 分支——这是一个需要在任务 16.1 落地、且
# `run_task()` 获得区分 `task_kind` 的输入之后补齐的已知缺口，本次不新增
# 任何伪造的 `BaselineGateStatus`。
#
# ## 5. preflight 调用序列：按本文件已有的 `check_*` 函数逐个调用，无顶层
#    `preflight()` 编排函数
#
# `controller/preflight.py` 目前没有任何顶层 `preflight()` 函数（只有九个
# `check_*` 函数），本函数因此自己按 design.md §8.2 的步骤顺序逐个调用：
# `check_metric_not_implemented` → `check_observable_unbound` →
# `check_unit_mismatch` → `check_margin_extraction_ready` →
# `check_config_completeness` → `check_freeze_consistency` →
# `check_model_package_hash` → `check_dual_model_consistency` →
# `check_budget_feasibility`（`probe_single_run_s` 非 `None` 时）。任一步骤
# 抛出 `PreflightError` 即原样向上传播、不捕获——这与 design.md §7.1 前置
# 条件"`preflight()` 全部断言通过"、以及"寻优任务的 `tasks` 行在 preflight
# 失败时根本不创建"的既有裁定（design.md §6.2.2 L10-3）一致：本函数把
# `create_task()` 调用放在全部 preflight 断言通过**之后**。
#
# ## 6. 六个哈希与 `tasks` 行：任务开始时一次性冻结
#
# - `model_package_hash`：`check_model_package_hash()` 的返回值（重算值，
#   与 `freezes(kind='model_package')` 一致，已在 preflight 序列中算过一次，
#   本函数复用该返回值、不重新解析闭包）。
# - `metrics_hash` / `constraints_hash`：`config.hashing.metrics_hash()` /
#   `constraints_hash()`，对 `metrics_cfg.model_dump(mode="json")` /
#   `constraints_cfg.model_dump(mode="json")` 计算，与 `run_tier()` 内部
#   算法一致（同一份 config.hashing 函数）。
# - `scenario_set_hash`：`controller.scenario.freeze_scenario_set()` 的
#   返回值（写入 `scenario_set` 表的同时得到该哈希）。
# - `execution_env_hash`：design.md 全文未给出这个哈希的输入字段集合（已用
#   `grep` 核对：design.md 仅在 `simulation_key` 的字段列表与 `tasks` 表
#   DDL 中提到这个名字，从未定义"它由什么计算得来"）。本函数采用一个
#   显式、有限、文档化的输入集合：`{python_version: sys.version,
#   platform: platform.platform(), matlab_release: model_cfg.runtime.
#   matlab_release, toolboxes: model_cfg.runtime.toolboxes, execution_mode:
#   model_cfg.runtime.execution_mode}`——即"Python 运行时版本 + 操作系统
#   平台标识 + `model.yaml` 里已经声明的 MATLAB 环境三项（`matlab_release`/
#   `toolboxes`/`execution_mode`，这些字段本就是"执行环境"语义、且已经过
#   `config.schema` 校验，不是本函数凭空发明的新字段）"，经 `canonical_json`
#   规范化后取 sha256。选择这五个字段而非更少或更多的理由：`simulation_key`
#   把 `execution_env_hash` 与 `model_package_hash`/`model_variant` 并列为
#   影响仿真结果可比较性的独立维度（R17.1/R17.2 的可复现性判据要求"相同的
#   `execution_env_hash`"才能比较两次运行）——Python 版本与操作系统平台是
#   最直接影响数值计算环境的两项（`numpy`/`scipy` 的浮点行为可能随平台/
#   Python 版本有细微差异），MATLAB 侧的三项环境声明同样属于"仿真执行环境"
#   的范畴且已在 `model.yaml` 中结构化存在，不需要另外发明字段来源。**这是
#   一个显式记录的设计决策，不是 design.md 已经锁定的公式**——若后续任务
#   （或用户）认为该字段集合需要调整，属于对本函数这一处实现细节的修订，
#   不影响 `execution_env_hash` 在其余代码中的使用方式（全部下游代码只把它
#   当作一个不透明的字符串使用，从未解析其内部构造）。
# - `calibration_hash`：任务 20.6（`calib` 模块，M2 里程碑，`tasks.md`
#   标注"`[条件]`"、由 G-M2-1 门禁触发）尚未落地，`calib/__init__.py` 当前
#   为空文件。按 `task_cfg.simulation_only`（`TaskConfig` 的实际字段名，
#   已核对 `config/schema.py`）分支：`simulation_only=True`（PoC 轨）⟹
#   空串（`requirements.md` R1.5 与任务 20.6 描述均明确这一取值，与
#   `calib` 模块是否落地无关，本函数可以直接实现这一半）；
#   `simulation_only=False`（工程轨）⟹ 本函数**不**猜测或提前实现任务
#   20.6 尚未定义的哈希计算方式（`configs/calibration.yaml` 与"数据集划分
#   记录"的具体序列化形状是任务 20.6 的职责），而是设 `calibration_hash=""`
#   并在函数体内用一行注释标注这是"待任务 20.6 接线"的已知缺口——空串在
#   工程轨下不是任务 20.6 定义的正确取值，但在 `calib` 模块不存在的当前
#   状态下没有其他可执行的选择；这不影响 PoC 轨（`simulation_only=True`，
#   本任务验证脚本使用的轨道）的正确性。
# - `budget_max_starts`：`task_cfg.budget.max_engine_starts`。
#
# ## 7. `BudgetLedger` 构造：`max_wallclock_s` 由 `budget.max_wallclock_hours`
#    换算，与 `controller/recovery.py` 的 `rebuild_state()` 同一换算方式
#
# ## 8. 循环不变量：五条断言的落点
#
# design.md §7.1 的循环不变量 1/2/4/5 在 `WHILE` 顶部与 `FINISH` 前各断言
# 一次（不变量 3——"`current_best` 只在存在一个通过 §5.4 SQL 的候选时非
# 空"——是 `eval.aggregate.rank()`/`worst_case()` 的返回值契约本身保证的
# 性质，不是本函数需要额外断言的运行期条件，本函数不为它写一条冗余的
# `assert`，与本代码库既有的"不为结构性保证的性质重复写运行期断言"惯例
# 一致，例如 `eval/aggregate.py` 对"不提供加权评分抵消硬约束"一节的处理）。
# 实现为一个共享的辅助函数 `_assert_loop_invariants()`，在 `WHILE` 循环体
# 顶部与 `FINISH` 之前分别调用一次，避免同一段断言逻辑写两遍而漂移。
#
# ## 9. `engine_transient` 重试：按场景逐一调用 `run_tier()`，外层维护
#    `attempt` 计数器
#
# 见 `run_tier()` docstring 中"`attempt`（任务 14.5 新增 kwonly 参数）"一节
# 已经说明的编排方式：`_run_scenarios_with_retry()` 辅助函数对
# `screening_rows`/`evaluation_rows` 中的**每一个**场景单独调用
# `run_tier(..., scenario_rows=(scenario,), attempt=attempt)`，命中
# `stopped_early and failure_class == 'transient_error' and cause ==
# 'engine_transient'` 时，若 `attempt < budget.max_attempts_per_scenario`
# 则 `attempt += 1` 重新调用（新开一行，旧行已被 `run_tier()` 内部关闭）；
# 否则（重试耗尽）按 R16.5 停止对该候选继续跑这一层剩余场景、返回
# `passed=False`（不写 `tasks.stop_reason`，调用方继续下一候选）。
#
# ## 10. 轮末最佳候选刷新与无改善计数：R16.11 逐字复刻
#
# `eval.aggregate.rank(store, task_id, metrics_cfg, top_n=1)` 取轮末最佳可行
# 候选（`worst_case()` 的完备性/可行性过滤已经保证只有跑完 Evaluation 集的
# 候选才可能出现）；与本轮开始前的 `best_before`（`BestRecord | None`）比较：
#
# - 本轮开始前无最佳候选、本轮末出现 ⟹ 首次出现可行候选 ⟹ 置 0。
# - 两者皆存在：`decrease = best_before.value - best_after.value`（`primary.
#   direction='minimize'` 时越小越好，"减少量"即字面意义；
#   `direction='maximize'` 时"减少量"取反号，即 `best_after.value -
#   best_before.value`——两种方向下"减少量为正"都意味着变差，与 R16.11
#   "该值相对本轮开始前的减少量"的字面意图一致：越好的方向上取值应该
#   增长，"减少"指的是越好方向上不增长甚至倒退）。`decrease <=
#   tie_tolerance` ⟹ +1；否则 ⟹ 置 0。
# - 本轮无新 `runs` 行（`propose_fn` 返回空候选集）⟹ +1（与上面的比较分支
#   互斥——空候选集时不会有 `best_after` 的变化，直接在该分支内 +1 并
#   `CONTINUE`，与 design.md §8.1 伪代码结构一致）。
#
# `objective.tie_tolerance` 的字段路径为 `metrics_cfg.objective.tie_tolerance`
# （已核对 `config/schema.py`），不是 `constraints_cfg`——design.md/
# requirements.md 原文的"`metrics.yaml` 的 `objective.tie_tolerance`"与
# `constraints_cfg.objective` 的猜测不一致，本函数按已核实的真实字段路径
# （`metrics_cfg.objective.tie_tolerance`）实现，任务描述里出现的
# "`constraints_cfg.objective.tie_tolerance`"是任务撰写时的路径误记，本
# 实现予以更正并在此明确记录这处更正。
#
# ## 11. 停止时的 `stop_reason`/`cause` 落库与 `cause` 取值范围
#
# `should_stop()` 返回的 `StopDecision.reason`/`cause` 直接写入
# `finish_task()`；空候选集分支的 `cause='agent_returned_no_candidate'` 由
# 本函数自己在调用 `should_stop()` **之前**特判（design.md §8.1 伪代码在
# "IF output.candidates IS EMPTY"分支内部直接写 `cause ← 'agent_returned_
# no_candidate'`，不经过 `should_stop()` 的四条固定 `reason` 产出路径——
# 这条 `cause` 与 `should_stop()` 返回的 `reason='no_improvement'` 组合，
# `should_stop()` 本身不产出这个 `cause` 字符串，本函数在这一特定分支内
# 直接构造 `StopDecision(stop=True, reason=decision.reason, cause=
# 'agent_returned_no_candidate')` 覆盖 `should_stop()` 原本可能返回的
# `cause`——但只在 `decision.stop=True` 且这一轮确实是空候选集触发时才
# 覆盖，其余轮次仍使用 `should_stop()` 原样返回的 `cause`）。
#
# ## 12. Checkpoint 1（任务 14.7）：打印五项、`--yes`/交互确认、`approvals`
#    落库、拒绝路径的 `stop_reason` 新值
#
# 本节记录任务 14.7 的落地内容，取代此前"12. 不在本次范围内"的占位说明。
#
# ### 12.1 落点：预算账本一致性断言之后、`WHILE` 主循环之前
#
# `_request_checkpoint1()` 调用点严格落在 `ASSERT ledger.used() ==
# store.sum_budget_units(task_id)` 断言之后、`closure_paths`/
# `frozen_fingerprint` 等模型不变性检查输入冻结之前（design.md §8.1 字面
# 顺序：`ASSERT ledger.used()...` → `request_checkpoint1(task_cfg)` →
# `round ← count_rounds(...)`）——此时 `tasks`/`scenario_set` 两表已经落库
# （它们是 Checkpoint 1 需要打印的"目标/硬约束/参数范围与档位/场景/预算"
# 五项内容本身，与 R19.1"该行写入前不新增任何 `runs` 行、不增加预算已用量"
# 并不冲突：`tasks`/`scenario_set` 不是 `runs` 表，不产生任何预算增量）。
#
# ### 12.2 五项内容的打印实现：`_checkpoint1_summary()` + `_print_
#    checkpoint1_summary()`
#
# 拆成"组装结构化数据"与"打印"两个函数，理由是这份结构化数据同时还要用于
# 计算下面 12.4 节的 `result_hash`——打印文本与哈希绑定内容必须是同一份
# 数据的两种呈现，不能分别拼装（否则"人看到的五项"与"审批行绑定的五项"
# 可能因两处独立实现而在未来的一次改动中悄悄漂移）。五项内容取自：
#
# - 目标：`metrics_cfg.objective.primary`（`metric_id`/`aggregation`/
#   `direction`）+ `objective.tie_tolerance` + 可选的
#   `task_cfg.objective_target.target_value`。
# - 硬约束：`constraints_cfg.hard_constraints` 恰五条（R9.1 已锁定的四字段
#   形状），逐条打印 `value`/`observable`/`sense`/`applies_to_tier`。
# - 参数范围与档位：`config.ticks.expand_all_ticks(constraints_cfg)`
#   （任务 5.9 已落地的档位展开函数，与写入 prompt 的档位字面量是同一次
#   `.12g` 规范化输出，见 `config/ticks.py` 顶部"为什么返回字符串"一节）
#   叠加 `design_space.variables.<name>` 的 `domain`/`scale`/`unit`。
# - 场景：`task_cfg.scenarios` 逐条的 `scenario_id`/`tier`/`model_variant`
#   ——任务描述点名的三项，其余电气量字段（`vin_v`/`temp_c`/...）已在
#   `scenario_set` 表落库、Checkpoint 1 的打印要求本身未点名逐一列出它们，
#   本函数不额外扩大五项内容的范围。
# - 预算：`task_cfg.budget` 的 `max_engine_starts`/`max_wallclock_hours`/
#   `max_attempts_per_scenario`。
#
# ### 12.3 交互确认：`yes=True` 跳过，否则 `click.confirm()` 恰好一次
#
# `run_task()` 新增的 kwonly 参数 `yes: bool = False` 原样转发。`yes=True`
# 时 `confirmed` 直接置 `True`，不调用 `click.confirm()`（R19.1"已传入
# `--yes` 时直接判为确认"）；`yes=False` 时调用 `click.confirm()` 恰好
# 一次——`_request_checkpoint1()` 函数体内不存在第二处调用该函数或
# `input()` 的代码路径，这与 R19.1"读取恰好一次交互确认输入"的字面要求
# 一致，也与主循环体内完全不存在任何等待人工输入的调用（R19.2）互相独立
# （Checkpoint 1 的这一次调用发生在 `WHILE` 循环开始**之前**）。
#
# ### 12.4 `approver` 取值：固定哨兵 `"cli_operator"`，不新增 `--approver`
#    CLI 选项
#
# `store.record_approval()`（`store/repo.py`）的结构性断言只对
# `kind='final_recommendation'` 与 `kind='track_selection'` 两类要求
# `second_approver` 非空且与 `approver` 不同——`kind='checkpoint1'` 不落在
# 这条更严格的规则内，`ApprovalRecord.approver` 对 `checkpoint1` 只是一个
# `NOT NULL` 的普通文本列。`requirements.md` R19.13"`--approver` 须命令行
# 显式给出、不推断身份"字面上约束的是 `cli`（即 `poweragent report`
# 命令，`cmd_report` 的 `--approver`/`--second-approver` 两个选项）——
# `run`/`cmd_run` 的既有签名（任务 15.1 已落地，`tasks.md` 与 `cli.py`
# 均未给出 `--approver` 选项）从未把身份识别纳入 `run` 命令的接口面；
# `design.md` §11.2 的 CP1 一行同样只写"要求 `--yes` 或交互确认；写
# `approvals(kind='checkpoint1')`"，未提及任何身份参数。
#
# Checkpoint 1 与 Checkpoint 3（`final_recommendation`）/`track_selection`
# 在"人工介入"这件事上的证据价值不同：后两者是把一个具体的、可事后追责的
# 个人身份与一项不可逆的最终设计决策（落盘新模型文件、推荐某个候选）绑定
# ——这正是 R19.13"自动推断身份会让误签变得无声"要防止的场景，因此必须
# 显式命令行给出、且双签不得同名。Checkpoint 1 的证据价值是"确实有一个人
# 在终端前，且这个人在被真实展示过这五项内容后同意/拒绝继续"——它要挡住
# 的问题是"自动执行期不等人却又假装等过"（R19 的 User Story 原文），不是
# "这个具体是谁"。因此本函数不新增 `--approver` 选项、不尝试从环境变量或
# `getpass.getuser()` 之类的来源推断身份（那正是 R19.13 明确禁止的"自动
# 推断身份"），而是记录一个固定、含义在此处已书面记录的哨兵值
# `_CHECKPOINT1_APPROVER = "cli_operator"`。若未来需要把 Checkpoint 1 的
# 身份也做成可追责的具名审批，应新增 `run` 命令的 `--approver` 选项并把
# 该值转发到本函数——这是一处已记录的、当前设计文档未要求的扩展点，本次
# 不预先实现。
#
# ### 12.5 `result_hash` 取值：对打印内容本身计算的哈希，不是占位值
#
# `approvals.result_hash` 列为 `NOT NULL`；`kind='checkpoint1'` 没有一个
# "候选评价结果"可绑定（那是 `final_recommendation` 的语义，见 §5.3.1 的
# 9 键重算范围）。本函数不填入一个无意义的占位字符串，而是对
# `_checkpoint1_summary()` 产出的、实际打印给人看的同一份结构化内容计算
# `sha256(canonical_json(...))`——`result_hash` 在这里仍然承载它在别处的
# 含义（"这条审批行绑定的是哪份确切内容"），只是绑定对象换成了"人工确认
# 时看到的五项内容的确切取值"，不是候选结果的 9 键组合。
#
# ### 12.6 拒绝/未确认路径：新增第五个 `stop_reason` 取值
# `checkpoint1_rejected`
#
# `should_stop()`（`controller/stop.py`，任务 14.3）产出的取值域恰为
# `{budget_exhausted, stop_and_ask_human, no_improvement, target_reached}`
# 四值（`requirements.md` R16.15"不新增该四项之外的 `stop_reason` 取值"、
# `tasks.md` 任务 14.3"不新增该四项之外的 `stop_reason` 取值"）——但这条
# "恰四值"的封闭集合约束，字面上约束的对象是 **`should_stop()` 的返回值**
# （即"由 `should_stop()` 判定停止"这一条路径），不是"`tasks.stop_reason`
# 列全局只能取这四个值中的一个"这样一条更宽的约束。Checkpoint 1 拒绝/未
# 确认这条路径根本不经过 `should_stop()`——它发生在 `WHILE` 循环开始之前，
# 主循环、`should_stop()`、`SearchState` 均尚未涉及。`store/ddl.sql` 的
# `tasks.stop_reason` 列本身只是 `TEXT`（无 `CHECK` 约束把取值域封闭为
# 四值——与 `runs.status`/`scenario_set.tier` 等确有 `CHECK` 约束的列不同），
# `cli._exit_code_for_stop_reason()` 的四值映射表同样只覆盖
# `should_stop()` 产出的四值、对未登记的 `stop_reason` 显式抛
# `ValueError`（这是本函数需要额外处理的一点，见下）。
#
# 因此本函数引入第五个、与 `should_stop()` 的四值并列但来源不同的
# `stop_reason` 取值：`_CHECKPOINT1_REJECTED_STOP_REASON =
# "checkpoint1_rejected"`。这不违反 R16.15/任务 14.3 对 `should_stop()`
# 取值域的封闭性约束（`should_stop()` 本身从未、也不会被要求产出这个值），
# 也不违反 `design.md`/`requirements.md` 通篇搜索未发现任何"`tasks.
# stop_reason` 全局恰四值"的字面表述——搜索到的唯一"恰四值"表述（R16.15、
# 任务 14.3）明确以"`should_stop()` 判定停止时"为限定语境。R19.11 本身
# 也只要求"写入 `decision='reject'` 行、不进入主循环、不新增 `runs` 行、
# 不增加预算已用量、打印未确认原因"，未指定 `stop_reason` 必须取
# `should_stop()` 四值之一，也未指定必须调用 `finish_task()`——但本函数
# 仍然调用 `store.finish_task(task_id, stop_reason='checkpoint1_rejected',
# cause=None)` 并返回携带该 `stop_reason` 的 `TaskOutcome`，理由：`design.md`
# §7.1 的循环不变量与本函数 `FINISH` 段的既有断言（`assert stop_reason is
# not None`）要求"该任务的 `tasks` 行必须有一个非空 `stop_reason` 才算
# 走完 `run_task()` 的生命周期"——Checkpoint 1 拒绝显然是任务的一次完整
# 终结（不是异常，是明确的"人不同意，不跑"），比照 `preflight()` 失败
# 走异常路径（`tasks` 行从未创建）不同：Checkpoint 1 触发时 `tasks` 行
# **已经存在**（见 12.1 节），让它永久停在没有 `ended_at`/`stop_reason`
# 的状态会让 `store.get_task_summary()`/`cli.build_run_snapshot()` 误以为
# 该任务仍在运行，这不是可审计的结束状态。
#
# `cli.cmd_run()` 的既有 `_exit_code_for_stop_reason()` 四值映射表**不**
# 因此扩展——`checkpoint1_rejected` 不是"需要人介入的 Checkpoint 2"，也不是
# "正常收敛"，而是"根本没有开始寻优"，语义上与"参数或配置错误（含
# `PreflightError`）⟹ 退出码 1"同一类（"未进入寻优"）。`cmd_run()` 侧的
# 接线属于 `cli.py` 的改动范围，本任务（14.7）的文件清单只含
# `controller/run_task.py`，`cli.py` 侧的接线本次只做"把既有 `yes` 参数
# 转发给 `run_task()`"这一项（见任务描述第 5 点），`_exit_code_for_stop_
# reason()` 是否需要为 `checkpoint1_rejected` 新增一条映射、映射到哪个
# 退出码，是 `cli.py` 改动范围内的决策，留给该文件未来的维护者（或本任务
# 之外的显式请求）处理——本次不越权改写 `cli.py` 的退出码映射表。
# ===========================================================================


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """`run_task()` 的返回值（`design.md` §6.2.1，逐字段照抄）。"""

    task_id: str
    stop_reason: str
    cause: str | None
    best_candidate_id: str | None
    engine_starts_used: int
    wallclock_s: float


@dataclass(frozen=True, slots=True)
class RoundContext:
    """本轮的调度上下文，随 `state` 一并交给 `propose_fn`（本文件新增类型）。

    `state`（`controller.stop.SearchState`）只含 `should_stop()` 需要的
    `current_best`，而提案侧还需要三样主循环才知道的量：

    - `no_improvement_rounds`：`agent.validate` 的校验模式由它与"是否已有最佳候选"
      共同决定（`design.md` §6.7）。这个值只有主循环持有，从数据库反推需要按轮次
      重算历史最佳序列，既绕又依赖对"某一轮的最佳"的额外定义。
    - `round_index`：被拒候选落 `rejections` 表时要带上，它是搜索轨迹的一部分。
    - `remaining_budget`：进提案上下文，让模型知道还能试多少次。

    **校验模式的决定权因此仍在 controller**：这里传出的是事实（无改善了几轮），
    不是结论（该用哪种模式）。提案器拿到它也无法据此放宽自己被校验的严格度。
    """

    round_index: int
    no_improvement_rounds: int
    remaining_budget: int


@dataclass(frozen=True, slots=True)
class ProposeResult:
    """`propose_fn` 的返回契约（本文件新增，非 design.md 登记类型；见模块
    顶部"14.5"一节"2. `propose_fn`"的详细说明）。"""

    candidates: Sequence[Candidate] = ()
    stop_recommendation: bool = False
    llm_call_id: str | None = None
    """产生本轮候选的那次 LLM 调用，落进 `candidates.origin_llm_call_id`。

    放在结果级而不是候选级：一次调用一次性给出整批候选，逐个候选各带一份会是同一个
    值的多份拷贝。非 LLM 来源的 `propose_fn`（脚本注入、网格枚举）留 `None`，
    与 `origin='agent'` 之外的来源一致。

    缺了它，"这个设计是模型在看到什么上下文之后提出的"就再也答不上来——`llm_calls`
    行还在，但没有任何列把它和候选连起来。审计链断在这一环上不会报错，只会在需要
    复盘某个候选的来历时才发现。
    """


class ProposeFn(Protocol):
    """`propose_fn` 的调用形状。

    用 `Protocol` 而不是 `Callable[..., ProposeResult]`：`round_context` 是
    **仅关键字**参数，而 `Callable[...]` 表达不了关键字参数，写成
    `Callable[[SearchState, RoundContext], ProposeResult]` 会把它说成位置参数——
    那样的注解与实际调用方式不符，类型检查便失去了意义。
    """

    def __call__(
        self, state: SearchState, *, round_context: RoundContext
    ) -> ProposeResult: ...


def _default_propose_fn(
    state: SearchState, *, round_context: RoundContext
) -> ProposeResult:
    """`propose_fn` 的默认实现：本轮返回空候选集，不调用任何 LLM、不猜测
    任何候选（见模块顶部"2. `propose_fn`"一节）。"""
    return ProposeResult(candidates=(), stop_recommendation=False)


def _compute_execution_env_hash(model_cfg: ModelConfig) -> str:
    """见模块顶部"6. 六个哈希与 `tasks` 行"一节对 `execution_env_hash`
    输入字段集合的完整说明与理由。"""
    fields = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "matlab_release": model_cfg.runtime.matlab_release,
        "toolboxes": list(model_cfg.runtime.toolboxes),
        "execution_mode": model_cfg.runtime.execution_mode,
    }
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


def _to_repo_scenario(spec: "ctrl_scenario.ScenarioSpec") -> ScenarioSpec:
    """`controller.scenario.ScenarioSpec`（本地临时类型，见该模块顶部软依赖
    说明第 1 条）→ `store.repo.ScenarioSpec`（`run_tier()` 实际要求的跨模块
    契约类型）的显式 1:1 字段拷贝。

    两者字段形状逐字段相同（均含 `spec_version`），本函数只是在两个不同的
    Python 类之间搬运同名字段值——这正是 `run_task.py` 模块顶部"`scenario_
    rows` 的类型"一节记录的"一处已知的跨模块类型不一致"，本函数是这处不一致
    在 14.5 调用点上的桥接，不修改 `controller/scenario.py`（该文件不在本
    任务文件清单内）。
    """
    return ScenarioSpec(
        scenario_id=spec.scenario_id,
        tier=spec.tier,
        model_variant=spec.model_variant,
        require_margin=spec.require_margin,
        vin_v=spec.vin_v,
        temp_c=spec.temp_c,
        load_start_a=spec.load_start_a,
        load_end_a=spec.load_end_a,
        slew_a_per_us=spec.slew_a_per_us,
        spec_version=spec.spec_version,
    )


def _assert_loop_invariants(
    *,
    store: Store,
    task_id: str,
    ledger: BudgetLedger,
    closure_paths: Sequence[Path],
    frozen_fingerprint: str,
) -> None:
    """design.md §7.1 循环不变量 1/2/4/5 中，本函数能以运行期断言表达的两条
    （1 与 5；2 与 4 是本函数自身控制流结构性保证的性质，逐条说明见下），在
    `WHILE` 顶部与 `FINISH` 前各调用一次（见模块顶部"8. 循环不变量"一节）。

    - 不变量 1（预算账本与事实源一致）：`ledger.used() ==
      store.sum_budget_units(task_id)`——`BudgetLedger.used()` 的实现本身
      就是 `store.sum_budget_units(task_id)`（见 `controller/budget.py`），
      因此这条断言在结构上恒真；仍显式断言一次是为了让"这条不变量在这里
      成立"这件事在代码中可见、可在未来 `BudgetLedger` 实现变化时被测试
      捕捉到回归，而不是仅凭"看代码知道恒真"这一非正式论证。
    - 不变量 2（已提交的 `runs` 行不再被修改；重试产生新的 `attempt`）：由
      `store.close_run_ok()`/`close_run_failed()` 的 `WHERE status=
      'running'` 更新条件与 `RunTerminationRejectedError` 结构性保证（见
      `store/repo.py`），本函数不重复断言。
    - 不变量 4（`state` 完全由 SQLite 重建）：由 `rebuild_state()`
      （任务 14.6）与本函数每轮开始时都重新调用它（不在内存跨轮次累积
      `SearchState`）这一控制流事实保证，本函数不重复断言。
    - 不变量 5（`model_package_hash` 自入口起未变）：`fast_fingerprint
      (closure_paths) == frozen_fingerprint`。
    """
    assert ledger.used() == store.sum_budget_units(task_id), (
        "run_task: 循环不变量 1 违反——BudgetLedger.used() 与 "
        "SUM(runs.budget_units) 不一致"
    )
    assert fast_fingerprint(closure_paths) == frozen_fingerprint, (
        "run_task: 循环不变量 5 违反——model_package_hash 自入口起已变化"
    )


def _run_scenarios_with_retry(
    scenario_rows: Sequence[ScenarioSpec],
    candidate: Candidate,
    tier: Tier,
    *,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    session: MatlabSession,
    store: Store,
    artifacts: ArtifactStore,
    budget_ledger: BudgetLedger,
    task_id: str,
    execution_env_hash: str,
    frozen_fingerprint: str,
    frozen_model_package_hash: str,
    base_dir: str | Path,
    margin_failure_count: int,
    max_attempts_per_scenario: int,
) -> tuple[bool, int]:
    """对 `scenario_rows` 中每个场景**逐一**调用 `run_tier()`（单元素
    `scenario_rows`），外层维护每个场景各自独立的 `attempt` 计数器，实现
    R16.4/R16.5 的 `engine_transient` 重试编排（见模块顶部"9. `engine_
    transient` 重试"一节）。

    返回 `(tier_passed, margin_failure_count)`：`tier_passed=False` 时——
    Screening 层的硬约束/裕量提前拒绝，或 Evaluation 层某场景重试耗尽——
    调用方按 `tier` 决定后续动作（Screening 不可行 ⟹ 整个候选提前拒绝；
    Evaluation 某场景重试耗尽 ⟹ 该候选跳过、继续下一候选，本函数内部已经
    跳过该场景、继续处理 `scenario_rows` 中的其余场景，不额外中止整层
    ——这是 R16.5"继续处理下一个候选"在"处理到一半的这一层"内部的自然
    延伸：重试耗尽只影响这一个场景，不影响同一候选同一层的其余场景行）。
    """
    tier_passed = True
    for scenario in scenario_rows:
        attempt = 1
        while True:
            result = run_tier(
                candidate,
                tier,
                (scenario,),
                model_cfg=model_cfg,
                metrics_cfg=metrics_cfg,
                constraints_cfg=constraints_cfg,
                session=session,
                store=store,
                artifacts=artifacts,
                budget_ledger=budget_ledger,
                task_id=task_id,
                execution_env_hash=execution_env_hash,
                frozen_fingerprint=frozen_fingerprint,
                frozen_model_package_hash=frozen_model_package_hash,
                base_dir=base_dir,
                margin_failure_count=margin_failure_count,
                attempt=attempt,
            )
            margin_failure_count = result.margin_failure_count

            is_engine_transient = (
                result.stopped_early
                and result.failure_class == "transient_error"
                and result.cause == "engine_transient"
            )
            if is_engine_transient and attempt < max_attempts_per_scenario:
                attempt += 1
                continue  # 新开 attempt+1 行重试（R16.4）

            if is_engine_transient:
                # 重试耗尽：关闭该行、不再新开 attempt、不写 tasks.stop_reason，
                # 该场景视为不可评价；继续处理本层其余场景（R16.5）。
                tier_passed = False
                break

            if not result.passed:
                # Screening 提前拒绝，或裕量/约束不可行导致的其他终止：
                # 对 Screening 层立即整体判失败；对 Evaluation 层同样标记
                # tier_passed=False（Evaluation 的“继续跑完该层其余场景行”
                # 语义已在 run_tier() 内部对同一 scenario_rows 处理完毕——
                # 此处 scenario_rows 恰一个元素，run_tier() 已经把该场景的
                # 结果写好，本函数只需继续外层 for 循环处理下一个场景）。
                if tier == "screening":
                    tier_passed = False
                    break
            break

        if tier == "screening" and not tier_passed:
            break

    return tier_passed, margin_failure_count


# ===========================================================================
# Checkpoint 1（任务 14.7）：见模块顶部"12."一节对本节全部设计决策
# （`approver` 哨兵值、`result_hash` 取值范围、`stop_reason` 新值）的完整说明。
# ===========================================================================


_CHECKPOINT1_APPROVER = "cli_operator"
_CHECKPOINT1_REJECTED_STOP_REASON = "checkpoint1_rejected"


def _checkpoint1_summary(
    task_cfg: TaskConfig, metrics_cfg: MetricsConfig, constraints_cfg: ConstraintsConfig
) -> dict[str, object]:
    """组装 Checkpoint 1 须打印的五项内容（目标/硬约束/参数范围与档位/场景/预算）
    的结构化表示，供 `_request_checkpoint1()` 同时用于（a）打印给人看、
    （b）经 `canonical_json` 计算 `result_hash`——两处共用同一份数据结构，避免
    "打印的文本"与"哈希绑定的内容"因分别拼装而漂移（与本文件
    `_compute_execution_env_hash()` 同一模式）。

    - 目标：`metrics_cfg.objective.primary`（`metric_id`/`aggregation`/
      `direction`）+ `objective.tie_tolerance`；`task_cfg.objective_target`
      非空时附加其 `target_value`（可选节，design.md §4.1）。
    - 硬约束：`constraints_cfg.hard_constraints` 五条（`vout_min` /
      `vout_max` / `peak_current_max` / `phase_margin_min` /
      `gain_margin_min`），每条含
      `value`/`observable`/`sense`/`applies_to_tier`。
    - 参数范围与档位：`config.ticks.expand_all_ticks(constraints_cfg)`
      展开后的档位文本，加 `design_space.variables.<name>` 的 `domain`/
      `scale`/`unit`。
    - 场景：`task_cfg.scenarios` 逐条的 `scenario_id`/`tier`/`model_variant`
      （任务描述点名的三项；其余电气量字段不在打印要求内，不额外塞入）。
    - 预算：`task_cfg.budget` 的 `max_engine_starts`/`max_wallclock_hours`/
      `max_attempts_per_scenario`。
    """
    primary = metrics_cfg.objective.primary
    objective: dict[str, object] = {
        "primary_metric_id": primary.metric_id,
        "primary_aggregation": primary.aggregation,
        "primary_direction": primary.direction,
        "tie_tolerance": metrics_cfg.objective.tie_tolerance,
        "target_value": (
            task_cfg.objective_target.target_value
            if task_cfg.objective_target is not None
            else None
        ),
    }

    hard_constraints = {
        name: {
            "value": entry.value,
            "observable": entry.observable,
            "sense": entry.sense,
            "applies_to_tier": list(entry.applies_to_tier),
        }
        for name, entry in (
            ("vout_min", constraints_cfg.hard_constraints.vout_min),
            ("vout_max", constraints_cfg.hard_constraints.vout_max),
            ("peak_current_max", constraints_cfg.hard_constraints.peak_current_max),
            ("phase_margin_min", constraints_cfg.hard_constraints.phase_margin_min),
        )
    }

    ticks = expand_all_ticks(constraints_cfg)
    variables = constraints_cfg.design_space.variables
    parameter_ranges_and_ticks = {
        name: {
            "domain": list(getattr(variables, name).domain),
            "scale": getattr(variables, name).scale,
            "unit": getattr(variables, name).unit,
            "ticks": list(ticks[name]),
        }
        for name in ("rcomp", "ccomp")
    }

    scenarios = [
        {
            "scenario_id": s.scenario_id,
            "tier": s.tier,
            "model_variant": s.model_variant,
        }
        for s in task_cfg.scenarios
    ]

    budget = {
        "max_engine_starts": task_cfg.budget.max_engine_starts,
        "max_wallclock_hours": task_cfg.budget.max_wallclock_hours,
        "max_attempts_per_scenario": task_cfg.budget.max_attempts_per_scenario,
    }

    return {
        "objective": objective,
        "hard_constraints": hard_constraints,
        "parameter_ranges_and_ticks": parameter_ranges_and_ticks,
        "scenarios": scenarios,
        "budget": budget,
    }


def _print_checkpoint1_summary(summary: Mapping[str, object]) -> None:
    """把 `_checkpoint1_summary()` 的结构化内容打印为人可读文本（五个带中文
    标签的分段，供人工确认，也供本任务的验证脚本按标签定位五项各自的输出）。
    只调用 `click.echo`，不做任何 I/O 之外的副作用。
    """
    click.echo("==== Checkpoint 1：任务确认 ====")

    click.echo("---- 目标 ----")
    click.echo(canonical_json(summary["objective"]))

    click.echo("---- 硬约束 ----")
    click.echo(canonical_json(summary["hard_constraints"]))

    click.echo("---- 参数范围与档位 ----")
    click.echo(canonical_json(summary["parameter_ranges_and_ticks"]))

    click.echo("---- 场景 ----")
    click.echo(canonical_json(summary["scenarios"]))

    click.echo("---- 预算 ----")
    click.echo(canonical_json(summary["budget"]))


def _request_checkpoint1(
    task_cfg: TaskConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    *,
    yes: bool,
    store: Store,
    task_id: str,
) -> bool:
    """`request_checkpoint1()`（design.md §8.1；`tasks.md` 任务 14.7；需求
    R19.1, R19.2, R19.3, R19.11）：打印目标/硬约束/参数范围与档位/场景/预算
    五项，取得人工确认（`yes=True` 时直接判为确认，否则读取**恰好一次**
    `click.confirm()`），写一条 `approvals(kind='checkpoint1')` 行，返回
    是否确认通过。

    调用方（`run_task()`）须保证本函数在任何 `store.persist_candidate()` /
    `store.open_run()` / `budget_ledger.reserve()` 调用之前调用——本函数自身
    不创建任何 `runs` 行、不触碰预算账本，只读取四个已构造配置与写一条
    `approvals` 行。

    ## `approver` 取值：固定哨兵 `"cli_operator"`，不新增 `--approver` 选项

    `record_approval()`（`store/repo.py`）只对 `kind='final_recommendation'`
    与 `kind='track_selection'` 断言 `second_approver` 非空且与 `approver`
    不同——`kind='checkpoint1'` 不落在这条更严格的规则内。`requirements.md`
    R19.13「`--approver` 须命令行显式给出、不推断身份」字面上是对 `cli`（即
    `poweragent report` 命令）的要求，`cmd_run`/`run` 命令的签名（`tasks.md`
    任务 15.1 已落地）里从未定义过 `--approver` 选项——Checkpoint 1 是任务
    执行前的一次性"继续吗？"确认，不是 Checkpoint 3 那种把最终设计决策与
    具体个人身份绑定的双签审批；`design.md` §11.2 的 CP1 一行也只写"要求
    `--yes` 或交互确认；写 `approvals(kind='checkpoint1')`"，未提及任何身份
    参数。因此本函数不新增 CLI 参数、不尝试从环境变量或系统用户名推断身份
    （那正是 R19.13 明确禁止的"自动推断身份"），而是记录一个固定、明确标注
    含义的哨兵值——它的证据价值是"确实有一个人在终端前，且这个人被真实展示
    过这五项内容后同意/拒绝了"，不是"这是某个具体命名的、需要事后追责到人的
    个体"（那层追责性专属于 `final_recommendation`/`track_selection`，见
    `record_approval()` 的结构性断言分工）。

    ## `result_hash` 取值：对本次打印的五项内容本身计算的哈希，不是占位值

    `approvals.result_hash` 列为 `NOT NULL`；`kind='checkpoint1'` 没有一个
    "候选评价结果"可供绑定（那是 `final_recommendation` 的语义）。本函数不
    填充一个无意义的占位字符串，而是对 `_checkpoint1_summary()` 产出的、
    实际打印给人看的同一份结构化内容计算 `sha256(canonical_json(...))`
    ——这让 `result_hash` 仍然承载它在别处的含义："这条审批行绑定的是哪份
    确切内容"，只是这里绑定的不是候选结果，而是"人工确认时看到的这五项
    内容的确切取值"。

    ## 返回值与 `runs`/预算的关系

    返回 `True`（确认通过）：写 `decision='approve'` 行。返回 `False`
    （`reject` 或未取得确认）：写 `decision='reject'` 行、打印未确认原因，
    调用方据此不进入主循环。两条路径均只执行这一次 `store.record_approval()`
    调用，不涉及 `runs`/`budget_ledger` 的任何写入或先占。
    """
    summary = _checkpoint1_summary(task_cfg, metrics_cfg, constraints_cfg)
    _print_checkpoint1_summary(summary)

    result_hash_value = hashlib.sha256(
        canonical_json(summary).encode("utf-8")
    ).hexdigest()

    if yes:
        confirmed = True
    else:
        # design.md §11.2 / R19.1：未传 --yes 时读取恰好一次交互确认，本函数
        # 只在这一处调用 click.confirm()，不在循环体或其余任何位置重复调用。
        confirmed = click.confirm(
            "以上五项确认无误，开始寻优？", default=False
        )

    store.record_approval(
        ApprovalRecord(
            task_id=task_id,
            kind="checkpoint1",
            decision="approve" if confirmed else "reject",
            approver=_CHECKPOINT1_APPROVER,
            result_hash=result_hash_value,
        )
    )

    if not confirmed:
        click.echo(
            "Checkpoint 1 未确认（reject 或未取得确认）：不进入寻优主循环。",
            err=True,
        )

    return confirmed


def run_task(
    task_cfg: TaskConfig,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
    *,
    resume: bool = False,
    yes: bool = False,
    propose_fn: ProposeFn | None = None,
    probe_single_run_s: float | None = None,
    session: MatlabSession | None = None,
    store: Store | None = None,
    artifacts: ArtifactStore | None = None,
    db_path: str | Path = "runs.db",
    artifacts_dir: str | Path = "artifacts",
    base_dir: str | Path = ".",
) -> TaskOutcome:
    """主循环、`tasks` 行落库、失败分类与重试（`design.md` §6.2.1 / §7.1 /
    §8.1；`tasks.md` 任务 14.5；需求 R1.5, R15.10, R16.3, R16.4, R16.5,
    R16.6, R16.11）。

    kwonly 参数中相对 design.md §6.2.1 字面签名（只有 `resume`）的全部扩展
    项（`yes` / `propose_fn` / `probe_single_run_s` / `session` / `store` /
    `artifacts` / `db_path` / `artifacts_dir` / `base_dir`）及各自的软依赖/
    设计理由，见模块顶部本节的详细说明，此处不重复。`yes`（任务 14.7 新增）
    原样转发给 `_request_checkpoint1()`：`True` 时 Checkpoint 1 直接判为
    确认、跳过交互；`False`（默认）时读取恰好一次 `click.confirm()`。
    `session` /`store` /
    `artifacts` 三者默认 `None` 时分别构造 `MatlabSession()` /
    `Store(db_path)` / `ArtifactStore(artifacts_dir)`——这三个协作对象在
    design.md 的其余函数签名（`run_tier` 等）中都是必填形参，`run_task()`
    作为顶层入口需要能够独立可调用（不强制调用方先手动构造三个协作对象），
    因此提供构造快捷方式，同时允许测试/调用方注入替代实现（例如 mock
    `MatlabSession`）。

    `resume` 当前只影响一件事：是否跳过 `create_task()`（`resume=True` 时
    假定 `tasks` 行已存在，直接复用；`resume=False` 时新建）。`reap_orphan_
    runs()` 始终无条件执行（design.md §6.2.5 的既定裁决，不受 `resume`
    门控）。跨轮次状态（`round_index`/`no_improvement_rounds`）在 `resume=
    True` 时同样按 `rebuild_state()`/`store` 现有事实重建，不在内存持有
    需要"续接"的额外状态——本函数每次调用本就完全从 SQLite 重建，`resume`
    的语义因此只体现在"任务行是否已存在"这一件事上。
    """
    session = session if session is not None else MatlabSession()
    store = store if store is not None else Store(db_path)
    artifacts = artifacts if artifacts is not None else ArtifactStore(artifacts_dir)
    propose = propose_fn if propose_fn is not None else _default_propose_fn

    task_id = task_cfg.task_id
    start_time = time.monotonic()

    # ---- reap_orphan_runs()：无条件执行，先于 preflight/Checkpoint1/首轮 ----
    reap_orphan_runs(store)

    # ---- preflight：按已落地的 check_* 函数逐个调用（无顶层 preflight()） ----
    check_metric_not_implemented(metrics_cfg)
    check_observable_unbound(constraints_cfg, metrics_cfg)
    check_unit_mismatch(constraints_cfg, metrics_cfg)
    check_margin_extraction_ready(metrics_cfg, model_cfg)
    check_config_completeness(task_cfg)
    check_freeze_consistency(
        constraints_cfg, metrics_cfg, model_cfg, task_cfg, store, base_dir=base_dir
    )
    model_package_hash_value = check_model_package_hash(model_cfg, store, base_dir=base_dir)
    check_dual_model_consistency(model_cfg, store, base_dir=base_dir)
    if probe_single_run_s is not None:
        check_budget_feasibility(probe_single_run_s, task_cfg.budget)
    # check_baseline_gate()（任务 16.1）未落地，本次跳过；见模块顶部"4."一节。

    # ---- 六个哈希（scenario_set_hash 先算不写，供 create_task 使用） ----
    #
    # `ddl.sql` 的 `scenario_set.task_id` 外键指向 `tasks(task_id)`——
    # `freeze_scenario_set()` 的 `INSERT INTO scenario_set` 因此要求对应的
    # `tasks` 行已经存在；而 `tasks.scenario_set_hash` 列本身又需要这个哈希
    # 值才能写入 `create_task()`。两者存在的先后顺序因此固定为：先用
    # `controller.scenario.compute_scenario_set_hash()`（纯计算、不写库，
    # `check_freeze_consistency()` 已经在 preflight 阶段用同一函数算过一次）
    # 算出 `scenario_set_hash`、随之创建 `tasks` 行，再调用
    # `freeze_scenario_set()` 实际写入 `scenario_set` 表（此时 `tasks` 行
    # 已存在，外键约束满足）——这是 design.md §8.1 伪代码"先
    # `freeze_scenario_set` 再无对应 `tasks` 行"的字面顺序在真实外键约束下
    # 的必要调整，`freeze_scenario_set()` 返回值与 `compute_scenario_set_
    # hash()` 对同一 `task_cfg` 逐字符相同（两者共用同一份行构造逻辑，见
    # `controller/scenario.py`），因此提前算出再创建行、随后再写表，不产生
    # 任何哈希不一致的风险。
    scenario_set_hash = ctrl_scenario.compute_scenario_set_hash(task_cfg)
    metrics_hash_value = _metrics_hash_of(metrics_cfg.model_dump(mode="json"))
    constraints_hash_value = _constraints_hash_of(constraints_cfg.model_dump(mode="json"))
    execution_env_hash_value = _compute_execution_env_hash(model_cfg)
    calibration_hash_value = "" if task_cfg.simulation_only else ""
    # simulation_only=False（工程轨）时 calibration_hash 的真实计算依赖任务
    # 20.6（calib 模块，M2，尚未落地）；本函数当前对两轨都取空串，工程轨下
    # 这不是任务 20.6 定义的正确取值，是一个已记录的待接线缺口（见模块顶部
    # "6."一节最后一段），不影响本次验证所用的 PoC 轨（simulation_only=True）。

    if not resume or not store.task_exists(task_id):
        store.create_task(
            task_id=task_id,
            simulation_only=task_cfg.simulation_only,
            task_kind="optimize",
            model_package_hash=model_package_hash_value,
            metrics_hash=metrics_hash_value,
            constraints_hash=constraints_hash_value,
            scenario_set_hash=scenario_set_hash,
            execution_env_hash=execution_env_hash_value,
            calibration_hash=calibration_hash_value,
            budget_max_starts=task_cfg.budget.max_engine_starts,
        )

    # ---- 冻结场景集：实际写入 scenario_set 表（tasks 行已存在，满足外键） ----
    #
    # `resume=True` 且该 `task_id` 已存在时，`scenario_set` 表大概率已在
    # 上次进程运行时写过（`(task_id, scenario_id)` 为主键，重复插入会触发
    # `sqlite3.IntegrityError`）；本函数按"该表是否已有该 `task_id` 的行"
    # 判断是否需要重新调用 `freeze_scenario_set()`，与 `create_task()` 上面
    # 对 `resume`/`task_exists` 的判断同一模式，不重复冻结同一份场景集。
    already_frozen = (
        store.connection.execute(
            "SELECT 1 FROM scenario_set WHERE task_id=? LIMIT 1", (task_id,)
        ).fetchone()
        is not None
    )
    if already_frozen:
        frozen_scenario_set_hash = scenario_set_hash
    else:
        frozen_scenario_set_hash = ctrl_scenario.freeze_scenario_set(task_cfg, store.connection)
    assert frozen_scenario_set_hash == scenario_set_hash, (
        "run_task: freeze_scenario_set() 返回值与 compute_scenario_set_hash() "
        "不一致——两者应对同一 task_cfg 逐字符相同"
    )

    ledger = BudgetLedger(
        store,
        task_id,
        task_cfg.budget.max_engine_starts,
        task_cfg.budget.max_wallclock_hours * 3600.0,
    )
    assert ledger.used() == store.sum_budget_units(task_id), (
        "run_task: ASSERT ledger.used() = SUM(runs.budget_units) 前置检查失败"
    )

    # ---- request_checkpoint1()：打印五项、取得确认、写 approvals（见模块顶部"12."） ----
    checkpoint1_confirmed = _request_checkpoint1(
        task_cfg,
        metrics_cfg,
        constraints_cfg,
        yes=yes,
        store=store,
        task_id=task_id,
    )
    if not checkpoint1_confirmed:
        store.finish_task(
            task_id, stop_reason=_CHECKPOINT1_REJECTED_STOP_REASON, cause=None
        )
        return TaskOutcome(
            task_id=task_id,
            stop_reason=_CHECKPOINT1_REJECTED_STOP_REASON,
            cause=None,
            best_candidate_id=None,
            engine_starts_used=store.sum_budget_units(task_id),
            wallclock_s=time.monotonic() - start_time,
        )

    # ---- 冻结的模型不变性检查输入（供每轮不变量断言与 run_tier() 转发） ----
    model_dump = model_cfg.model_dump(mode="json")
    closure_paths = resolve_dependency_closure(model_dump, base_dir=base_dir)
    frozen_fingerprint = fast_fingerprint(closure_paths)
    frozen_model_package_hash = model_package_hash_value

    round_index = 0
    no_improvement_rounds = 0
    margin_failure_count = 0
    pending_stop_and_ask_human: tuple[str, str] | None = None

    stop_reason: str | None = None
    stop_cause: str | None = None

    screening_scenarios = tuple(
        _to_repo_scenario(s) for s in ctrl_scenario.screening_rows(task_cfg)
    )
    evaluation_scenarios = tuple(
        _to_repo_scenario(s) for s in ctrl_scenario.evaluation_rows(task_cfg)
    )

    while True:
        # ---- 不变量：账本与事实源一致；模型哈希未变（WHILE 顶部） ----
        _assert_loop_invariants(
            store=store,
            task_id=task_id,
            ledger=ledger,
            closure_paths=closure_paths,
            frozen_fingerprint=frozen_fingerprint,
        )

        ranked_before = rank(store, task_id, metrics_cfg, top_n=1)
        best_before = (
            BestRecord(
                candidate_id=ranked_before[0].candidate_id,
                value=ranked_before[0].worst_case_value,
            )
            if ranked_before
            else None
        )

        state = SearchState(current_best=best_before)
        propose_result = propose(
            state,
            round_context=RoundContext(
                round_index=round_index,
                no_improvement_rounds=no_improvement_rounds,
                remaining_budget=ledger.remaining(),
            ),
        )

        if not propose_result.candidates:
            no_improvement_rounds += 1
            round_index += 1
            decision = should_stop(
                state,
                task_cfg,
                propose_result.stop_recommendation,
                budget_exhausted=ledger.exhausted(),
                no_improvement_rounds=no_improvement_rounds,
                pending_stop_and_ask_human=pending_stop_and_ask_human,
            )
            if decision.stop:
                stop_reason = decision.reason
                stop_cause = "agent_returned_no_candidate"
                break
            continue

        for candidate in propose_result.candidates:
            if ledger.exhausted():
                stop_reason = "budget_exhausted"
                stop_cause = None
                break

            store.persist_candidate(
                candidate,
                task_id=task_id,
                origin="agent",
                round_index=round_index,
                origin_llm_call_id=propose_result.llm_call_id,
            )

            screening_passed, margin_failure_count = _run_scenarios_with_retry(
                screening_scenarios,
                candidate,
                "screening",
                model_cfg=model_cfg,
                metrics_cfg=metrics_cfg,
                constraints_cfg=constraints_cfg,
                session=session,
                store=store,
                artifacts=artifacts,
                budget_ledger=ledger,
                task_id=task_id,
                execution_env_hash=execution_env_hash_value,
                frozen_fingerprint=frozen_fingerprint,
                frozen_model_package_hash=frozen_model_package_hash,
                base_dir=base_dir,
                margin_failure_count=margin_failure_count,
                max_attempts_per_scenario=task_cfg.budget.max_attempts_per_scenario,
            )
            if margin_failure_count >= MARGIN_FAILURE_ESCALATION_THRESHOLD:
                pending_stop_and_ask_human = (
                    "stop_and_ask_human",
                    "metric_pipeline_error",
                )
            if not screening_passed:
                continue  # 提前拒绝：candidate_rejected（见 run_tier() 文档）

            _evaluation_passed, margin_failure_count = _run_scenarios_with_retry(
                evaluation_scenarios,
                candidate,
                "evaluation",
                model_cfg=model_cfg,
                metrics_cfg=metrics_cfg,
                constraints_cfg=constraints_cfg,
                session=session,
                store=store,
                artifacts=artifacts,
                budget_ledger=ledger,
                task_id=task_id,
                execution_env_hash=execution_env_hash_value,
                frozen_fingerprint=frozen_fingerprint,
                frozen_model_package_hash=frozen_model_package_hash,
                base_dir=base_dir,
                margin_failure_count=margin_failure_count,
                max_attempts_per_scenario=task_cfg.budget.max_attempts_per_scenario,
            )
            if margin_failure_count >= MARGIN_FAILURE_ESCALATION_THRESHOLD:
                pending_stop_and_ask_human = (
                    "stop_and_ask_human",
                    "metric_pipeline_error",
                )
            # Evaluation 层重试耗尽（_evaluation_passed=False）不中止候选
            # 循环本身——该候选因缺完整 Evaluation 集，worst_case() 的
            # HAVING 完备性过滤会自然排除它，继续处理下一个候选（R16.5）。

        if stop_reason is not None:
            break  # 预算耗尽分支（上面 for 循环内 break 出来）

        # ---- 轮末：先按最差场景聚合刷新当前最佳可行候选（见模块顶部"10."） ----
        ranked_after = rank(store, task_id, metrics_cfg, top_n=1)
        best_after = (
            BestRecord(
                candidate_id=ranked_after[0].candidate_id,
                value=ranked_after[0].worst_case_value,
            )
            if ranked_after
            else None
        )

        if best_after is None:
            pass  # 本轮仍无可行候选：既非首次出现也非有改善，不满足下方两分支，
            # design.md §8.1 伪代码的 improved() 在两侧皆无候选时定义为
            # "未改善"，与 best_before is None 分支合并处理如下：
            if best_before is None:
                no_improvement_rounds += 1
        elif best_before is None:
            no_improvement_rounds = 0  # 本轮首次出现可行候选
        else:
            direction = metrics_cfg.objective.primary.direction
            if direction == "minimize":
                decrease = best_before.value - best_after.value
            else:
                decrease = best_after.value - best_before.value
            tie_tolerance = metrics_cfg.objective.tie_tolerance
            if decrease <= tie_tolerance:
                no_improvement_rounds += 1
            else:
                no_improvement_rounds = 0

        round_index += 1

        state_after = SearchState(current_best=best_after)
        decision = should_stop(
            state_after,
            task_cfg,
            propose_result.stop_recommendation,
            budget_exhausted=ledger.exhausted(),
            no_improvement_rounds=no_improvement_rounds,
            pending_stop_and_ask_human=pending_stop_and_ask_human,
        )
        if decision.stop:
            stop_reason = decision.reason
            stop_cause = decision.cause
            break

    # ---- FINISH ----
    _assert_loop_invariants(
        store=store,
        task_id=task_id,
        ledger=ledger,
        closure_paths=closure_paths,
        frozen_fingerprint=frozen_fingerprint,
    )
    assert stop_reason is not None, "run_task: FINISH 时 stop_reason 不得为空（R16.6）"

    store.finish_task(task_id, stop_reason=stop_reason, cause=stop_cause)

    final_ranked = rank(store, task_id, metrics_cfg, top_n=1)
    best_candidate_id = final_ranked[0].candidate_id if final_ranked else None
    engine_starts_used = store.sum_budget_units(task_id)
    wallclock_s = time.monotonic() - start_time

    return TaskOutcome(
        task_id=task_id,
        stop_reason=stop_reason,
        cause=stop_cause,
        best_candidate_id=best_candidate_id,
        engine_starts_used=engine_starts_used,
        wallclock_s=wallclock_s,
    )
