"""poweragent/store/repo.py

跨模块数据契约（`design.md` §5.2）与 `Store` 仓储层（`design.md` §6.9，`tasks.md`
T6 / 任务 8.3）。本模块是全系统读写 `runs` / `candidates` / `metric_results` /
`constraint_results` / `approvals` / `llm_calls` / `interventions` / `evidence` /
`freezes` / `baselines` / `rejections` 十一张事实表的仓储实现（`tasks` 表由本模块
读取但不在本任务写入其生命周期字段，那是 `controller/run_task.py`，任务 14.5，的
职责）。

## 范围边界（需求 R14.9）

本模块**只按调用方传入值原样写回** `constraint_results.feasible`、
`metric_results.valid`、`runs.failure_class`、`runs.cause`、`tasks.stop_reason`
五项，不读取 `metrics.yaml` / `constraints.yaml` 的任何阈值，不提供排序候选、
判定可行性、判定停止或签发审批的代码路径。`record_approval` 与 `close_run_*`
里的检查是**结构性/完整性断言**（六列终态不可覆盖、双签署名不得相同、四元组
唯一键不得重复插入），不是领域决策逻辑——这与任务描述「不提供判定路径」的例外
显式对齐。

## `cause` 取值集合（`design.md` §11.1，Literal 枚举承载）

失败分类恰四类（`FailureClass`），`cause` 取值集合按分类分别登记为
`CandidateRejectedCause` / `TransientErrorCause` / `StopAndAskHumanCause` 三个
`Literal` 别名（`budget_exhausted` 无合法 `cause` 取值，登记为「—」）。其中
`metric_invalid:<metric_id>`、`out_of_domain:<name>`、`off_tick:<name>`、
`low_novelty:<距离>` 四项在设计登记表中是**动态后缀**取值，`Literal` 无法穷举
这类字符串，因此运行期校验（`_is_legal_cause`）对 `candidate_rejected` 类额外
按固定前缀做 `startswith` 匹配；固定取值仍以 `Literal` 承载，新增固定取值需改
本模块代码（`_CANDIDATE_REJECTED_FIXED` 等 frozenset 常量），不读配置。

本轮变更：**删** `retrieval_insufficient`（检索为空已是合法状态，无触发条件，
保留即死代码）、**增** `artifact_missing`（归 `transient_error`：`simulation_key`
命中但产物缺失或往返校验失败时标记，产物丢失是存储层问题、与候选可行性无关）。

## `freeze_baseline` 的「不覆盖」语义：幂等 no-op，而非拒绝并抛异常

任务描述列出了两种候选实现（no-op 或 raise），指出这是需要显式记录的选择。本
实现选择**幂等 no-op**：同一 `gate_key` 的重复冻结请求直接返回、不报错、不修改
已存在的行。理由——Baseline Gate 的结论以 `gate_key` 缓存查询（`find_baseline`
命中即跳过重新仿真，`design.md` §6.2.2），意味着 `check_baseline_gate` 在重复调用
（例如进程重启后重新执行 `preflight`）时会对同一个已冻结的 `gate_key` 再次判定
「可行」并再次尝试冻结。若把重复冻结实现为抛异常，`preflight` 每次重跑都需要额外
分支去吞掉这个异常，才能保持「命中缓存即跳过」的语义；实现为 no-op 则调用方完全
不需要关心这层，`freeze_baseline` 与 `find_baseline` 的组合天然幂等。这与
`open_run` 的「重复插入必须拒绝」形成对比是刻意的：`runs` 的四元组唯一键重复
意味着**同一份仿真被要求跑两次**（预算与产物都会重复消耗，必须挡住）；而
`baselines` 的 `gate_key` 重复意味着**同一个已冻结的结论被再次确认**（不产生
任何新的仿真或预算副作用，允许静默通过）。

## `freeze()` 的「拒绝重复」语义 与 `freeze_baseline()` 的「幂等 no-op」语义为何不同

`freeze()`（任务 5.5，`freezes` 表读写接口）与上面 `freeze_baseline()` 都面对
「同一个键的第二次写入请求」，但选择相反：`freeze_baseline()` no-op、`freeze()`
拒绝并抛 `FreezeAlreadyExistsError`。区别在于键背后的语义——`gate_key` 重复
意味着「同一个已冻结的结论被再次确认」（不产生任何新副作用，允许静默通过，见
上文）；而 `freezes.kind`（`model_package` / `safety` / `task_set`）代表「这份
配置状态从此刻起被锁定」，是整个 M0/M5 流程里的单向事件。若对同一个 `kind` 的
第二次冻结请求（哪怕携带的 `hash` 与首次相同）也悄悄放行，一旦携带的 `hash`
其实**不同**（例如模型包在两次冻结之间被人改动过），no-op 会把这次悄悄的
目标漂移彻底吞掉——`preflight()` 后续比对到的仍是第一次冻结的 `hash`，但没人
知道配置已经变了。这必须在写入时就暴露为错误，而不是留给下一次 `preflight()`
去猜。因此 `freeze()` 是**写一次即锁**，第二次调用无论 `hash` 是否相同都拒绝。

`update_safety_freeze_metrics_hash()` 是这条「写一次即锁」规则里唯一被显式挖
出的例外——`design.md` §4.5 / requirements.md R3.8 明确允许 `kind='safety'` 行
的 `hash` 在 M1 出口前因补齐 `compare_tolerance` 而更新恰好一次。这个例外没有
做成 `freeze()` 的一个参数（例如 `allow_overwrite=True`），而是做成一个独立
方法，理由：`freeze()` 的调用点不应该需要知道「除了 safety 之外都不能覆盖，
safety 在特定条件下可以覆盖一次」这种依 `kind` 分支的特例逻辑；把例外收进
一个命名清楚、自带「只能用一次」互锁的独立方法，`freeze()` 本身可以保持对
全部三个 `kind` 完全一致、无分支的「写一次即锁」语义。

## `freeze_task_set()`：委托给 `freeze()` 的薄封装，不写独立实现（任务 19.1）

`kind='task_set'` 与 `update_safety_freeze_metrics_hash()` 挖出的
`kind='safety'` 例外不同——`requirements.md` R21.2 只要求「定稿时把
`task_set.md` 的 sha256 写入 `freezes(kind='task_set')`，此后不修改该文件」，
没有类似 `compare_tolerance` 补齐那样的「允许更新一次」窗口。`kind='task_set'`
与 `kind='model_package'`/`'safety'` 因此共享同一条约束——「至多一行、写一次
即锁、第二次调用无论 `hash` 是否相同都拒绝」——`freeze()` 已经完整承载这条
约束（`kind` 参数本身就是 `FreezeKind` 三值之一，`freezes.kind` 主键与
`freeze()` 内部的存在性检查对三个 `kind` 无差别生效）。`freeze_task_set()`
因此不重新实现「至多一行」的判断或 SQL 语句，只做「读文件、算 sha256、调用
`self.freeze('task_set', hash=...)`」三件事，是一个纯粹的薄封装。

重复调用 `freeze_task_set()`（例如 `task_set.md` 被误改后再次调用）与直接
调用 `freeze('task_set', ...)` 两次触发同一个 `FreezeAlreadyExistsError`，
不静默吞掉、不做成 `freeze_baseline()` 那种幂等 no-op——理由与「`freeze()`
的『拒绝重复』语义」一节对 `model_package`/`safety` 的论证完全适用于
`task_set`：`kind='task_set'` 代表「这份任务集文档从此刻起被锁定」，是单向
事件，第二次写入请求即便携带的 `hash` 与首次相同也不该被静默放行——若
`task_set.md` 在两次冻结之间被人改动过，no-op 会把这次目标漂移悄悄吞掉。

`detail` 留空（`freeze()` 的默认值 `None`）：R21.2 要求人工在文件内加注定稿
日期，但该日期与 `frozen_at` 的一致性「不对……做机器校验（由人工核对）」
（同一条 AC 原文），`freeze_task_set()` 没有需要写入 `detail` 列的结构化
内容，不替 R21.2 未要求的机器校验发明一个落点。

`update_safety_freeze_metrics_hash()` 的「只能用一次」由 `freezes.detail` 列
承载的**标记约定**实现：更新发生后，`detail` 列的文本以固定前缀
`metrics_hash_updated_at_m1_exit` 打头（见 `_METRICS_HASH_UPDATE_MARKER`
常量），后跟调用方传入的描述文本。方法在执行更新前检查现有 `detail` 是否已
包含该标记——已包含则拒绝（抛 `SafetyFreezeUpdateWindowClosedError`）、且
`hash`/`detail`/`frozen_at` 三列原样保持不变；未包含则更新 `hash` 并把
`detail` 覆写为「标记 + 本次描述」。该标记字符串是纯文本约定（不是新增列、不
是新表），选择「固定前缀」而非「精确等于」是为了让调用方仍能在 `detail` 里
附加自由文本描述（例如具体改了哪个字段），同时保持标记本身可用简单的子串
匹配（`in`）识别、便于人工用 `grep` 或 `SELECT ... WHERE detail LIKE '%...%'`
直接定位。

**职责边界（本模块 vs. 调用方）**：`freeze()` / `update_safety_freeze_metrics_hash()`
都只做机械层面的「至多一次」/「恰好一次」约束，不做语义校验：

- `freeze(kind='safety', hash=...)` 的 `hash` 计算——把 `constraints_hash`、
  `metrics_hash`、`scenario_set_hash` 三个具名值组成映射、经 `canonical_json`
  序列化后取 sha256——由调用方（`controller/preflight.py`，任务 14.2）完成；
  本模块不知道、也不需要知道这三个值各自代表什么，只接收调用方算好的
  `hash` 字符串原样落盘。
- `update_safety_freeze_metrics_hash()` 不校验「当前确实处于 M1 出口前」、也
  不校验「这次变更确实只涉及 `compare_tolerance` 字段」——这两条时序/字段范围
  判断需要读取里程碑状态与新旧 `metrics.yaml` 内容的差异，本模块的方法签名
  里没有这些输入，也不应该有：判断权属于调用方（`controller/preflight.py`
  或触发该次更新的配置校验路径）。本方法只回答一个机械问题——「这个
  `kind='safety'` 行的 `hash` 此前是否已经被这个专用通道改过一次」——回答
  「是」就拒绝，回答「否」就放行并落下标记，仅此而已。

## `candidates` 表的写入不在本任务范围内

`store/ddl.sql` 的 `runs.candidate_id` 外键指向 `candidates(candidate_id)`
（`PRAGMA foreign_keys=ON`），但 `design.md` §6.9 给出的 `Store` 接口列表中不含
写入 `candidates` 行的方法；算法伪代码（§8.1）里的 `persist_candidate(...)` 调用
发生在 `open_run(...)` 之前、属于 `controller/run_task.py`（任务 14.4/14.5）的
编排职责。本模块因此不提供 `candidates` 写入方法——调用方必须在调用 `open_run`
前确保对应的 `candidates` 行已存在，否则 `open_run` 的 `INSERT INTO runs` 会因
外键约束失败而抛 `sqlite3.IntegrityError`（本模块不特殊包装该失败路径，因为它
与「四元组已存在」是两类不同的完整性失败）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, Sequence

from poweragent.config.hashing import canonical_json
from poweragent.store import db

__all__ = [
    "Tier",
    "ModelVariant",
    "FailureClass",
    "CandidateRejectedCause",
    "TransientErrorCause",
    "StopAndAskHumanCause",
    "Cause",
    "Candidate",
    "ScenarioSpec",
    "SimulationResult",
    "MetricResult",
    "Violation",
    "ConstraintResult",
    "CachedEvaluation",
    "LlmCallRecord",
    "ApprovalRecord",
    "BaselineGateStatus",
    "FreezeStatus",
    "TaskSummary",
    "RunAlreadyExistsError",
    "RunTerminationRejectedError",
    "CauseValidationError",
    "ApprovalValidationError",
    "GrantBudgetError",
    "FreezeKind",
    "FreezeAlreadyExistsError",
    "SafetyFreezeMissingError",
    "SafetyFreezeUpdateWindowClosedError",
    "InterventionValidationError",
    "WORST_CASE_SQL",
    "Store",
]


# ===========================================================================
# design.md §5.2：跨模块数据契约（frozen dataclass，全 SI 单位，逐字段照抄）
# ===========================================================================

Tier = Literal["screening", "evaluation", "robustness"]
ModelVariant = Literal["switching", "averaged"]
FailureClass = Literal[
    "candidate_rejected", "transient_error", "stop_and_ask_human", "budget_exhausted"
]


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    parameters_si: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    scenario_id: str
    tier: Tier
    model_variant: ModelVariant
    require_margin: bool
    vin_v: float
    temp_c: float
    load_start_a: float
    load_end_a: float
    slew_a_per_us: float
    spec_version: str


@dataclass(frozen=True, slots=True)
class SimulationResult:
    run_id: str
    status: Literal["ok", "diverged", "solver_error", "timeout", "engine_transient"]
    waveform_ref: str | None
    observable_ref: str | None  # 含裕量原始频响数据的引用
    elapsed_ms: int
    engine_starts: int  # 本次真实启动 Simulink 的次数（含裕量额外启动）


@dataclass(frozen=True, slots=True)
class MetricResult:
    run_id: str
    metric_id: str
    value: float | None
    valid: bool
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Violation:
    constraint: str
    limit: float
    actual: float | None
    unit: str


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    candidate_id: str
    scenario_id: str
    run_id: str
    feasible: bool
    violations: Sequence[Violation]


# ===========================================================================
# 支撑类型：design.md §6.9 的 Store 接口引用了这些类型名但未在别处给出完整字段，
# 按对应 SQLite 表列与调用点用途最小化定义（非 §5.2 登记范围，故不加入其 frozen
# dataclass 清单，但同样 frozen + slots 以保持一致风格）。
# ===========================================================================


@dataclass(frozen=True, slots=True)
class CachedEvaluation:
    """`find_cached_evaluation()` 的返回类型：`evaluation_key` 命中缓存时的完整
    评价结果（产物引用 + 指标 + 约束判定），供调用方直接复用、不重算指标、不重判
    约束（`design.md` §5.4 / §6.2.3）。"""

    waveform_ref: str | None
    observable_ref: str | None
    metrics: Sequence[MetricResult]
    constraint_result: ConstraintResult


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """`log_llm_call()` 的输入，对应 `llm_calls` 表六字段 + `evidence_ids`
    （`design.md` §5.1）。`llm_call_id` 与 `created_at` 由 `log_llm_call()` 生成，
    不在本记录中提供。"""

    task_id: str
    role: str
    model_id: str
    prompt_hash: str
    context_hash: str
    tokens: int
    outcome: Literal["ok", "schema_invalid", "repaired", "empty"]
    evidence_ids: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """`record_approval()` 的输入，对应 `approvals` 表（`design.md` §5.1）。
    `approval_id` 与 `created_at` 由 `record_approval()` 生成，不在本记录中提供。"""

    task_id: str
    kind: str
    decision: Literal["approve", "reject"]
    approver: str
    result_hash: str
    candidate_id: str | None = None
    second_approver: str | None = None
    extra_units: int | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class BaselineGateStatus:
    """`find_baseline()` 的返回类型，逐列对应 `baselines` 表（`design.md` §5.1）。

    `design.md` §6.2.2 另有一个同名但字段不同的 `BaselineGateStatus`
    （含 `evidence_bound: bool`），那是 `controller.preflight.check_baseline_gate()`
    的返回类型——`evidence_bound` 取自 `model.yaml` 的 `measured_evidence_ref`，
    不是 `baselines` 表的列，因此不属于本仓储层的重建范围；两者同名不同形是
    "controller 组合 store 读到的数据与 model.yaml 派生的数据" 的自然结果，不是
    本模块的疏漏。
    """

    gate_key: str
    task_id: str
    passed: bool
    frozen_result_hash: str | None
    diagnosis_ref: str | None
    frozen_at: str


FreezeKind = Literal["model_package", "safety", "task_set"]


@dataclass(frozen=True, slots=True)
class FreezeStatus:
    """`find_freeze()` 的返回类型，逐列对应 `freezes` 表（`design.md` §4.5 /
    `ddl.sql`）。"""

    kind: FreezeKind
    hash: str
    detail: str | None
    frozen_at: str


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """`get_task_summary()` 的返回类型：`tasks` 行里供 `cli.build_run_snapshot()`
    （`tasks.md` 任务 15.2）读取的四个展示字段。这是一个刻意缩小的只读投影
    （只含调用方需要的四列），不是 `tasks` 表的完整镶像——`tasks` 表还有
    `model_package_hash` / `budget_max_starts` 等字段，但 `format_status()` 的
    单行状态输出不需要它们，同「小而窄的 Store 读方法」既有惯例（`task_exists`
    / `sum_budget_units` 等）保持一致，不多返回调用方不需要的列。"""

    simulation_only: bool
    started_at: str
    stop_reason: str | None
    cause: str | None


# ===========================================================================
# 异常
# ===========================================================================


class RunAlreadyExistsError(RuntimeError):
    """`open_run()` 以已存在的 `(task_id, candidate_id, scenario_id, attempt)`
    四元组插入时抛出；所在事务已回滚，既有行内容不变。"""


class RunTerminationRejectedError(RuntimeError):
    """`close_run_ok()` / `close_run_failed()` 的 `UPDATE ... WHERE status='running'`
    影响行数为 0 时抛出（该 `run_id` 不存在，或已处于终态）；所在事务已回滚，
    该行的六列（`status` `failure_class` `cause` `waveform_ref` `observable_ref`
    `ended_at`）保持首次终结时的取值不变。"""


class CauseValidationError(ValueError):
    """`close_run_failed()` 收到的 `cause` 不在给定 `FailureClass` 的登记取值
    集合内时抛出（`design.md` §11.1）；抛出前不执行任何写入。"""


class ApprovalValidationError(ValueError):
    """`record_approval()` 的结构性断言不满足时抛出：`kind='final_recommendation'`
    而 `second_approver` 为空或与 `approver` 相同，或 `kind` 属于必填五/四列集合
    而任一必填列为空。抛出前不执行任何写入、不产生审批行。"""


class GrantBudgetError(RuntimeError):
    """`grant_budget()` 找不到匹配的 `approvals` 行（`approval_id` 与 `task_id`
    不匹配）时抛出；所在事务已回滚。"""


class FreezeAlreadyExistsError(RuntimeError):
    """`freeze()` 对已存在一行的 `kind` 再次调用时抛出（`freezes.kind` 主键本身
    也会在这种情况下触发 `sqlite3.IntegrityError`，但本方法在触发数据库层异常
    之前就先查行是否存在并抛出这个更具体的异常，见模块顶部「`freeze()` 的『拒绝
    重复』语义」一节）；抛出前不执行任何写入，既有行的 `hash`/`detail`/
    `frozen_at` 三列不变。"""


class SafetyFreezeMissingError(RuntimeError):
    """`update_safety_freeze_metrics_hash()` 在不存在 `kind='safety'` 行时调用
    时抛出（不能更新一个从未冻结过的行）；抛出前不执行任何写入。"""


class SafetyFreezeUpdateWindowClosedError(RuntimeError):
    """`update_safety_freeze_metrics_hash()` 在 `kind='safety'` 行的 `detail`
    已记录过一次更新（`_METRICS_HASH_UPDATE_MARKER` 已出现在现有 `detail` 中）
    时抛出；抛出前不执行任何写入，`hash`/`detail`/`frozen_at` 三列保持首次
    更新（或从未更新）时的取值完全不变。"""


class InterventionValidationError(ValueError):
    """`log_intervention()` 收到的 `reason` 长度不在 1~500 字符（非空文本）
    区间内时抛出；抛出前不执行任何写入。

    `checkpoint` 越域交由 `ddl.sql` 的 `CHECK (checkpoint IN ('cp1','cp2','cp3',
    'other'))` 在写入时以 `sqlite3.IntegrityError` 拒绝（见 `log_intervention()`
    文档），本异常只覆盖该 `CHECK` 约束无法表达的 `reason` 长度校验。
    `cli.cmd_log()` 此前（任务 15.1）已有同一条校验并转译为 `click.UsageError`
    （面向 CLI 用户的错误文案与退出码），但那只是**这一条约束当前唯一的调用
    路径**上的校验，不是这条约束本身的权威强制点——`log_intervention()` 是
    `interventions` 表事实上的唯一写入方法，任何未来绕过 `cmd_log()` 直接持有
    `Store` 实例调用本方法的调用方（例如 `controller/` 里尚未落地的埋点路径）
    都不应因此绕开这条长度约束。本异常把校验收进仓储层、在写入前拒绝，使其
    对全部调用方一致生效，而不是依赖每个调用方各自重复实现同一条检查
    （`tasks.md` 任务 15.3 / `requirements.md` R20.4）。"""


# ===========================================================================
# cause 取值登记（design.md §11.1）：三个 Literal 别名 + 校验函数
# ===========================================================================

CandidateRejectedCause = Literal[
    "constraint_violation",
    "unstable",
    "solver_error",
    "timeout",
    "diverged",
    "duplicate",
    "unit_or_dimension",
    "key_mismatch",
    # 以下四项为动态后缀取值，Literal 无法穷举，运行期以固定前缀 + startswith 承载
    # （见 _CANDIDATE_REJECTED_PREFIXES）：
    #   "metric_invalid:<metric_id>", "out_of_domain:<name>",
    #   "off_tick:<name>", "low_novelty:<距离>"
]
TransientErrorCause = Literal["engine_transient", "process_restart", "artifact_missing"]
StopAndAskHumanCause = Literal[
    "metric_pipeline_error",
    "model_systemic_error",
    "model_structure_suspected",
    "no_feasible_region",
    "baseline_infeasible",
    "parallel_nondeterminism",
    "margin_extraction_unreliable",
    "dual_model_inconsistent",
    "model_mutated_during_optimize",
]
# budget_exhausted 登记为「—」：本类无合法 cause 取值，close_run_failed 对该类的
# 任何 cause 输入一律拒绝。
Cause = CandidateRejectedCause | TransientErrorCause | StopAndAskHumanCause

_CANDIDATE_REJECTED_FIXED: frozenset[str] = frozenset(
    {
        "constraint_violation",
        "unstable",
        "solver_error",
        "timeout",
        "diverged",
        "duplicate",
        "unit_or_dimension",
        "key_mismatch",
    }
)
_CANDIDATE_REJECTED_PREFIXES: tuple[str, ...] = (
    "metric_invalid:",
    "out_of_domain:",
    "off_tick:",
    "low_novelty:",
)
_TRANSIENT_ERROR_CAUSES: frozenset[str] = frozenset(
    {"engine_transient", "process_restart", "artifact_missing"}
)
_STOP_AND_ASK_HUMAN_CAUSES: frozenset[str] = frozenset(
    {
        "metric_pipeline_error",
        "model_systemic_error",
        "model_structure_suspected",
        "no_feasible_region",
        "baseline_infeasible",
        "parallel_nondeterminism",
        "margin_extraction_unreliable",
        "dual_model_inconsistent",
        "model_mutated_during_optimize",
    }
)
_BUDGET_EXHAUSTED_CAUSES: frozenset[str] = frozenset()  # 登记为「—」


def _is_legal_cause(cls: FailureClass, cause: str) -> bool:
    if cls == "candidate_rejected":
        return cause in _CANDIDATE_REJECTED_FIXED or cause.startswith(
            _CANDIDATE_REJECTED_PREFIXES
        )
    if cls == "transient_error":
        return cause in _TRANSIENT_ERROR_CAUSES
    if cls == "stop_and_ask_human":
        return cause in _STOP_AND_ASK_HUMAN_CAUSES
    if cls == "budget_exhausted":
        return cause in _BUDGET_EXHAUSTED_CAUSES
    return False


def _validate_cause(cls: FailureClass, cause: str) -> None:
    if not _is_legal_cause(cls, cause):
        raise CauseValidationError(
            f"close_run_failed: cause={cause!r} is not a legal value for "
            f"failure_class={cls!r} (design.md §11.1 registry)"
        )


# ===========================================================================
# 小工具：ID 生成、UTC ISO 8601 秒级时间戳、violations 的 JSON 编解码
# ===========================================================================


# ===========================================================================
# `freezes(kind='safety').detail` 的更新窗口标记约定（design.md §4.5 / R3.8 /
# R3.12）：更新发生后 `detail` 以该前缀打头，`update_safety_freeze_metrics_hash`
# 靠子串匹配识别「是否已用掉这唯一一次窗口」，人工也可用同一字符串直接 grep。
# ===========================================================================

_METRICS_HASH_UPDATE_MARKER = "metrics_hash_updated_at_m1_exit"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _violation_to_dict(v: Violation) -> dict[str, object]:
    return {"constraint": v.constraint, "limit": v.limit, "actual": v.actual, "unit": v.unit}


def _violation_from_json(text: str) -> list[Violation]:
    import json

    return [
        Violation(
            constraint=str(d["constraint"]),
            limit=float(d["limit"]),
            actual=None if d["actual"] is None else float(d["actual"]),
            unit=str(d["unit"]),
        )
        for d in json.loads(text)
    ]


# ===========================================================================
# worst-case 聚合的唯一判定 SQL（`design.md` §5.4；`tasks.md` 任务 12.2；需求
# 追溯 R9.5, R9.6, R9.7, R9.12；属性 CP-6）
#
# 与 `design.md` §5.4 逐字符相同（仅顶部注释路径已按本文件实际落点调整），供
# `eval/aggregate.py` 的 `WORST_CASE_SQL` 常量原样复用（该模块从本模块 import
# 此常量，而不是重新抄写一份，避免两处文本漂移）。`HAVING COUNT(DISTINCT
# scenario_id) = (SELECT COUNT(*) FROM eval_set)` 是「任一 Evaluation 场景缺失
# 即不可行」的全部实现——本模块不在 Python 侧对该结果做任何补全或再聚合。
# ===========================================================================

WORST_CASE_SQL = """
-- poweragent/eval/aggregate.py: WORST_CASE_SQL
WITH eval_set AS (
  SELECT scenario_id FROM scenario_set
   WHERE task_id = :task_id AND tier = 'evaluation'
),
ok_rows AS (
  SELECT r.run_id, r.candidate_id, r.scenario_id
    FROM runs r JOIN eval_set e USING (scenario_id)
   WHERE r.task_id = :task_id AND r.status = 'done'
),
feasible_rows AS (
  SELECT o.* FROM ok_rows o
    JOIN constraint_results cr ON cr.run_id = o.run_id
   WHERE cr.feasible = 1
),
primary_vals AS (
  SELECT f.candidate_id, f.scenario_id, m.value
    FROM feasible_rows f
    JOIN metric_results m ON m.run_id = f.run_id
   WHERE m.metric_id = :primary_metric AND m.valid = 1
)
SELECT candidate_id,
       MAX(value)                  AS worst_case,
       COUNT(DISTINCT scenario_id) AS n_ok
  FROM primary_vals
 GROUP BY candidate_id
HAVING COUNT(DISTINCT scenario_id) = (SELECT COUNT(*) FROM eval_set);
"""


# ===========================================================================
# Store
# ===========================================================================


class Store:
    """SQLite 唯一事实源的仓储层（`design.md` §6.9）。

    `__init__` 复用 `store/db.py`（任务 8.1）的连接助手：`db_path` 指向的文件已
    存在时用 `db.connect()` 打开（回读 PRAGMA、不重跑 DDL）；不存在时用
    `db.init_db()` 从 `ddl.sql` 建库。本类不重复实现 PRAGMA 设置或 DDL 执行逻辑。
    """

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if path.exists():
            self._conn = db.connect(path)
        else:
            self._conn = db.init_db(path)

    def tx(self) -> AbstractContextManager[sqlite3.Connection]:
        """`BEGIN IMMEDIATE`；正常退出提交，异常回滚并原样传播（委托 `db.tx()`）。"""
        return db.tx(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        """底层 `sqlite3.Connection`（任务 14.5 新增，最小暴露）。

        `controller/scenario.py` 的 `freeze_scenario_set(task_cfg, conn)`
        （任务 14.1）签名接受裸 `sqlite3.Connection`，其模块 docstring 明确
        标注这是"`Store` 落地前的临时应对"，"一旦 `Store` 落地，签名应改为
        接受 `Store`"——但截至本任务（14.5）撰写时该函数签名尚未跟进这项
        既定的后续清理（`controller/scenario.py` 不在本任务文件清单内，改动
        它属于对另一个已完成任务的返工，见本任务文件顶部对同一类型不一致
        问题的处理原则）。`run_task()` 需要调用 `freeze_scenario_set()` 但
        只持有一个 `Store` 实例，因此本属性提供最小的桥接：暴露 `Store`
        已经持有的连接，供 `run_task()` 传给 `freeze_scenario_set(task_cfg,
        store.connection)`。不新增任何写入路径、不绕过 `tx()` 的事务边界——
        调用方仍需自行用 `poweragent.store.db.tx()` 或等价方式管理事务。
        """
        return self._conn

    def task_exists(self, task_id: str) -> bool:
        """`tasks` 表中是否存在该 `task_id`。

        供 `cli`（任务 15.1）校验 `--task` 指向的 `task_id` 是否存在——`report` /
        `log` 两个子命令均要求该值在拒绝路径（未定义子命令 / 缺失必需参数 /
        `--checkpoint` 越域 / `--task` 指向不存在的 `task_id`）触发时不写任何
        数据库行；本方法是只读查询，不开事务，与 `find_cached_simulation()` 等
        既有只读方法风格一致。
        """
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE task_id=? LIMIT 1", (task_id,)
        ).fetchone()
        return row is not None

    def get_task_summary(self, task_id: str) -> TaskSummary | None:
        """按 `task_id` 读取 `tasks` 行的四个展示字段
        （`simulation_only` / `started_at` / `stop_reason` / `cause`）；无命中
        返回 `None`。

        供 `cli.build_run_snapshot()`（任务 15.2）组装 `RunSnapshot` 使用；
        本方法是只读投影查询，不开事务，与 `task_exists()` 等既有只读方法
        风格一致（见 `TaskSummary` 类文档的范围说明）。
        """
        row = self._conn.execute(
            "SELECT simulation_only, started_at, stop_reason, cause "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        simulation_only, started_at, stop_reason, cause = row
        return TaskSummary(
            simulation_only=bool(simulation_only),
            started_at=started_at,
            stop_reason=stop_reason,
            cause=cause,
        )

    def create_task(
        self,
        *,
        task_id: str,
        simulation_only: bool,
        task_kind: str,
        model_package_hash: str,
        metrics_hash: str,
        constraints_hash: str,
        scenario_set_hash: str,
        execution_env_hash: str,
        calibration_hash: str,
        budget_max_starts: int,
    ) -> None:
        """写一条 `tasks` 行（`design.md` §5.1；`tasks.md` 任务 14.5；需求
        R1.5）：`simulation_only`、`task_kind`、六个哈希中的五个（末项
        `calibration_hash` 由调用方按 PoC/工程轨自行算好后原样传入，本方法
        不区分两轨）、`budget_max_starts`。`started_at` 由本方法生成
        （`_now_iso()`，与全模块既有写入方法同一时间戳格式），不接受调用方
        传入——`started_at` 是"这条 `tasks` 行第一次被创建"这一事实本身的
        时间戳，没有理由由调用方代传一个可能与之不同的值。`ended_at` /
        `stop_reason` / `cause` 三列保持 `NULL`（`ddl.sql` 允许 `NULL`），
        由 `finish_task()` 在任务结束时一次性写入。

        `design.md` §6.9 的 `Store` 接口列表未登记本方法——`tasks` 行的创建
        与生命周期字段写入此前一直划给 `run_task()`（任务 14.5）自己决定
        落点；`store/repo.py` 当前没有任何方法可以完成"写一条 `tasks` 行"这
        件事（`ddl.sql` 的 `tasks` 表存在，但仓储层从未提供写入方法）。本
        方法是任务 14.5 范围内对 `store/repo.py` 的最小必要补充：只新增
        "写一行、按主键防重复插入"这一件事，不涉及任何领域判定（不校验
        `budget_max_starts` 是否合理、不校验哈希格式），与本文件"只按调用方
        传入值原样写回"的既定范围原则一致。

        同一 `task_id` 重复调用：`tasks.task_id` 主键约束使第二次 `INSERT`
        抛 `sqlite3.IntegrityError`；本方法不捕获、不转译——`task_id` 重复
        意味着调用方两次为同一个任务标识发起了"任务开始"，这是调用方的用法
        错误（例如误把 `resume` 场景当成"从零开始"来调用本方法），不是需要
        静默吞掉或转译为专用异常的正常路径。
        """
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(task_id, simulation_only, task_kind, model_package_hash, "
                " metrics_hash, constraints_hash, scenario_set_hash, "
                " execution_env_hash, calibration_hash, budget_max_starts, "
                " started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    int(simulation_only),
                    task_kind,
                    model_package_hash,
                    metrics_hash,
                    constraints_hash,
                    scenario_set_hash,
                    execution_env_hash,
                    calibration_hash,
                    budget_max_starts,
                    _now_iso(),
                ),
            )

    def finish_task(
        self, task_id: str, *, stop_reason: str, cause: str | None
    ) -> None:
        """把 `stop_reason` / `cause` / 非空 `ended_at` 写入该 `task_id` 对应
        的 `tasks` 行（`design.md` §7.1 / §8.1 `FINISH` 段；`tasks.md` 任务
        14.5；需求 R16.6）。

        本方法只做一次无条件 `UPDATE`（`tasks.task_id` 为主键，至多影响一
        行）；`task_id` 不存在时 `UPDATE` 影响 0 行，本方法不因此报错——
        "调用方传入了一个不存在的 `task_id`"与"该 `task_id` 存在但重复调用
        本方法"两者都不是本方法需要区分的情形（`run_task()` 在其自身控制流
        中只会对自己刚 `create_task()` 过的 `task_id` 调用一次本方法，不存在
        需要本方法自我防御的重复终结场景）。
        """
        with self.tx() as conn:
            conn.execute(
                "UPDATE tasks SET stop_reason=?, cause=?, ended_at=? "
                "WHERE task_id=?",
                (stop_reason, cause, _now_iso(), task_id),
            )

    def persist_candidate(
        self,
        candidate: Candidate,
        *,
        task_id: str,
        origin: Literal["agent", "baseline", "dense_grid", "manual"],
        round_index: int | None = None,
        origin_llm_call_id: str | None = None,
    ) -> None:
        """写一条 `candidates` 行（`design.md` §8.1 伪代码的 `persist_candidate(...)`
        调用；`tasks.md` 任务 14.5；模块顶部"`candidates` 表的写入不在本任务
        范围内"一节已指明该调用发生在 `open_run()` 之前、属于
        `controller/run_task.py`（任务 14.4/14.5）的编排职责——本方法是任务
        14.5 落地这一编排职责时对 `Store` 的最小必要补充，理由与
        `create_task()` / `finish_task()` 相同：仓储层此前没有任何方法可以
        完成"写一条 `candidates` 行"这件事）。

        `INSERT OR IGNORE`（幂等）：同一 `candidate_id` 被同一任务内多轮
        `propose()` 重复提出时（例如某个候选生成源在不同轮次返回了相同的
        `parameters_si`），第二次调用视为对同一份不可变候选记录的重复确认，
        不覆盖已有行、不报错——与本文件 `freeze_baseline()` 的既有幂等 no-op
        选择同源（候选内容本身按 `candidate_id = sha256(canonical_json(
        parameters_si))[:16]` 派生，同一 `candidate_id` 不可能对应不同的
        `parameters_si`，因此"忽略重复插入"不会丢失任何信息）。
        """
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO candidates "
                "(candidate_id, task_id, parameters_si, origin, "
                " origin_llm_call_id, round_index, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    task_id,
                    canonical_json(dict(candidate.parameters_si)),
                    origin,
                    origin_llm_call_id,
                    round_index,
                    _now_iso(),
                ),
            )

    def count_interventions_by_checkpoint(self, task_id: str) -> Mapping[str, int]:
        """按 `checkpoint` 分组统计该 `task_id` 的 `interventions` 行数，返回
        `{'cp1': N, 'cp2': N, 'cp3': N, 'other': N}`——四个键恒定存在（`ddl.sql`
        的 `CHECK (checkpoint IN ('cp1','cp2','cp3','other'))` 已把取值域封闭为
        这四项），未出现的 `checkpoint` 取 0，不省略键。

        供 `cli.build_run_snapshot()`（任务 15.2）计算「`cp1+cp3` 合计」与
        「`cp2` 合计」两个计数；本方法只做分组计数，`cp1+cp3` 的合并留给调用方
        （合并方式是展示口径，不是本方法该决定的存储层语义）。
        """
        counts: dict[str, int] = {"cp1": 0, "cp2": 0, "cp3": 0, "other": 0}
        for checkpoint, n in self._conn.execute(
            "SELECT checkpoint, COUNT(*) FROM interventions "
            "WHERE task_id=? GROUP BY checkpoint",
            (task_id,),
        ):
            counts[checkpoint] = n
        return counts

    def sum_llm_tokens(self, task_id: str) -> int:
        """按 `task_id` 过滤的 `SUM(llm_calls.tokens)`，无匹配行时取 0
        （`COALESCE`），与 `sum_budget_units()` 同一模式。

        供 `cli.build_run_snapshot()`（任务 15.2）计算状态行的 `llm_tokens`
        字段——`tokens` 口径固定为 prompt 与 completion token 数之和（见
        `LlmCallRecord` 文档），本方法原样求和、不做任何单价换算（成本核算已
        从状态行删除，见 `tasks.md` 任务 15.2）。
        """
        (total,) = self._conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM llm_calls WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return int(total)

    # ------------------------------------------------------------------
    # 幂等写入
    # ------------------------------------------------------------------

    def open_run(
        self,
        *,
        task_id: str,
        candidate: Candidate,
        scenario: ScenarioSpec,
        attempt: int,
        simulation_key: str,
        budget_units: int,
        cache_hit: bool = False,
    ) -> str:
        """插入一条 `status='running'` 的 `runs` 行，返回新生成的 `run_id`。

        `(task_id, candidate_id, scenario_id, attempt)` 四元组已存在时，`ddl.sql`
        的 `UNIQUE` 约束使 `INSERT` 抛 `sqlite3.IntegrityError`；本方法捕获该异常、
        改抛 `RunAlreadyExistsError`，异常在 `tx()` 上下文内向外传播触发回滚，既有
        行内容不变（需求 R14.12）。

        调用前置条件：`candidate.candidate_id` 对应的 `candidates` 行已存在
        （外键约束，见模块顶部说明）——本方法不创建 `candidates` 行。

        `cache_hit`（可选 kwonly，默认 `False`，任务 14.4 新增）：写入 `ddl.sql`
        既有的 `runs.cache_hit` 列（`INTEGER NOT NULL DEFAULT 0`）。该列此前
        （任务 8.3）在 `INSERT` 语句中未被显式赋值，只能落到 DDL 默认值 `0`——
        `design.md` §8.4 的 `run_tier` 伪代码要求缓存命中路径写入
        `cache_hit := 1`（`evaluation_key` 命中与 `simulation_key` 命中且产物
        完整两条路径皆是），且这一列被 `tasks.md` 任务 9.4 的 Recovery 层测试
        与 CP-4 属性直接断言（`simulation_key` 命中 ⟹ `cache_hit=1`）。缺少
        写入入口意味着这条断言在任务 8.3 落地的接口上无法被满足——本参数是
        task 14.4（`controller/run_task.py` 的 `run_tier()`）落地时补齐的最小
        必要扩展，默认值 `False` 保持对既有调用方（任务 9.x 起的既有测试与
        代码路径，若有）完全向后兼容。
        """
        run_id = _new_id("run")
        now = _now_iso()
        try:
            with self.tx() as conn:
                conn.execute(
                    "INSERT INTO runs "
                    "(run_id, task_id, candidate_id, scenario_id, attempt, status, "
                    " simulation_key, budget_units, cache_hit, started_at) "
                    "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                    (
                        run_id,
                        task_id,
                        candidate.candidate_id,
                        scenario.scenario_id,
                        attempt,
                        simulation_key,
                        budget_units,
                        int(cache_hit),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise RunAlreadyExistsError(
                    f"open_run: (task_id={task_id!r}, "
                    f"candidate_id={candidate.candidate_id!r}, "
                    f"scenario_id={scenario.scenario_id!r}, attempt={attempt}) "
                    "already exists"
                ) from exc
            raise
        return run_id

    def close_run_ok(self, run_id: str, result: SimulationResult) -> None:
        """把 `run_id` 从 `running` 终结为 `done`，写入 `waveform_ref` /
        `observable_ref` / `ended_at`（`failure_class` / `cause` 保持 `NULL`）。

        `UPDATE ... WHERE run_id=? AND status='running'`：影响行数为 0 时该行
        不存在或已处于终态，回滚并抛 `RunTerminationRejectedError`，六列保持
        首次终结取值不变（需求 R14.7）。
        """
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE runs SET status='done', waveform_ref=?, observable_ref=?, "
                "ended_at=? WHERE run_id=? AND status='running'",
                (result.waveform_ref, result.observable_ref, _now_iso(), run_id),
            )
            if cur.rowcount == 0:
                raise RunTerminationRejectedError(
                    f"close_run_ok: run_id={run_id!r} not found or not in "
                    "'running' status"
                )

    def close_run_failed(self, run_id: str, cls: FailureClass, cause: str) -> None:
        """把 `run_id` 从 `running` 终结为 `failed`，写入 `failure_class` /
        `cause` / `ended_at`。`cause` 须是 `cls` 登记表内的合法取值（`design.md`
        §11.1），否则抛 `CauseValidationError`、不执行任何写入。

        `UPDATE ... WHERE run_id=? AND status='running'` 的影响行数为 0 时同
        `close_run_ok`：回滚并抛 `RunTerminationRejectedError`。
        """
        _validate_cause(cls, cause)
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE runs SET status='failed', failure_class=?, cause=?, "
                "ended_at=? WHERE run_id=? AND status='running'",
                (cls, cause, _now_iso(), run_id),
            )
            if cur.rowcount == 0:
                raise RunTerminationRejectedError(
                    f"close_run_failed: run_id={run_id!r} not found or not in "
                    "'running' status"
                )

    def write_evaluation(
        self,
        run_id: str,
        metrics: Sequence[MetricResult],
        cr: ConstraintResult,
        evaluation_key: str,
    ) -> None:
        """写入 `metric_results`（每个 `MetricResult` 一行）与 `constraint_results`
        （`cr` 一行），并把 `evaluation_key` 一并戳到该 `runs` 行的
        `evaluation_key` 列（`ddl.sql` 该列存在于 `runs` / `metric_results` /
        `constraint_results` 三表，三处保持一致）。

        `feasible` / `valid` 按调用方传入值原样写入，不读取任何配置阈值
        （需求 R14.9）。
        """
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET evaluation_key=? WHERE run_id=?",
                (evaluation_key, run_id),
            )
            for m in metrics:
                conn.execute(
                    "INSERT INTO metric_results "
                    "(run_id, metric_id, value, valid, invalid_reason, evaluation_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        m.run_id,
                        m.metric_id,
                        m.value,
                        int(m.valid),
                        m.invalid_reason,
                        evaluation_key,
                    ),
                )
            conn.execute(
                "INSERT INTO constraint_results "
                "(candidate_id, scenario_id, run_id, feasible, violations, evaluation_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    cr.candidate_id,
                    cr.scenario_id,
                    cr.run_id,
                    int(cr.feasible),
                    canonical_json([_violation_to_dict(v) for v in cr.violations]),
                    evaluation_key,
                ),
            )

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def find_cached_simulation(self, simulation_key: str) -> SimulationResult | None:
        """按 `simulation_key` 查找可复用的已完成仿真产物，取最近一条
        `status='done'` 的行；无命中返回 `None`。

        `runs` 表不单独存储 `SimulationResult.status`（`ok`/`diverged`/...）的
        细粒度枚举——只有 `status='done'` 的行才是可复用的成功仿真，因此重建时
        固定回填 `status='ok'`。
        """
        row = self._conn.execute(
            "SELECT run_id, waveform_ref, observable_ref, elapsed_ms, budget_units "
            "FROM runs WHERE simulation_key=? AND status='done' "
            "ORDER BY started_at DESC LIMIT 1",
            (simulation_key,),
        ).fetchone()
        if row is None:
            return None
        run_id, waveform_ref, observable_ref, elapsed_ms, budget_units = row
        return SimulationResult(
            run_id=run_id,
            status="ok",
            waveform_ref=waveform_ref,
            observable_ref=observable_ref,
            elapsed_ms=elapsed_ms or 0,
            engine_starts=budget_units,
        )

    def find_cached_evaluation(self, evaluation_key: str) -> CachedEvaluation | None:
        """按 `evaluation_key` 查找可复用的完整评价结果（指标 + 约束判定）；
        无命中或约束判定缺失返回 `None`。
        """
        run_row = self._conn.execute(
            "SELECT waveform_ref, observable_ref FROM runs "
            "WHERE evaluation_key=? AND status='done' ORDER BY started_at DESC LIMIT 1",
            (evaluation_key,),
        ).fetchone()
        if run_row is None:
            return None
        waveform_ref, observable_ref = run_row

        metrics = [
            MetricResult(
                run_id=r[0],
                metric_id=r[1],
                value=r[2],
                valid=bool(r[3]),
                invalid_reason=r[4],
            )
            for r in self._conn.execute(
                "SELECT run_id, metric_id, value, valid, invalid_reason "
                "FROM metric_results WHERE evaluation_key=?",
                (evaluation_key,),
            )
        ]

        cr_row = self._conn.execute(
            "SELECT candidate_id, scenario_id, run_id, feasible, violations "
            "FROM constraint_results WHERE evaluation_key=?",
            (evaluation_key,),
        ).fetchone()
        if cr_row is None:
            return None
        candidate_id, scenario_id, cr_run_id, feasible, violations_json = cr_row
        cr = ConstraintResult(
            candidate_id=candidate_id,
            scenario_id=scenario_id,
            run_id=cr_run_id,
            feasible=bool(feasible),
            violations=_violation_from_json(violations_json),
        )
        return CachedEvaluation(
            waveform_ref=waveform_ref,
            observable_ref=observable_ref,
            metrics=metrics,
            constraint_result=cr,
        )

    def query_worst_case(
        self, task_id: str, primary_metric: str
    ) -> list[tuple[str, float, int]]:
        """执行 `WORST_CASE_SQL`（`design.md` §5.4），返回满足完备条件的候选
        原始行 `(candidate_id, worst_case, n_ok)`；无候选满足时返回空列表。

        本方法只是对模块级 `WORST_CASE_SQL` 常量的一次带参数执行 + 逐行取值，
        不做任何领域判定——完备性、可行性与有效性判定全部落在 SQL 的
        `HAVING` 子句与三层 CTE 里（见模块顶部注释）。之所以在 `Store` 上开
        这一个专用方法，而不是暴露一个通用的只读 SQL 执行入口：这条 SQL 是
        针对 worst-case 聚合这一件事的多 CTE 复合查询，专用方法能让调用方
        （`eval/aggregate.py` 的 `worst_case()`）不必知道也不必拼装 SQL 文本，
        同时不给其余调用方留下任意执行 SQL 的通道（与本模块「不提供排序/
        可行性/停止/审批判定路径」的既有范围边界一致）。
        """
        cur = self._conn.execute(
            WORST_CASE_SQL, {"task_id": task_id, "primary_metric": primary_metric}
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # 埋点
    # ------------------------------------------------------------------

    def log_llm_call(self, rec: LlmCallRecord) -> str:
        """写一条 `llm_calls` 行，返回生成的 `llm_call_id`。"""
        llm_call_id = _new_id("llm")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO llm_calls "
                "(llm_call_id, task_id, role, model_id, prompt_hash, context_hash, "
                " tokens, outcome, evidence_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    llm_call_id,
                    rec.task_id,
                    rec.role,
                    rec.model_id,
                    rec.prompt_hash,
                    rec.context_hash,
                    rec.tokens,
                    rec.outcome,
                    canonical_json(list(rec.evidence_ids)),
                    _now_iso(),
                ),
            )
        return llm_call_id

    def log_intervention(
        self, task_id: str, checkpoint: str, reason: str, action: str
    ) -> str:
        """写一条 `interventions` 行，返回生成的 `intervention_id`
        （`design.md` §6.9 接口签名逐参数一致；`tasks.md` 任务 15.3；
        需求 R20.4）。

        - `checkpoint` 须落在 `ddl.sql` 的 `CHECK (checkpoint IN ('cp1','cp2',
          'cp3','other'))` 内，不合规值由数据库层的 `CHECK` 约束拒绝
          （`sqlite3.IntegrityError`），本方法不重复实现该校验。「缺省
          `other`」与「`action` 缺省空字符串」由唯一调用方 `cli.cmd_log()`
          （任务 15.1）的 `click.Option` 默认值落地——本方法签名逐参数对应
          `design.md` §6.9 登记的接口，不自行引入默认值。
        - `reason` 须为 1~500 字符的非空文本，不满足时本方法抛
          `InterventionValidationError`、不执行任何写入——这条校验此前
          （任务 15.1）只在 `cli.cmd_log()` 里实现一次；本方法是
          `interventions` 表事实上的唯一写入方法，把同一条校验搬到这里
          （`cmd_log()` 保留原有校验作为面向 CLI 用户的早失败与错误文案，
          两处校验内容完全一致、不冲突）使其对任何未来直接持有 `Store`
          调用本方法的调用方同样生效，理由见 `InterventionValidationError`
          文档。
        - `created_at` 由本方法用 `_now_iso()` 生成（写入时刻系统时钟的
          UTC ISO 8601 秒级时间戳），方法签名中没有任何参数可以代入调用方
          指定的时间戳——`created_at` 不接受调用方输入（需求 R20.4）。
        """
        if not (1 <= len(reason) <= 500):
            raise InterventionValidationError(
                f"log_intervention: reason 长度须在 1~500 字符之间，实际为 {len(reason)}"
            )
        intervention_id = _new_id("intervention")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO interventions "
                "(intervention_id, task_id, checkpoint, reason, action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (intervention_id, task_id, checkpoint, reason, action, _now_iso()),
            )
        return intervention_id

    def record_approval(self, rec: ApprovalRecord) -> str:
        """写一条 `approvals` 行，返回生成的 `approval_id`。

        `kind='final_recommendation'`：断言 `second_approver` 非空且
        `!= approver`；`decision`/`approver`/`second_approver`/`result_hash`/
        `candidate_id` 五列均须非空。
        `kind='track_selection'`：断言 `decision`/`approver`/`second_approver`/
        `result_hash` 四列均须非空。
        任一断言不满足即抛 `ApprovalValidationError`，不执行任何写入、不产生
        审批行。
        """
        if rec.kind == "final_recommendation":
            if not rec.second_approver:
                raise ApprovalValidationError(
                    "record_approval: kind='final_recommendation' requires a "
                    "non-empty second_approver"
                )
            if rec.second_approver == rec.approver:
                raise ApprovalValidationError(
                    "record_approval: kind='final_recommendation' requires "
                    "second_approver != approver"
                )
            if not rec.candidate_id:
                raise ApprovalValidationError(
                    "record_approval: kind='final_recommendation' requires a "
                    "non-empty candidate_id"
                )
        elif rec.kind == "track_selection":
            if not rec.second_approver:
                raise ApprovalValidationError(
                    "record_approval: kind='track_selection' requires a "
                    "non-empty second_approver"
                )

        approval_id = _new_id("approval")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO approvals "
                "(approval_id, task_id, kind, candidate_id, result_hash, approver, "
                " second_approver, decision, extra_units, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    rec.task_id,
                    rec.kind,
                    rec.candidate_id,
                    rec.result_hash,
                    rec.approver,
                    rec.second_approver,
                    rec.decision,
                    rec.extra_units,
                    rec.note,
                    _now_iso(),
                ),
            )
        return approval_id

    # ------------------------------------------------------------------
    # 本轮新增：两张支撑表（rejections / baselines）+ approvals.extra_units
    # ------------------------------------------------------------------

    def record_rejection(
        self,
        *,
        task_id: str,
        round_index: int,
        llm_call_id: str | None,
        raw_parameters: Mapping[str, float],
        reason: str,
    ) -> str:
        """写一条 `rejections` 行，返回生成的 `rejection_id`。

        `raw_parameters` 按 `design.md` §5.3.1 的 `canonical_json` 规则序列化
        后落 `raw_parameters` 列（该列语义为"原始取值的规范化 JSON"，`ddl.sql`
        注释）。`reason` 取值同 `candidate_rejected` 校验类 `cause`——本方法不
        对 `reason` 做取值校验：`agent.validate`（任务 24）产生的拒绝原因字符串
        （如 `key_mismatch`、`off_tick:<name>`）本身就落在
        `_CANDIDATE_REJECTED_FIXED` / `_CANDIDATE_REJECTED_PREFIXES` 的登记表内，
        重复校验是对同一份登记表的第二次断言，不产生额外保障；`record_rejection`
        的职责是"如实落库"，取值合规性由产生 `reason` 的那一端（`validate()`）
        保证。
        """
        rejection_id = _new_id("rejection")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO rejections "
                "(rejection_id, task_id, round_index, llm_call_id, raw_parameters, "
                " reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rejection_id,
                    task_id,
                    round_index,
                    llm_call_id,
                    canonical_json(dict(raw_parameters)),
                    reason,
                    _now_iso(),
                ),
            )
        return rejection_id

    def freeze_baseline(
        self,
        *,
        gate_key: str,
        task_id: str,
        passed: bool,
        frozen_result_hash: str | None,
        diagnosis_ref: str | None,
    ) -> None:
        """写一条 `baselines` 行；同一 `gate_key` 下已存在冻结记录时**幂等
        no-op**（不覆盖、不报错，见模块顶部「`freeze_baseline` 的『不覆盖』
        语义」一节的选择与理由）。
        """
        with self.tx() as conn:
            existing = conn.execute(
                "SELECT 1 FROM baselines WHERE gate_key=?", (gate_key,)
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                "INSERT INTO baselines "
                "(gate_key, task_id, passed, frozen_result_hash, diagnosis_ref, "
                " frozen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    gate_key,
                    task_id,
                    int(passed),
                    frozen_result_hash,
                    diagnosis_ref,
                    _now_iso(),
                ),
            )

    def find_baseline(self, gate_key: str) -> BaselineGateStatus | None:
        """按 `gate_key` 查找已冻结的前值基线；无命中返回 `None`。"""
        row = self._conn.execute(
            "SELECT gate_key, task_id, passed, frozen_result_hash, diagnosis_ref, "
            "frozen_at FROM baselines WHERE gate_key=?",
            (gate_key,),
        ).fetchone()
        if row is None:
            return None
        gk, task_id, passed, frozen_result_hash, diagnosis_ref, frozen_at = row
        return BaselineGateStatus(
            gate_key=gk,
            task_id=task_id,
            passed=bool(passed),
            frozen_result_hash=frozen_result_hash,
            diagnosis_ref=diagnosis_ref,
            frozen_at=frozen_at,
        )

    def mark_artifact_missing(self, run_id: str) -> None:
        """把已处于 `status='done'` 的 `runs` 行的 `cause` 列标记为
        `'artifact_missing'`（`design.md` §8.4 / §11.1；`tasks.md` 任务 14.4；
        `cause` 归 `transient_error` 分类）。

        ## 这不是 `close_run_*` 的第二次终态写入——是对既有终态行的窄范围
        审计标注，刻意绕开「六列终态不可覆盖」的既有不变量

        `close_run_ok()` / `close_run_failed()` 保护的六列（`status` /
        `failure_class` / `cause` / `waveform_ref` / `observable_ref` /
        `ended_at`）共同描述「这次仿真运行到底发生了什么」——这条不变量对一个
        `status='running'` → 首次终结的行完全适用。但本方法处理的是另一件事：
        一个**此前已经成功完成、状态早已是 `'done'`** 的历史行，它当年产出的
        `waveform_ref`/`observable_ref` 指向的产物文件，在**今天**（一次全新
        的仿真请求命中了它的 `simulation_key` 时）被发现缺失或读取/哈希失败。
        这不是"重新判定那次仿真当年是否成功"——它当年确实成功了，`status=
        'done'` 这一事实没有变、不应该变。这是一条独立的、追加式的审计注记：
        "这份产物后来被发现不可用"。`cause` 列因此在这里被用作一个**在 done
        行上也能携带信息**的槎位——`close_run_ok()` 把它留 `NULL`（成功的行
        本不需要"原因"），本方法是 `NULL` 之外唯一会写入这一列的路径，且只写
        这一列，不触碰其余五个受保护列（尤其不改 `status`，该行依然是
        `'done'`——它的仿真本身没有失败，只是产物后来丢了）。

        `controller/run_task.py` 的 `run_tier()`（任务 14.4）是本方法目前唯一
        的调用方：`simulation_key` 命中但产物缺失或往返校验失败时，先调用本
        方法标注旧行，再把本次请求当作完全未命中、重新仿真并计入完整预算。

        ## 幂等、best-effort：不存在匹配行或 `cause` 已非空时静默 no-op

        `run_id`（须同时满足 `status='done'` 且 `cause IS NULL`）不匹配任何行
        时不抛异常——这是一条纯粹的审计标注写入，不是调用方控制流决策所依赖
        的前提（`run_tier()` 在调用本方法之后总是无条件继续走"视为未命中、
        重新仿真"这条路径，不检查本方法是否成功标注），没有必要为一个
        "标注失败"的边缘情形（例如该行已被标注过、或调用方传入了不存在的
        `run_id`）中断主流程。这与 `freeze_baseline()` 的幂等 no-op 选择同源
        （见模块顶部「`freeze_baseline` 的『不覆盖』语义」一节）：重复标注
        同一件"产物已确认缺失"的事实不产生任何新副作用，允许静默通过。
        """
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET cause='artifact_missing' "
                "WHERE run_id=? AND status='done' AND cause IS NULL",
                (run_id,),
            )

    def sum_budget_units(self, task_id: str) -> int:
        """按 `task_id` 过滤的 `SUM(runs.budget_units)`，不排除任何状态的行
        （含 `failed`、`cause='process_restart'`、`cache_hit=1`），无匹配行时
        取 0（`COALESCE`）。

        供 `controller.budget.BudgetLedger.used()`（任务 9.2）调用；本方法是
        只读聚合查询，不开事务，与 `find_cached_simulation()` 等既有只读方法
        风格一致。
        """
        (total,) = self._conn.execute(
            "SELECT COALESCE(SUM(budget_units), 0) FROM runs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return int(total)

    def sum_approved_budget_increase(self, task_id: str) -> int:
        """按 `task_id` 过滤、`kind='budget_increase'` 且 `decision='approve'`
        的 `SUM(approvals.extra_units)`，无匹配行时取 0（`COALESCE`）。

        供 `controller.budget.BudgetLedger.remaining()`（任务 9.2）调用；同一
        `approval_id` 对应 `approvals` 表中恰一行，`grant_budget()` 对同一
        `approval_id` 的重复调用只覆盖该行的 `extra_units` 值、不产生第二行，
        因此本方法的 `SUM` 天然不会对同一 `approval_id` 重复计入。
        """
        (total,) = self._conn.execute(
            "SELECT COALESCE(SUM(extra_units), 0) FROM approvals "
            "WHERE task_id=? AND kind='budget_increase' AND decision='approve'",
            (task_id,),
        ).fetchone()
        return int(total)

    def grant_budget(self, *, task_id: str, extra_units: int, approval_id: str) -> None:
        """把追加额度写入该 `approvals` 行的 `extra_units` 列。

        本方法**不改变任何内存计数器**——`Store` 类本身不持有任何预算相关的
        内存状态；`BudgetLedger.remaining()`（`controller/budget.py`，任务 9.2）
        在每次调用时完全从本方法写入的 `approvals.extra_units` 与 `runs`
        表重新求和计算，因此崩溃重启后可完整恢复。

        `approval_id` 与 `task_id` 不匹配（该审批行不存在，或存在但 `task_id`
        不同）时抛 `GrantBudgetError`，不执行任何写入。
        """
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE approvals SET extra_units=? WHERE approval_id=? AND task_id=?",
                (extra_units, approval_id, task_id),
            )
            if cur.rowcount == 0:
                raise GrantBudgetError(
                    f"grant_budget: no approvals row for approval_id={approval_id!r} "
                    f"task_id={task_id!r}"
                )

    # ------------------------------------------------------------------
    # 三个冻结点（design.md §4.5，任务 5.5）
    # ------------------------------------------------------------------

    def freeze(self, kind: FreezeKind, *, hash: str, detail: str | None = None) -> None:
        """写一条 `freezes` 行；`kind` 已存在一行时**拒绝**并抛
        `FreezeAlreadyExistsError`（写一次即锁，见模块顶部「`freeze()` 的
        『拒绝重复』语义」一节；与 `freeze_baseline()` 的幂等 no-op 刻意相反）。

        `kind='safety'` 的 `hash` 计算——`sha256(canonical_json({constraints_hash,
        metrics_hash, scenario_set_hash}))`——由调用方完成，本方法对 `hash` 的
        内容不作任何解读，原样落盘（见模块顶部「职责边界」一节）。
        """
        with self.tx() as conn:
            existing = conn.execute(
                "SELECT 1 FROM freezes WHERE kind=?", (kind,)
            ).fetchone()
            if existing is not None:
                raise FreezeAlreadyExistsError(
                    f"freeze: kind={kind!r} already has a frozen row; a freeze "
                    "point is write-once and cannot be re-frozen"
                )
            conn.execute(
                "INSERT INTO freezes (kind, hash, detail, frozen_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, hash, detail, _now_iso()),
            )

    def find_freeze(self, kind: FreezeKind) -> FreezeStatus | None:
        """按 `kind` 查找已冻结的行；无命中返回 `None`。"""
        row = self._conn.execute(
            "SELECT kind, hash, detail, frozen_at FROM freezes WHERE kind=?",
            (kind,),
        ).fetchone()
        if row is None:
            return None
        k, hash_, detail, frozen_at = row
        return FreezeStatus(kind=k, hash=hash_, detail=detail, frozen_at=frozen_at)

    def freeze_task_set(self, path: str | Path) -> None:
        """读取 `path` 指向的 `task_set.md`、计算其 sha256、写入
        `freezes(kind='task_set')`（`design.md` §4.5 / `requirements.md`
        R21.2；`tasks.md` 任务 19.1）。

        对 `freeze()` 的薄封装（见模块顶部「`freeze_task_set()`：委托给
        `freeze()` 的薄封装，不写独立实现」一节）：本方法只负责「读文件、
        算哈希」这一段 `freeze()` 本身不做的工作，「至多一行、写一次即锁」
        由 `freeze()` 负责——第二次调用（不论文件内容是否变化）与直接调用
        `freeze('task_set', ...)` 两次同样抛 `FreezeAlreadyExistsError`，
        不捕获、不转译，抛出前不执行任何写入（`freeze()` 的既有语义）。

        `path` 按二进制字节读取（`Path.read_bytes()`），不做文本行尾规范化
        ——`design.md` §5.3.1 的行尾规范化规则只适用于参与 `metrics_hash` /
        `constraints_hash` / `scenario_set_hash` 等配置哈希的 YAML/JSON 文本
        输入；`task_set.md` 的 sha256 是「对这份已定稿文档的字节内容做防篡改
        校验」，`requirements.md` R21.2 的字面表述是「其 sha256」，未提及任何
        规范化步骤，故直接对文件原始字节取 sha256。
        """
        content = Path(path).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        self.freeze("task_set", hash=digest)

    def update_safety_freeze_metrics_hash(self, new_hash: str, *, detail: str) -> None:
        """`kind='safety'` 行的 `hash` 列在 M1 出口前允许更新恰好一次（补齐
        `compare_tolerance`，`design.md` §4.5 / requirements.md R3.8）；本方法
        是「写一次即锁」规则里唯一的例外，且自带「只能用一次」互锁。

        - 不存在 `kind='safety'` 行时抛 `SafetyFreezeMissingError`（不能更新一
          个从未冻结过的行）。
        - 现有 `detail` 已包含更新标记（`_METRICS_HASH_UPDATE_MARKER`）时抛
          `SafetyFreezeUpdateWindowClosedError`，且 `hash`/`detail`/`frozen_at`
          三列保持完全不变。
        - 否则把 `hash` 更新为 `new_hash`，`detail` 覆写为「标记 + 调用方传入
          的描述文本」。

        本方法**不**校验「当前确实处于 M1 出口前」，也**不**校验「这次变更确实
        只涉及 `compare_tolerance` 字段而非其他字段」——这两条时序/字段范围判断
        属于调用方（`controller/preflight.py`）的职责，本方法只机械地强制「这
        个更新通道至多被使用一次」（见模块顶部「职责边界」一节）。
        """
        with self.tx() as conn:
            row = conn.execute(
                "SELECT detail FROM freezes WHERE kind='safety'"
            ).fetchone()
            if row is None:
                raise SafetyFreezeMissingError(
                    "update_safety_freeze_metrics_hash: no kind='safety' row "
                    "exists yet; freeze it first"
                )
            existing_detail = row[0]
            if existing_detail and _METRICS_HASH_UPDATE_MARKER in existing_detail:
                raise SafetyFreezeUpdateWindowClosedError(
                    "update_safety_freeze_metrics_hash: the single metrics_hash "
                    "update window has already been used "
                    f"(existing detail={existing_detail!r})"
                )
            new_detail = f"{_METRICS_HASH_UPDATE_MARKER}: {detail}"
            conn.execute(
                "UPDATE freezes SET hash=?, detail=? WHERE kind='safety'",
                (new_hash, new_detail),
            )
