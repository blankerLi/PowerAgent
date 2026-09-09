"""poweragent/config/schema.py

四个配置文件（`task.yaml` / `model.yaml` / `metrics.yaml` / `constraints.yaml`）的
pydantic v2 强类型模型（design.md §4；tasks.md 任务 5.1；需求追溯 R1.1, R3.1, R5.7,
R7.9, R8.1, R9.1, R10.1, R10.10）。

本模块的范围（与占位符处理的边界划分）
----------------------------------------
本模块只定义**形状**：字段类型、必填/可选、单位字符串字面量、数值闭区间、恰定数量的
结构约束（如恰四条 `hard_constraints`、恰两项 `constraint_observables`）。

四个模板文件（`configs/task_template.yaml` / `model.yaml` / `metrics.yaml` /
`constraints.yaml`，见任务 5.7）中大量物理数值字段仍以字面量字符串 `"<...>"` 占位，
等待 Owner 在 M0 填写。**检测并拒绝这些占位符是 `config/loader.py`（任务 5.2）的职责，
不是本模块的职责**：`config/loader.py` 在把原始 YAML 数据喂给本模块的 pydantic 模型
之前，先对全部必填字段做占位符预检（pre-flight），命中 `"<...>"` 占位形式即抛出
`config_incomplete: <字段路径>`（需求 R2.4）并不再构造模型。因此本模块里的物理数值
字段一律按其真实类型建模（`float` / `str` / `int` 等），**不引入
`Literal["<...>"]` 之类的占位符感知联合类型**——占位符感知不应该渗透进强类型 schema，
否则每个数值字段的类型签名都会被污染成「真实类型 | 占位符」，而这类污染本该由加载器
在模型构造之前一次性挡住。

本模块显式**不实现**的校验（按任务边界划给其他任务）
----------------------------------------------------
- `ticks` 的展开（`{count, spacing: log}` → 具体档位列表）与档位字面量的
  `.12g` 规范化 —— 归任务 5.9。
- 占位符预检与四类不合规的逐条报告、`load_all()` —— 归任务 5.2（`config/loader.py`）。
- `canonical_json` / 各类哈希 —— 归任务 5.3（`config/hashing.py`）。

跨文件一致性与取值上界断言（任务 5.8，`check_cross_file_consistency()`）
--------------------------------------------------------------------------
`divergence_guard` vs `hard_constraints`、`metrics.yaml` 与 `constraints.yaml` 的
单位逐字符一致、`ticks` 等比校验、`tick_match_rel_tol` 与 `tick_ratio` 的关系，
由本模块底部的 `check_cross_file_consistency()` 实现（任务 5.8，单独派发的第二轮
增补）。`extra_engine_starts_per_candidate`（`MarginExtraction`，0<=v<=4）与
`max_llm_repair_rounds`（`Budget`，0<=v<=5）的数值上界已在本模块上方以单文件
`Field(ge=, le=)` 约束实现（任务 5.1），任务 5.8 不重复定义、只在文档与测试中确认。

`check_cross_file_consistency()` **不是** pydantic validator，而是一个操作四个
**已构造**模型实例的普通函数：pydantic v2 的 `model_validator` 只能看到同一个模型
自身的字段，无法跨越 `ModelConfig` / `MetricsConfig` / `ConstraintsConfig` 三个
独立构造的模型互相取值（每个模型只在各自的 `model_validate()` 调用中知道自己的
数据）。因此跨文件断言必须在四个文件都成功构造之后，由调用方（`config.loader.
load_all()`，任务 5.2）传入四个已构造实例后统一执行，这也正是任务 5.8 描述中
「`config` 加载全部四文件，因此不破坏导入边界」一句的含义。

单位与数值域校验的实现方式
--------------------------
凡「字段声明了 `unit`」的物理量，本模块对其 `unit` 字面量做逐字符相等校验
（`Literal[...]`，pydantic 在校验期已经是逐字符比较，不需要额外的字符串比较逻辑）。
凡 schema 声明了闭区间的数值字段，使用 pydantic 的 `Field(ge=..., le=...)` 或
`model_validator` 表达闭区间约束。
"""

from __future__ import annotations

from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# 公共基类
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    """全部 schema 模型的公共基类：拒绝未声明字段，字段名与配置文件键一一对应。"""

    model_config = ConfigDict(extra="forbid")


# 设计变量键集合：三处（候选 parameters_si、design_space.variables、
# io_contract.injectable_params）恰为 {rcomp, ccomp}（R1.1）。
DesignVariableName = Literal["rcomp", "ccomp"]

# invalid_if 恰 8 项固定枚举（R7.9）；不是表达式字符串，枚举外字符串一律拒绝。
InvalidPredicateName = Literal[
    "waveform_unreadable",
    "signal_missing",
    "window_empty",
    "waveform_not_converged",
    "vout_out_of_guard",
    "no_step_detected",
    "not_settled_within_window",
    "extraction_failed",
]


# ===========================================================================
# task.yaml
# ===========================================================================


class ObjectiveTarget(StrictModel):
    """可选节；给出则参与 controller.stop 的 target_reached 判定。"""

    target_value: float


class Budget(StrictModel):
    max_engine_starts: int = Field(gt=0)
    max_wallclock_hours: float = Field(gt=0)
    max_attempts_per_scenario: int = Field(default=2, ge=1)
    max_llm_repair_rounds: int = Field(default=2, ge=0, le=5)


class LlmConfig(StrictModel):
    timeout_s: int = Field(default=120, gt=0)
    max_network_retries: int = Field(default=2, ge=0)


class StopConfig(StrictModel):
    no_improvement_rounds: int = Field(gt=0)
    stop_on_first_feasible: bool = False


ScenarioTier = Literal["screening", "evaluation", "robustness"]
ModelVariant = Literal["switching", "averaged"]


class ScenarioSpec(StrictModel):
    scenario_id: str = Field(min_length=1)
    tier: ScenarioTier
    model_variant: ModelVariant
    vin_v: float
    temp_c: float
    load_start_a: float
    load_end_a: float
    slew_a_per_us: float
    require_margin: bool


class ComponentTolerance(StrictModel):
    parameter: str = Field(min_length=1)
    relative_range: tuple[float, float]

    @model_validator(mode="after")
    def _check_range_order(self) -> "ComponentTolerance":
        low, high = self.relative_range
        if low > high:
            raise ValueError(
                f"relative_range: 下限 {low} 不得大于上限 {high}"
            )
        return self


class RobustnessConfig(StrictModel):
    component_tolerance: list[ComponentTolerance] = Field(default_factory=list)
    include_calibration_uncertainty: bool = True


class TaskConfig(StrictModel):
    """`task.yaml` 的全节强类型模型（design.md §4.1）。"""

    task_id: str = Field(min_length=1)
    simulation_only: bool
    objective_target: ObjectiveTarget | None = None
    budget: Budget
    llm: LlmConfig = Field(default_factory=LlmConfig)
    stop: StopConfig
    scenarios: list[ScenarioSpec] = Field(min_length=1)
    robustness: RobustnessConfig = Field(default_factory=RobustnessConfig)

    @model_validator(mode="after")
    def _check_scenario_id_unique(self) -> "TaskConfig":
        seen: set[str] = set()
        for scenario in self.scenarios:
            if scenario.scenario_id in seen:
                raise ValueError(
                    f"scenarios: scenario_id 重复: {scenario.scenario_id!r}"
                )
            seen.add(scenario.scenario_id)
        return self


# ===========================================================================
# model.yaml
# ===========================================================================


class ModelVariantPackage(StrictModel):
    """`model_package.switching` / `model_package.averaged` 的依赖闭包清单（R4.1）。"""

    entry: str = Field(min_length=1)
    referenced_models: list[str] = Field(default_factory=list)
    data_dictionaries: list[str] = Field(default_factory=list)
    matlab_functions: list[str] = Field(default_factory=list)
    init_scripts: list[str] = Field(default_factory=list)
    mat_inputs: list[str] = Field(default_factory=list)
    custom_libraries: list[str] = Field(default_factory=list)


class ModelPackage(StrictModel):
    switching: ModelVariantPackage
    # 条件节；averaged_model_required=false 时留空（不填），故允许 None。
    averaged: ModelVariantPackage | None = None


class InjectableParam(StrictModel):
    block_path: str = Field(min_length=1)
    param: str = Field(min_length=1)
    unit: Literal["ohm", "F"]


class InjectableParams(StrictModel):
    """`io_contract.injectable_params`：设计变量键集合恰为 {rcomp, ccomp}（R1.1）。"""

    rcomp: InjectableParam
    ccomp: InjectableParam

    @model_validator(mode="after")
    def _check_units(self) -> "InjectableParams":
        if self.rcomp.unit != "ohm":
            raise ValueError("injectable_params.rcomp.unit: 必须为 'ohm'")
        if self.ccomp.unit != "F":
            raise ValueError("injectable_params.ccomp.unit: 必须为 'F'")
        return self


class OutputSignal(StrictModel):
    logsout_name: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    dimension: str | None = None


class OutputSignals(StrictModel):
    vout: OutputSignal
    iout: OutputSignal
    iphase: OutputSignal


SolverType = Literal["ode23tb", "ode23t", "ode15s", "ode1be"]


class SolverConfig(StrictModel):
    type: SolverType
    max_step: float = Field(gt=0)
    rel_tol: float = Field(gt=0)
    stop_time: float = Field(gt=0)


class DivergenceGuard(StrictModel):
    """MATLAB 侧安全界，与 eval 的评价阈值分离（R5.7）。"""

    vout_abs_max: float = Field(gt=0)
    iphase_abs_max: float = Field(gt=0)


class IoContract(StrictModel):
    injectable_params: InjectableParams
    output_signals: OutputSignals
    solver: SolverConfig
    initial_condition: str = Field(min_length=1)
    vout_target_v: float = Field(gt=0)
    divergence_guard: DivergenceGuard


class BaselineParametersSi(StrictModel):
    """`model.yaml` 的 `baseline.parameters_si`：键集合恰为 {rcomp, ccomp}（R10.1）。

    本模块只校验字段存在与类型；「落在 domain 闭区间内、不要求命中 ticks」的校验
    依赖 `constraints.yaml` 的 `design_space.variables`，属于跨文件校验，归任务 5.8。
    """

    rcomp: float
    ccomp: float


class Baseline(StrictModel):
    parameters_si: BaselineParametersSi
    # 允许 null 且不判为 config_incomplete（R10.1）。
    measured_evidence_ref: str | None = None


ExecutionMode = Literal["serial", "fast_restart", "parsim"]


class RuntimeConfig(StrictModel):
    matlab_release: str = Field(min_length=1)
    toolboxes: list[str] = Field(default_factory=list)
    python_version: str = Field(min_length=1)
    execution_mode: ExecutionMode = "serial"
    # 本轮新增字段：单次仿真墙钟上限（int），map_error.m 的 timeout 判据来源。
    max_wallclock_per_run_s: int = Field(gt=0)


class DualModelCheckpoint(StrictModel):
    scenario_id: str = Field(min_length=1)
    steady_tol: float = Field(gt=0)
    transient_tol: float = Field(gt=0)


class DualModelConsistency(StrictModel):
    """条件节；仅供 M-1/M0 的 Dual-Model Consistency 测试层与 preflight 消费，运行期不复核。"""

    checkpoints: list[DualModelCheckpoint] = Field(default_factory=list)


class ModelConfig(StrictModel):
    """`model.yaml` 的全节强类型模型（design.md §4.2）。"""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_package: ModelPackage
    averaged_model_required: bool
    io_contract: IoContract
    baseline: Baseline
    runtime: RuntimeConfig
    dual_model_consistency: DualModelConsistency = Field(
        default_factory=DualModelConsistency
    )


# ===========================================================================
# metrics.yaml
# ===========================================================================

MetricId = Literal[
    "output_ripple",
    "overshoot",
    "undershoot",
    "settling_time",
    "phase_peak_current",
    "phase_margin",
    "gain_margin",
    "efficiency",
    "sensitivity",
]

# 每个指标只允许适用于自身的 invalid_if 谓词子集（design.md §4.3 表格；R7.9）。
_INVALID_IF_ALLOWED_BY_METRIC: Mapping[str, frozenset[str]] = {
    "settling_time": frozenset(
        {
            "waveform_unreadable",
            "signal_missing",
            "window_empty",
            "waveform_not_converged",
            "vout_out_of_guard",
            "no_step_detected",
            "not_settled_within_window",
        }
    ),
    "output_ripple": frozenset(
        {
            "waveform_unreadable",
            "signal_missing",
            "window_empty",
            "waveform_not_converged",
            "vout_out_of_guard",
        }
    ),
    "overshoot": frozenset(
        {
            "waveform_unreadable",
            "signal_missing",
            "window_empty",
            "waveform_not_converged",
            "vout_out_of_guard",
            "no_step_detected",
        }
    ),
    "undershoot": frozenset(
        {
            "waveform_unreadable",
            "signal_missing",
            "window_empty",
            "waveform_not_converged",
            "vout_out_of_guard",
            "no_step_detected",
        }
    ),
    "phase_peak_current": frozenset(
        {
            "waveform_unreadable",
            "signal_missing",
            "window_empty",
            "waveform_not_converged",
        }
    ),
    "phase_margin": frozenset({"extraction_failed"}),
    "gain_margin": frozenset({"extraction_failed"}),
}


class CompareTolerance(StrictModel):
    """`compare_tolerance`：至少 absolute 或 relative 之一（时域指标含两者，裕量类只含 absolute）。"""

    absolute: float | None = Field(default=None, ge=0)
    relative: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_at_least_one(self) -> "CompareTolerance":
        if self.absolute is None and self.relative is None:
            raise ValueError("compare_tolerance: absolute 与 relative 不可同时缺失")
        return self


class TimeDomainMetricSpec(StrictModel):
    """五项时域指标（`output_ripple` / `overshoot` / `undershoot` / `settling_time` /
    `phase_peak_current`）的公共形状（design.md §4.3）。"""

    signal: str = Field(min_length=1)
    definition: str | None = None
    window: tuple[str, str]
    band: str | None = None
    filter: str
    unit: Literal["V", "us", "A"]
    aggregation: str | None = None
    invalid_if: list[InvalidPredicateName] = Field(min_length=1)
    compare_tolerance: CompareTolerance
    threshold_source: str = Field(min_length=1)


class MarginMetricSpec(StrictModel):
    """`phase_margin` / `gain_margin` 的公共形状（design.md §4.3）。"""

    source: Literal["frequency_response"]
    unit: Literal["deg", "dB"]
    invalid_if: list[InvalidPredicateName] = Field(min_length=1)
    compare_tolerance: CompareTolerance
    threshold_source: str = Field(min_length=1)


class MetricsSection(StrictModel):
    """`metrics.yaml` 的 `metrics` 节：六项指标（settling_time / output_ripple /
    overshoot / undershoot / phase_peak_current / phase_margin / gain_margin）。"""

    settling_time: TimeDomainMetricSpec
    output_ripple: TimeDomainMetricSpec
    overshoot: TimeDomainMetricSpec
    undershoot: TimeDomainMetricSpec
    phase_peak_current: TimeDomainMetricSpec
    phase_margin: MarginMetricSpec
    gain_margin: MarginMetricSpec

    @model_validator(mode="after")
    def _check_invalid_if_applicable(self) -> "MetricsSection":
        for metric_id in (
            "settling_time",
            "output_ripple",
            "overshoot",
            "undershoot",
            "phase_peak_current",
            "phase_margin",
            "gain_margin",
        ):
            spec = getattr(self, metric_id)
            allowed = _INVALID_IF_ALLOWED_BY_METRIC[metric_id]
            offending = [v for v in spec.invalid_if if v not in allowed]
            if offending:
                raise ValueError(
                    f"metrics.{metric_id}.invalid_if: 谓词 {offending!r} 不适用于该指标"
                    f"（只允许 {sorted(allowed)!r}）"
                )
        return self


class ConstraintObservableSpec(StrictModel):
    """`constraint_observables` 的单项形状：硬约束的支撑观测量，不是项目指标（R7.9）。"""

    signal: str = Field(min_length=1)
    window: tuple[str, str]
    aggregation: Literal["min", "max"]
    unit: Literal["V", "A"]


class ConstraintObservables(StrictModel):
    """恰两项 `obs.vout_min` / `obs.vout_max`；不进 active_metrics、不进 objective、
    不计入「六项指标」（R7.9）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vout_min: ConstraintObservableSpec = Field(alias="obs.vout_min")
    vout_max: ConstraintObservableSpec = Field(alias="obs.vout_max")


PrimaryMarginMethod = Literal[
    "linear_analysis_on_averaged", "freq_response_estimator_on_switching"
]
CrossCheckMethod = Literal["manual_bode_reference", "the_other_method"]


class CrossCheckTolerance(StrictModel):
    phase_deg: float = Field(gt=0)
    gain_db: float = Field(gt=0)


class CrossCheckRecord(StrictModel):
    """含 `path` 与 `sha256` 的结构（R8.1）。"""

    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class MarginExtraction(StrictModel):
    """`margin_extraction` 六项全部必填、不留占位（R8.1）。"""

    primary_method: PrimaryMarginMethod
    cross_check_method: CrossCheckMethod
    cross_check_tolerance: CrossCheckTolerance
    extra_engine_starts_per_candidate: int = Field(ge=0, le=4)
    cross_check_record: CrossCheckRecord


ObjectiveDirection = Literal["minimize", "maximize"]
AggregationKind = Literal["max_over_completed_evaluation_set"]


class PrimaryObjective(StrictModel):
    metric_id: MetricId
    aggregation: AggregationKind
    direction: ObjectiveDirection


class SecondaryObjectiveTerm(StrictModel):
    """`objective.secondary_lexicographic` 每项：含 `metric_id` 与 `direction` 两字段（R1.2）。"""

    metric_id: MetricId
    direction: ObjectiveDirection


class Objective(StrictModel):
    primary: PrimaryObjective
    tie_tolerance: float = Field(ge=0)
    secondary_lexicographic: list[SecondaryObjectiveTerm] = Field(default_factory=list)


class MetricsConfig(StrictModel):
    """`metrics.yaml` 的全节强类型模型（design.md §4.3）。"""

    active_metrics: list[MetricId] = Field(min_length=1)
    metrics: MetricsSection
    constraint_observables: ConstraintObservables
    margin_extraction: MarginExtraction
    objective: Objective

    @model_validator(mode="after")
    def _check_active_metrics_unique(self) -> "MetricsConfig":
        seen: set[str] = set()
        for metric_id in self.active_metrics:
            if metric_id in seen:
                raise ValueError(f"active_metrics: 重复的 metric_id: {metric_id!r}")
            seen.add(metric_id)
        return self


# ===========================================================================
# constraints.yaml
# ===========================================================================

ConstraintSense = Literal["lower", "upper"]


class HardConstraintEntry(StrictModel):
    """`hard_constraints` 单条：恰含 value / observable / sense / applies_to_tier
    四字段，**不含 `model_variant` 字段**（R9.1，本轮已删除该字段）。"""

    value: float
    observable: str = Field(min_length=1)
    sense: ConstraintSense
    applies_to_tier: list[ScenarioTier] = Field(min_length=1)


class HardConstraints(StrictModel):
    """恰五条且不接受第六条（R9.1）：vout_min / vout_max / peak_current_max /
    phase_margin_min / gain_margin_min。

    `gain_margin_min` 是本节的第五条，由 AC1 的变更记录引入：`phase_margin_min`
    只是 45°/6 dB 配对判据的一半，缺了增益裕量那一半时，参考扫描的最优点会落在
    增益裕量 1.9 dB 的角点上——形式上满足全部约束，实际接近失稳。
    """

    vout_min: HardConstraintEntry
    vout_max: HardConstraintEntry
    peak_current_max: HardConstraintEntry
    phase_margin_min: HardConstraintEntry
    gain_margin_min: HardConstraintEntry


DesignVariableScale = Literal["log", "linear"]


class TicksCount(StrictModel):
    """`ticks` 的隐式（展开式）形式：`{count, spacing: log}`。"""

    count: int = Field(gt=1)
    spacing: Literal["log"]


class DesignVariableSpec(StrictModel):
    unit: Literal["ohm", "F"]
    domain: tuple[float, float]
    scale: DesignVariableScale
    # ticks 为显式列表（长度 >= 2）或 {count, spacing} 展开式；具体展开逻辑归任务 5.9。
    ticks: list[float] | TicksCount

    @model_validator(mode="after")
    def _check_domain_order(self) -> "DesignVariableSpec":
        low, high = self.domain
        if low >= high:
            raise ValueError(f"domain: 下限 {low} 必须严格小于上限 {high}")
        return self


class DesignVariables(StrictModel):
    """设计变量键集合恰为 {rcomp, ccomp}（R1.1）。"""

    rcomp: DesignVariableSpec
    ccomp: DesignVariableSpec

    @model_validator(mode="after")
    def _check_units(self) -> "DesignVariables":
        if self.rcomp.unit != "ohm":
            raise ValueError("design_space.variables.rcomp.unit: 必须为 'ohm'")
        if self.ccomp.unit != "F":
            raise ValueError("design_space.variables.ccomp.unit: 必须为 'F'")
        return self


class DeviceLimitEntry(StrictModel):
    """关键器件参数，唯一权威来源；`source` 须非空字符串（R13.5）。"""

    value: float
    source: str = Field(min_length=1)


class DeviceLimits(StrictModel):
    cout_c: DeviceLimitEntry
    cout_esr: DeviceLimitEntry
    l_per_phase: DeviceLimitEntry


class DesignSpace(StrictModel):
    variables: DesignVariables
    novelty_min_ticks: float = Field(gt=0)
    # 本轮新增字段：档位命中的相对容差，默认 1e-6（R9.1）。
    tick_match_rel_tol: float = Field(default=1e-6, gt=0)
    device_limits: DeviceLimits


class ConstraintsConfig(StrictModel):
    """`constraints.yaml` 的全节强类型模型（design.md §4.4）。"""

    hard_constraints: HardConstraints
    design_space: DesignSpace


# ===========================================================================
# 任务 5.8：跨文件一致性与取值上界断言
# ===========================================================================
#
# 架构说明（为何这是普通函数而不是 pydantic validator）
# --------------------------------------------------------------------------
# `TaskConfig` / `ModelConfig` / `MetricsConfig` / `ConstraintsConfig` 各自由
# 独立的 `model_validate()` 调用构造，pydantic v2 的 `model_validator` 只能看到
# **同一个模型自身**的字段——`ModelConfig` 的 validator 无法读到
# `ConstraintsConfig` 的实例，反之亦然。因此任何需要同时比较两个不同顶层模型
# 字段的断言（`model.yaml` 的 `divergence_guard` vs `constraints.yaml` 的
# `hard_constraints`；`ticks` 与 `tick_match_rel_tol` 的关系虽在同一个
# `ConstraintsConfig` 内、但涉及跨节推导，归入本节一并实现以保持内聚）都不可能
# 落在某一个模型的 validator 里。
#
# `check_cross_file_consistency()` 是一个操作**四个已构造模型实例**的普通函数，
# 由调用方（`config.loader.load_all()`，任务 5.2）在全部四个文件各自成功通过
# `model_validate()` 之后调用一次；这正是本任务描述「`config` 加载全部四文件，
# 因此不破坏导入边界」一句的含义——`config` 包内部允许这种跨模型引用，
# 只是不能把它做成某个模型自身的 validator。
#
# 与 `unit_mismatch` 的边界划分（重要）
# --------------------------------------------------------------------------
# `tasks.md` 任务 5.8 的原文本身把 `unit_mismatch` 的 `PreflightError` 抛出入口
# 指向**任务 12.6**（`controller/preflight.py`），不是本任务：
#
#     "同一物理量在 metrics.yaml 与 constraints.yaml 声明的单位字符串必须
#      逐字符相同...；抛 PreflightError('unit_mismatch') 的入口见任务 12.6"
#
# 本函数因此**不实现** unit_mismatch 检查。理由不仅是任务边界，还有一个更根本
# 的 schema 约束：`requirements.md` R9.1 要求 `hard_constraints` 每条**恰含**
# `value` / `observable` / `sense` / `applies_to_tier` **四个字段**且不含
# `model_variant`——这是已锁定的验收标准，不允许再加一个 `unit` 字段。
# `design.md` §4.4 的 YAML 示例同样只给四个字段（`value` 后的 `<V>` 是注释里的
# 单位标注，不是数据字段）。
#
# 因此 `constraints.yaml` 的 `hard_constraints` 结构上**没有第二处**可与
# `metrics.yaml` 比较的单位声明——`hard_constraints.<name>.observable` 指向的
# 物理量的单位，唯一的结构化来源就是 `metrics.yaml`（经
# `constraint_observables.obs.*.unit` 或 `metrics.<metric_id>.unit`）。
# `design.md` §6.10（R18.12）「硬约束条目的单位取自 constraints.yaml 中该约束
# 声明的单位」与 R9.1 的四字段约束之间存在措辞上的张力：读作「该约束在语义上
# 对应的单位」（经 observable 绑定从 metrics.yaml 取得）而非「constraints.yaml
# 里另有一个 unit 字段」，是唯一不违反 R9.1 的读法。本函数不越权替任务 12.6
# 做决定，只在此处记录该结论供任务 12.6 的实现者参考：unit_mismatch 检查应
# 比较的是「metrics.yaml 中某个 observable/metric 的 unit」与「该硬约束在
# 报告/其他配置节中被要求匹配的单位期望」，而不是 constraints.yaml 自身的
# 第二个 unit 字段（因为不存在）。
#
# `extra_engine_starts_per_candidate` 与 `max_llm_repair_rounds` 的取值上界
# --------------------------------------------------------------------------
# 两者已在本文件上方以单文件 `Field(ge=, le=)` 约束实现（任务 5.1）：
# `MarginExtraction.extra_engine_starts_per_candidate: int = Field(ge=0, le=4)`、
# `Budget.max_llm_repair_rounds: int = Field(default=2, ge=0, le=5)`。二者均是
# 单文件内的数值域约束，不是跨文件断言，因此不属于本函数需要新增代码的范围；
# 本模块不重复定义、不弱化既有约束。


class CrossFileConsistencyError(ValueError):
    """跨文件一致性或取值上界断言失败时抛出（design.md §4.2；`tasks.md` 任务 5.8；
    需求追溯 R3.14, R3.15, R11.7, R15.12）。

    消息中含全部被比较字段的完整路径（形如 `<file>:<field.path>`），便于定位
    究竟是哪两个字段发生了冲突。
    """


def _tick_ratio_from_explicit_list(variable_name: str, ticks: list[float]) -> float:
    """从显式档位列表计算相邻档位比，并断言全部相邻比在 `1e-6` 相对容差内一致
    （等比强制，R3.15）。

    以第一个相邻比（`ticks[1] / ticks[0]`）为参考值，其余相邻比与参考值的相对
    偏差超出 `1e-6` 即拒绝、不做归一化修正。列表长度小于 2（无相邻对可比）视为
    不合规。
    """
    if len(ticks) < 2:
        raise CrossFileConsistencyError(
            f"constraints.yaml:design_space.variables.{variable_name}.ticks: "
            f"长度 {len(ticks)} 小于 2，无法确定相邻档位比"
        )

    ratios = [ticks[i + 1] / ticks[i] for i in range(len(ticks) - 1)]
    reference = ratios[0]

    for index, ratio in enumerate(ratios):
        if abs(ratio - reference) > 1e-6 * abs(reference):
            raise CrossFileConsistencyError(
                f"constraints.yaml:design_space.variables.{variable_name}.ticks: "
                f"不满足等比约束——相邻比 ratios[{index}]={ratio!r} 与参考相邻比 "
                f"ratios[0]={reference!r} 的相对偏差超出 1e-6 容差"
                f"（ticks={ticks!r}）"
            )

    return reference


def _tick_ratio_from_ticks_count(
    variable_name: str, domain: tuple[float, float], ticks_count: TicksCount
) -> float:
    """`{count, spacing: log}` 隐式形式的等比档位比：由构造保证等比，
    此处只推导其隐含的相邻档位比供 (d) 的容差关系检查使用，不做展开
    （展开归任务 5.9）。"""

    low, high = domain
    count = ticks_count.count
    if count < 2:
        raise CrossFileConsistencyError(
            f"constraints.yaml:design_space.variables.{variable_name}.ticks.count: "
            f"{count!r} 必须大于等于 2，才能定义相邻档位比"
        )
    return (high / low) ** (1.0 / (count - 1))


def _check_ticks_geometric_and_tolerance(constraints_cfg: ConstraintsConfig) -> None:
    """(c) `ticks` 强制等比 + (d) `tick_match_rel_tol` 严格小于
    `0.01 × (tick_ratio − 1)`（R3.15）。

    对 `rcomp` 与 `ccomp` 两个设计变量各自：
    - `ticks` 为显式列表时，先做等比校验（(c)），再用得到的参考相邻比做 (d)；
    - `ticks` 为 `TicksCount` 时，跳过 (c)（隐式形式由构造保证等比），
      用推导出的隐含相邻比做 (d)。
    """
    design_space = constraints_cfg.design_space
    tol = design_space.tick_match_rel_tol

    for variable_name in DesignVariables.model_fields:
        spec: DesignVariableSpec = getattr(design_space.variables, variable_name)
        ticks = spec.ticks

        if isinstance(ticks, TicksCount):
            tick_ratio = _tick_ratio_from_ticks_count(variable_name, spec.domain, ticks)
        else:
            tick_ratio = _tick_ratio_from_explicit_list(variable_name, ticks)

        threshold = 0.01 * (tick_ratio - 1)
        if not (tol < threshold):
            raise CrossFileConsistencyError(
                f"constraints.yaml:design_space.tick_match_rel_tol={tol!r} 必须严格小于 "
                f"0.01 × (tick_ratio − 1)={threshold!r}"
                f"（tick_ratio={tick_ratio!r} 取自 "
                f"constraints.yaml:design_space.variables.{variable_name}.ticks），"
                f"否则容差不足以远小于相邻档位间隔，两个相邻档位可能被混淆"
            )


def _check_divergence_guard_vs_hard_constraints(
    model_cfg: ModelConfig, constraints_cfg: ConstraintsConfig
) -> None:
    """(a) `divergence_guard` 的两个安全界不得低于对应硬约束值（R3.14）。

    安全界低于评价阈值会使合法候选被误判为发散，因此这里用 `<` 判失败
    （取等仍合规，对应「不低于」的措辞）。
    """
    guard = model_cfg.io_contract.divergence_guard
    hard_constraints = constraints_cfg.hard_constraints

    if guard.vout_abs_max < hard_constraints.vout_max.value:
        raise CrossFileConsistencyError(
            f"model.yaml:io_contract.divergence_guard.vout_abs_max "
            f"({guard.vout_abs_max!r}) must be >= "
            f"constraints.yaml:hard_constraints.vout_max.value "
            f"({hard_constraints.vout_max.value!r})"
        )

    if guard.iphase_abs_max < hard_constraints.peak_current_max.value:
        raise CrossFileConsistencyError(
            f"model.yaml:io_contract.divergence_guard.iphase_abs_max "
            f"({guard.iphase_abs_max!r}) must be >= "
            f"constraints.yaml:hard_constraints.peak_current_max.value "
            f"({hard_constraints.peak_current_max.value!r})"
        )


def check_cross_file_consistency(
    task_cfg: TaskConfig,
    model_cfg: ModelConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
) -> None:
    """跨文件一致性断言；不合规时抛出 `CrossFileConsistencyError`
    （`ValueError` 子类），消息含被比较字段的完整路径。

    调用时机：`config.loader.load_all()`（任务 5.2）在四个配置文件**全部**
    成功构造为 pydantic 模型之后调用本函数一次；任一单文件构造失败时不会
    走到这里（`load_all()` 的既有行为——只要有一条问题，四个模型都不构造）。

    本函数覆盖的检查（对应 `tasks.md` 任务 5.8 列出的五项中的第 1、3、4 项）：
      (a) `model.yaml:io_contract.divergence_guard` 的两个安全界不得低于
          对应的 `constraints.yaml:hard_constraints` 阈值。
      (c) `constraints.yaml:design_space.variables.{rcomp,ccomp}.ticks`
          显式列表形式下强制等比。
      (d) `tick_match_rel_tol` 严格小于 `0.01 × (tick_ratio − 1)`。

    本函数**不覆盖**的两项（均有明确的归属理由，见本节顶部的架构说明）：
      (b) unit_mismatch —— `tasks.md` 任务 5.8 原文明确把该 `PreflightError`
          的抛出入口指向任务 12.6（`controller/preflight.py`），且
          `constraints.yaml` 的 `hard_constraints` 因 R9.1「恰四字段」的
          约束没有第二个 `unit` 字段可比较。
      (e) `extra_engine_starts_per_candidate` / `max_llm_repair_rounds` 的
          取值上界 —— 已由任务 5.1 的单文件 `Field(ge=, le=)` 约束实现，
          不是跨文件断言，此处不重复定义。

    `task_cfg` 目前未被本函数使用；保留该形参是为了与
    `config.loader.load_all()` 一次性传入全部四个已构造实例的调用惯例保持
    一致，也为未来可能出现的、需要同时引用 `task.yaml` 的跨文件断言预留位置
    （例如 `dual_model_consistency.checkpoints[].scenario_id` 与
    `task.yaml` 场景集的一致性——该检查目前由
    `config/dual_model_consistency.py` 独立实现，未合并进本函数）。
    """
    _check_divergence_guard_vs_hard_constraints(model_cfg, constraints_cfg)
    _check_ticks_geometric_and_tolerance(constraints_cfg)
