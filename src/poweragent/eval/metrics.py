"""poweragent/eval/metrics.py

五项时域指标 + 两项约束支撑观测量（`design.md` §6.5.1；`tasks.md` 任务 10.1；
需求追溯 R7.1, R7.2, R7.3, R7.5, R7.7, R7.10）。

## 本模块的范围

`eval` 只接受产物引用（`waveform_ref: str`）与已校验配置（`MetricsConfig` /
`TimeDomainMetricSpec` / `ConstraintObservableSpec` / `DivergenceGuard`，均来自
`config/schema.py`），代码中不引用 `.slx` 路径、`SimulationInput`、`logsout`
（那些是仿真期的概念，运行期早已结束）；本模块不调 LLM、不发起仿真、不 import
`sim.*` / `agent.*`。

## `Waveform`：已加载波形的最小数据持有者

`design.md` §6.5.1 只给出 `compute_metrics(waveform_ref: str, ...)` 与
`output_ripple(w: Waveform, spec: MetricSpec) -> MetricResult` 等函数签名，未在
文档任何地方定义 `Waveform` 类本身的字段——因此本模块自行设计并在此登记：

```
Waveform.signals: Mapping[str, tuple[np.ndarray, np.ndarray]]
    信号名 -> (time_s, data)。data 对单通道信号（Vout/Iout）为 1 维数组；对多相
    信号（Iphase，dimension=n_phase）为形状 (len(time_s), n_phase) 的 2 维数组
    （约定：时间维在前）。
Waveform.step_trigger_s: float | None
    该场景负载阶跃的触发时刻（绝对仿真时间，秒）；场景不含阶跃或上游未能识别
    阶跃时为 None——`no_step_detected` 判据直接查这个字段，不在本模块内做信号
    检测。
Waveform.sim_start_s: float = 0.0
Waveform.sim_end_s: float | None
    None 时取被访问信号自身最后一个采样时刻（不同信号采样长度可能不同，因此
    不能定死为一个模块级常量）。
```

这是一个「已加载/已提取」的数据持有者，不含任何仿真态概念。它是
`compute_metrics()` 的调用方（尚未搭建的上游波形加载步骤，可能落在
`sim/simulate.py` 或一个专门的 loader 模块）应产出的形状。本模块自身只在
`_load_waveform()` 这一个私有函数里触碰 MAT 文件 I/O，且明确假定该文件已经是
纯数值数组的产物、不是仿真中间态（该假设与 `collect_signals.m` 当前实际落盘的
`Simulink.SimulationData.Dataset` 格式之间的差距，见 `_load_waveform()` 的
docstring——这是一个已知但不在本任务范围内解决的架构缺口）。

## 窗口符号解析约定

`TimeDomainMetricSpec.window` / `ConstraintObservableSpec.window` 是
`tuple[str, str]` 符号标签，`configs/metrics.yaml` 实际使用的取值域为：
`sim_start` / `sim_end` / `step_trigger` / `step_trigger_plus_500us` /
`steady_start` / `steady_end`。`_resolve_time_label()` 把这些符号换算为绝对秒：

- `sim_start` -> `Waveform.sim_start_s`；`sim_end` -> `Waveform.sim_end_s`（`None`
  时取该信号最后采样时刻）。
- `step_trigger` -> `Waveform.step_trigger_s`；`step_trigger_plus_<N>us` ->
  `step_trigger_s + N microseconds`（`N` 从标签文本本身解析，不是硬编码字面
  量）；`step_trigger_s` 为 `None` 时视为「阶跃触发点未识别」。
- `steady_start` / `steady_end`：`design.md` / `metrics.yaml` 都没有给出稳态窗口
  起止的精确公式。本模块采用「仿真末 10% 窗口」这一约定
  （`_STEADY_WINDOW_TAIL_FRACTION = 0.10`）——与 `waveform_not_converged` 判据
  描述的「仿真末 10% 窗口的 Vout 峰峰值」同一惯例复用，即
  `steady_start = sim_end - 0.10 * (sim_end - sim_start)`、`steady_end = sim_end`。
  **这是一个待真实波形可用后校准的临时解释，不是已确认的设计结论**，此处显式
  标注供后续复核。

## 已知架构缺口：`vout_target_v` 未被 `MetricSpec` / `compute_metrics()` 携带

`overshoot` / `undershoot` 的取值定义直接是 `vout_target_v ± Vout` 峰值之差；
`settling_time.band` 在 `metrics.yaml` 中写作符号形式 `"0.01_of_vout_target"`，
同样需要 `vout_target_v`（`model.yaml` 的 `io_contract.vout_target_v`）才能换算
成绝对伏特误差带。但 `design.md` §6.5.1 给出的 `compute_metrics()` 签名
（`waveform_ref, scenario, metrics_cfg, *, run_id, guard`）与三个指标函数签名
（`(w, spec) -> MetricResult`）都不包含这个值，`TimeDomainMetricSpec` 本身也没有
`vout_target_v` 字段。

本模块的处理方式：`compute_metrics()` 与 `settling_time` / `overshoot` /
`undershoot` 三个函数都新增一个**关键字参数** `vout_target_v: float`——这与
`compute_metrics()` 已有的 `guard: DivergenceGuard` 完全同构：`guard` 同样是
从 `model.yaml` 的 `io_contract.divergence_guard` 单独摘出、以关键字参数形式
传入，而不是要求 `compute_metrics()` 接收整份 `ModelConfig`。`vout_target_v`
照此先例处理。`settling_time.band` 的符号形式 `"<ratio>_of_vout_target"` 由
`_resolve_settling_band()` 用一个通用的正则解析（不是针对某个具体比例的字面量）
换算为绝对值，因此不需要额外的上游预解析步骤。

**真正尚未闭环的部分**：现在还没有任何模块负责在真实调用时把
`model.yaml.io_contract.vout_target_v` 读出来并传给 `compute_metrics()`——
`compute_metrics()` 的真实调用点属于 controller（`design.md` 任务
14.x／`run_task()` 编排），该调用点目前尚未搭建。本模块在此把
`vout_target_v` 登记为 `compute_metrics()` 签名的显式必填关键字参数，作为对
上游调用点的一个清楚契约；这不是「无法解决」的缺口，而是一个需要在 T14.x
接线时对齐的、已被本次实现直接暴露出来的参数。

## 已知架构缺口：`output_ripple.filter`（带宽 Hz）的占位符解析

`output_ripple.filter` 在 `metrics.yaml` 模板中仍是物理数值占位符（M0 待填的
带宽 Hz），与 `vout_target_v` 不同，这个值**没有**可从其他已知字段推导出的
符号约定——它就是一个独立的物理量，本模块无法替上游把它填出来。`_apply_filter()`
的处理方式：`spec.filter == "none"` 时不滤波；`spec.filter` 是可解析为浮点数的
字符串时，按该值（Hz，简单滑动平均近似低通，非精确 Butterworth，见函数内文档）
滤波；除此之外（仍是占位符或任何其他非数值字符串）抛出 `ValueError`，清楚指出
这不是本函数的职责范围——正常情况下 `"<...>"` 占位符会被 `config/loader.py`
的预检拦在更早的阶段（不会进入到这里），因此这个异常分支预期只在配置被绕过
预检直接构造时才会触发。

## `INVALID_PREDICATES`：8 个具名无效谓词的查表注册表（任务 10.2）

`design.md` §6.5.1 与 `metrics.yaml` 的 `invalid_if` 枚举一一对应的 8 个具名
谓词函数（`_pred_waveform_unreadable` ... `_pred_extraction_failed`），签名统一
为 `(w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard) -> bool`，
登记进 `INVALID_PREDICATES: Mapping[str, InvalidPredicate]`；`_check_invalid()`
按 `spec.invalid_if` 声明的子列表依次查表调用，返回首个命中枚举值，不做任何
表达式解析或求值。

**两项谓词因缺 `vout_target_v` 而恒不触发（沿用任务 10.1 已标注的架构缺口）**：

- `_pred_waveform_not_converged`：判据「仿真末 10% 窗口的 Vout 峰峰值大于稳态
  判定带」只需要误差带的**宽度**（`2 × band`），不需要 `vout_target_v` 本身，
  因此当 `spec.band` 已是可解析的绝对伏特数值字符串时本谓词可以正常判定；但
  `metrics.yaml` 实际写的是符号形式 `"0.01_of_vout_target"`（需要
  `vout_target_v` 才能换算出绝对值），而本谓词签名不带 `vout_target_v`，故此
  时退化为恒不触发（返回 `False`），不阻断其余指标——这与任务 10.1 对
  `settling_time.band` 缺口的处理是同一个未闭环的上游参数传递问题，不是本
  谓词自身的判据错误。
- `_pred_not_settled_within_window`：判据是「窗口内某时刻起持续落在
  `[vout_target - band, vout_target + band]` 内直到窗口末尾」，这个误差带的
  **中心**就是 `vout_target_v` 本身，无法像 `waveform_not_converged` 那样只用
  宽度绕过，因此本谓词在当前签名下**恒返回 `False`**（明确的已知限制，见
  函数自身 docstring）。真正的 `not_settled_within_window` 判定继续由
  `settling_time()` 自身完成（它拿到了 `vout_target_v` 关键字参数），经共享的
  `_find_settle_index()` 辅助函数产出——`settling_time()` 不经过
  `INVALID_PREDICATES` 这条路径。

`_pred_extraction_failed` 对 `TimeDomainMetricSpec` 恒返回 `False`——该谓词仅
适用于 `phase_margin` / `gain_margin`（`MarginMetricSpec`），属于 `margin.py`
（任务 11.x）的判定范围，登记进本模块的 `INVALID_PREDICATES` 只是为了满足「8
个谓词一一对应」的完整性要求，`eval/metrics.py` 自身的调用路径不会用到它。

`no_step_detected` 与 `not_settled_within_window` 因此有**两套并存的判定入口**
（`_check_invalid()` 经 `INVALID_PREDICATES` 查表 / `settling_time` `overshoot`
`undershoot` 函数体内自带的 `_StepNotDetected` 捕获与结算点搜索），刻意保留
两者而非只留一套：这三个指标函数的公开签名（`design.md` §6.5.1）只接受
`(w, spec)`、不接受 `guard`，意味着它们必须能在完全脱离 `_check_invalid()` /
`compute_metrics()` 调用路径（例如任务 10.3 的 Metric Fixture 测试直接调用某个
指标函数）时仍独立给出正确的 `no_step_detected` / `not_settled_within_window`
判定，不能依赖「上游已经查过表」这个前提。经 `compute_metrics()` 正常路径调用
时，只要该指标的 `invalid_if` 声明了这两项（`metrics.yaml` 模板确实如此），
`_check_invalid()` 会先于指标函数本身命中，函数体内对应分支实际上不会被走到,
但这是无害的防御性冗余，不是需要清理的死代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from poweragent.config.schema import (
    ConstraintObservableSpec,
    DivergenceGuard,
    MetricsConfig,
    TimeDomainMetricSpec,
)
from poweragent.store.repo import MetricResult, ScenarioSpec

__all__ = [
    "Waveform",
    "InvalidPredicate",
    "INVALID_PREDICATES",
    "compute_metrics",
    "output_ripple",
    "overshoot",
    "undershoot",
    "settling_time",
    "phase_peak_current",
    "observable",
    "IMPLEMENTED_TIME_DOMAIN_METRICS",
]


# ===========================================================================
# Waveform：已加载波形的最小数据持有者（本模块自行设计，见模块 docstring）
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Waveform:
    """已从 `waveform_ref` 加载/提取的信号数据；不含 `.slx` 路径、
    `SimulationInput`、`logsout` 等仿真态概念——`eval` 层只消费已提取的时间序列
    数组。字段含义见模块 docstring「`Waveform`：已加载波形的最小数据持有者」。
    """

    signals: Mapping[str, tuple[np.ndarray, np.ndarray]]
    step_trigger_s: float | None = None
    sim_start_s: float = 0.0
    sim_end_s: float | None = None


class _StepNotDetected(Exception):
    """内部信号：窗口符号解析过程中需要 `step_trigger` 但
    `Waveform.step_trigger_s` 为 `None`。不对外暴露，调用方按需转译为
    `invalid_reason='no_step_detected'`。"""


# 稳态窗口（`steady_start`/`steady_end`）采用的「仿真末 10% 窗口」惯例，与
# `waveform_not_converged` 判据描述的惯例一致（design.md §4.3 表格）。见模块
# docstring 的「窗口符号解析约定」一节：这是待真实波形校准的临时解释。
_STEADY_WINDOW_TAIL_FRACTION = 0.10

_STEP_TRIGGER_PLUS_PATTERN = re.compile(r"^step_trigger_plus_(\d+)us$")


def _resolve_time_label(label: str, w: Waveform, signal_end_time: float) -> float:
    """把 `window` 元组中的一个符号标签换算为绝对仿真时间（秒）。

    `signal_end_time` 是被访问信号自身最后一个采样时刻，供 `sim_end` 在
    `Waveform.sim_end_s is None` 时兜底（不同信号采样长度可能不同，不能定死为
    一个跨信号共享的模块级常量）。
    """

    if label == "sim_start":
        return w.sim_start_s
    if label == "sim_end":
        return w.sim_end_s if w.sim_end_s is not None else signal_end_time
    if label == "step_trigger":
        if w.step_trigger_s is None:
            raise _StepNotDetected()
        return w.step_trigger_s

    plus_match = _STEP_TRIGGER_PLUS_PATTERN.match(label)
    if plus_match:
        if w.step_trigger_s is None:
            raise _StepNotDetected()
        offset_us = int(plus_match.group(1))
        return w.step_trigger_s + offset_us * 1e-6

    if label in ("steady_start", "steady_end"):
        sim_end = w.sim_end_s if w.sim_end_s is not None else signal_end_time
        steady_start = sim_end - _STEADY_WINDOW_TAIL_FRACTION * (sim_end - w.sim_start_s)
        return steady_start if label == "steady_start" else sim_end

    raise ValueError(f"未知的窗口符号标签: {label!r}")


def _window_signal(
    w: Waveform, signal: str, window: tuple[str, str]
) -> tuple[np.ndarray, np.ndarray] | None:
    """按 `window` 符号标签截取 `signal` 对应的 `(time, data)`；`signal` 不存在
    时返回 `None`（由调用方决定归为 `signal_missing` 还是其他谓词）。可能抛出
    `_StepNotDetected`（调用方需捕获并转译）。"""

    if signal not in w.signals:
        return None
    time, data = w.signals[signal]
    end_time = float(time[-1]) if len(time) else 0.0
    t0 = _resolve_time_label(window[0], w, end_time)
    t1 = _resolve_time_label(window[1], w, end_time)
    mask = (time >= t0) & (time <= t1)
    return time[mask], data[mask]


# ===========================================================================
# 8 个具名无效谓词与 `INVALID_PREDICATES` 查表注册表（任务 10.2；design.md
# §6.5.1；需求追溯 R7.2, R7.4, R7.8, R7.9）
# ===========================================================================

# 统一签名类型别名：design.md §6.5.1 给出的抽象 `MetricSpec` 在本代码库中的
# 具体类型是 `TimeDomainMetricSpec`（`_pred_extraction_failed` 语义上适用于
# `MarginMetricSpec`，但为满足「8 个函数签名统一」的要求仍声明为同一别名，见
# 该函数自身 docstring）。
InvalidPredicate = Callable[[Waveform, TimeDomainMetricSpec, DivergenceGuard], bool]


def _pred_waveform_unreadable(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """整段波形（非窗口切片）不可读：`spec.signal` 存在但其数据为空或含非有限
    值（Inf/NaN）。`spec.signal` 根本不存在属于 `_pred_signal_missing` 的职责，
    本谓词在该情形下返回 `False`（不越权重复判定，避免两个谓词在同一输入上都
    命中导致 `_check_invalid()` 的「首个命中」顺序产生歧义）。"""

    if spec.signal not in w.signals:
        return False
    time, data = w.signals[spec.signal]
    return len(time) == 0 or len(data) == 0 or not np.all(np.isfinite(data))


def _pred_signal_missing(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """`spec.signal` 指定的信号在波形产物中不存在。"""

    return spec.signal not in w.signals


def _pred_window_empty(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """窗口符号标签**成功解析**但截取结果零采样点。

    刻意**不**把 `_StepNotDetected`（窗口引用了 `step_trigger` 系标签但
    `Waveform.step_trigger_s is None`，即阶跃触发点本身未知）算作
    `window_empty`——两者是 `metrics.yaml` 枚举里语义不同的失败原因：「窗口能
    确定、但窗口内恰好没有采样点」vs「窗口本身无法确定」。后者交给
    `_pred_no_step_detected` 判定，本谓词遇到该异常时返回 `False`（不越权）。
    `spec.signal` 不存在时 `_window_signal()` 返回 `None`，同样不算 `window_empty`
    （交给 `_pred_signal_missing`），本谓词返回 `False`。
    """

    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        return False
    return windowed is not None and len(windowed[0]) == 0


def _pred_waveform_not_converged(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """仿真末 10% 窗口（复用 `_STEADY_WINDOW_TAIL_FRACTION`）的 `spec.signal`
    峰峰值大于稳态判定带。「稳态判定带」取 `spec.band`：误差带定义为
    `[vout_target - band, vout_target + band]`，宽度为 `2 * band`——峰峰值
    (`max - min`) 与这个宽度是同一量纲下可直接比较的两个数，因此判据为
    `峰峰值 > 2 * band`（不需要 `vout_target_v` 本身，只需要带宽）。

    **已知限制**：`spec.band` 在 `metrics.yaml` 中实际写作符号形式
    `"<ratio>_of_vout_target"`（需 `vout_target_v` 才能换算为绝对伏特值），
    而本谓词签名（design.md §6.5.1 锁定）不携带 `vout_target_v`。`spec.band`
    仍是该符号形式、无法解析为纯数值时，本谓词返回 `False`（不阻断其余指标，
    也不臆造一个默认带宽）——这是任务 10.1 已标注的 `vout_target_v` 架构缺口
    在本谓词上的延伸，不是本次实现的判据错误，见模块 docstring。`spec.signal`
    不存在或窗口为空时同样返回 `False`（交给 `_pred_signal_missing` /
    `_pred_window_empty` 判定）。
    """

    if spec.signal not in w.signals:
        return False
    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        return False
    if windowed is None or len(windowed[0]) == 0:
        return False
    time, data = w.signals[spec.signal]
    end_time = float(time[-1]) if len(time) else 0.0
    sim_end = w.sim_end_s if w.sim_end_s is not None else end_time
    tail_start = sim_end - _STEADY_WINDOW_TAIL_FRACTION * (sim_end - w.sim_start_s)
    mask = (time >= tail_start) & (time <= sim_end)
    tail_data = data[mask]
    if len(tail_data) == 0:
        return False

    band_str = spec.band
    if band_str is None:
        return False
    try:
        band = float(band_str)
    except ValueError:
        return False  # 仍是符号形式（如 "0.01_of_vout_target"），无 vout_target_v 无法换算

    peak_to_peak = float(np.max(tail_data) - np.min(tail_data))
    return peak_to_peak > 2.0 * band


def _pred_vout_out_of_guard(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """`spec.signal` 的**全信号**（非窗口切片）绝对值越过
    `guard.vout_abs_max`——与任务 10.1 既有实现的判定范围保持一致（全信号，
    不是窗口内数据）。`spec.signal` 不存在时返回 `False`（交给
    `_pred_signal_missing`）。"""

    if spec.signal not in w.signals:
        return False
    _, data = w.signals[spec.signal]
    return bool(np.any(np.abs(data) > guard.vout_abs_max))


_STEP_TRIGGER_DEPENDENT_LABELS = ("step_trigger",)


def _window_needs_step_trigger(window: tuple[str, str]) -> bool:
    return any(
        label in _STEP_TRIGGER_DEPENDENT_LABELS
        or _STEP_TRIGGER_PLUS_PATTERN.match(label)
        for label in window
    )


def _pred_no_step_detected(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """`spec.window` 引用了 `step_trigger` 或 `step_trigger_plus_<N>us` 标签，
    且 `Waveform.step_trigger_s is None`（阶跃触发点未识别）。直接检查窗口标签
    文本与 `step_trigger_s` 字段，不调用 `_window_signal()` / 不捕获
    `_StepNotDetected`——本谓词的判据本身就是该异常将会被抛出的前提条件，没有
    必要先触发再捕获。"""

    return _window_needs_step_trigger(spec.window) and w.step_trigger_s is None


def _pred_not_settled_within_window(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """窗口内不存在「进入误差带后不再离开、直到窗口末尾」的时刻。

    **已知限制**：判据的误差带为 `[vout_target - band, vout_target + band]`，
    需要 `vout_target_v` 本身（不是只需要带宽——`waveform_not_converged` 可以
    绕开 `vout_target_v` 是因为它只比较峰峰值与带宽，本谓词比较的是信号与一个
    以 `vout_target_v` 为中心的区间，无法同样绕开）。本谓词签名不携带
    `vout_target_v`，因此**恒返回 `False`**（不臆造默认中心值）。真正的
    `not_settled_within_window` 判定由 `settling_time()` 完成（它拿到了
    `vout_target_v` 关键字参数），经共享的 `_find_settle_index()` 辅助函数
    产出，见模块 docstring「`INVALID_PREDICATES`」一节。本谓词登记进
    `INVALID_PREDICATES` 只为满足「8 个谓词一一对应」的完整性要求。
    """

    return False


def _pred_extraction_failed(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> bool:
    """恒返回 `False`：该判据仅适用于 `phase_margin` / `gain_margin`
    （`MarginMetricSpec`，不是本模块处理的 `TimeDomainMetricSpec`），属于
    `margin.py`（任务 11.x）自身的控制流（频响采集或提取失败时直接产出
    `invalid_reason='extraction_failed'`），不经过本模块的 `INVALID_PREDICATES`
    查表。登记进注册表只为满足「8 个谓词一一对应」的完整性要求，
    `eval/metrics.py` 自身的调用路径（`_check_invalid()`）不会遇到
    `TimeDomainMetricSpec.invalid_if` 含 `'extraction_failed'` 的情形——
    `config/schema.py` 的 `_INVALID_IF_ALLOWED_BY_METRIC` 已把该谓词限定为
    仅 `phase_margin` / `gain_margin` 可声明。"""

    return False


INVALID_PREDICATES: Mapping[str, InvalidPredicate] = {
    "waveform_unreadable": _pred_waveform_unreadable,
    "signal_missing": _pred_signal_missing,
    "window_empty": _pred_window_empty,
    "waveform_not_converged": _pred_waveform_not_converged,
    "vout_out_of_guard": _pred_vout_out_of_guard,
    "no_step_detected": _pred_no_step_detected,
    "not_settled_within_window": _pred_not_settled_within_window,
    "extraction_failed": _pred_extraction_failed,
}


def _check_invalid(
    w: Waveform, spec: TimeDomainMetricSpec, guard: DivergenceGuard
) -> str | None:
    """按 `spec.invalid_if` 中声明的枚举值依次查 `INVALID_PREDICATES`、调用对应
    谓词函数，返回首个命中的枚举值；全部不命中返回 `None`。**不做任何表达式
    解析或求值**——`spec.invalid_if` 本身就是枚举值列表，本函数只是「按声明顺序
    查表调用」。"""

    for name in spec.invalid_if:
        if INVALID_PREDICATES[name](w, spec, guard):
            return name
    return None


# ===========================================================================
# 五项时域指标
# ===========================================================================

_SYMBOLIC_BAND_PATTERN = re.compile(r"^([0-9]*\.?[0-9]+)_of_vout_target$")


def _resolve_settling_band(band_spec: str | None, vout_target_v: float) -> float:
    """把 `TimeDomainMetricSpec.band` 换算为绝对伏特误差带。

    支持两种形式：`"<ratio>_of_vout_target"`（`metrics.yaml` 实际使用的符号
    形式，`ratio` 从文本本身解析，不是硬编码字面量）；或可直接解析为浮点数的
    绝对伏特字符串（已在上游预先解析好的情形）。除此之外（包括残留的
    `"<...>"` 占位符——正常应已被 `config/loader.py` 的预检拦下）抛出
    `ValueError`。
    """

    if band_spec is None:
        raise ValueError("settling_time: spec.band 为空，无法确定误差带")

    symbolic = _SYMBOLIC_BAND_PATTERN.match(band_spec)
    if symbolic:
        return float(symbolic.group(1)) * vout_target_v

    try:
        return float(band_spec)
    except ValueError as exc:
        raise ValueError(
            f"settling_time: spec.band={band_spec!r} 既不是 "
            "'<ratio>_of_vout_target' 符号形式，也不是可解析的绝对伏特数值"
        ) from exc


def _apply_filter(time: np.ndarray, data: np.ndarray, filter_spec: str) -> np.ndarray:
    """按 `spec.filter` 对窗口内数据做低通滤波；`"none"` 时原样返回。

    数值字符串（Hz 带宽）时用一个简单滑动平均近似低通（不是精确的
    Butterworth/IIR 设计——`output_ripple` 只需要「压掉开关纹波高频分量、留下
    低频包络」这个粗粒度效果，选用滑动平均是为了实现简单、行为可预测；若后续
    需要更精确的滤波器设计，替换本函数即可，不影响调用方）。非 `"none"` 且不可
    解析为浮点数时抛出 `ValueError`（见模块 docstring 的「已知架构缺口」）。
    """

    if filter_spec == "none":
        return data

    try:
        bandwidth_hz = float(filter_spec)
    except ValueError as exc:
        raise ValueError(
            f"output_ripple: spec.filter={filter_spec!r} 既非 'none' 也不是可"
            " 解析的数值(Hz) —— 符号化/占位带宽的解析不是本函数职责"
        ) from exc

    if bandwidth_hz <= 0 or len(data) < 3 or len(time) < 2:
        return data

    dt = float(np.median(np.diff(time)))
    if dt <= 0:
        return data

    window_samples = max(1, int(round(1.0 / (bandwidth_hz * dt))))
    window_samples = min(window_samples, len(data))
    if window_samples <= 1:
        return data

    kernel = np.ones(window_samples) / window_samples
    return np.convolve(data, kernel, mode="same")


def output_ripple(w: Waveform, spec: TimeDomainMetricSpec, *, run_id: str) -> MetricResult:
    """稳态窗口内 Vout 峰峰值（V）。窗口 `[steady_start, steady_end]`，按
    `spec.filter` 滤波后取 `max - min`。只读 `spec.signal` / `spec.window` /
    `spec.filter`。"""

    metric_id = "output_ripple"
    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        windowed = None
    if windowed is None or len(windowed[0]) == 0:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="window_empty",
        )
    time, data = windowed
    filtered = _apply_filter(time, data, spec.filter)
    value = float(np.max(filtered) - np.min(filtered))
    return MetricResult(run_id=run_id, metric_id=metric_id, value=value, valid=True)


def overshoot(
    w: Waveform, spec: TimeDomainMetricSpec, *, run_id: str, vout_target_v: float
) -> MetricResult:
    """阶跃窗口内 `max(Vout) - vout_target`（V），非正时记 0（无超调）。窗口
    `[step_trigger, step_trigger_plus_500us]`，`spec.filter='none'`。阶跃触发点
    未识别（`Waveform.step_trigger_s is None`）⟹ `no_step_detected`。只读
    `spec.signal` / `spec.window`。"""

    metric_id = "overshoot"
    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="no_step_detected",
        )
    if windowed is None or len(windowed[0]) == 0:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="window_empty",
        )
    _, data = windowed
    delta = float(np.max(data) - vout_target_v)
    return MetricResult(run_id=run_id, metric_id=metric_id, value=max(delta, 0.0), valid=True)


def undershoot(
    w: Waveform, spec: TimeDomainMetricSpec, *, run_id: str, vout_target_v: float
) -> MetricResult:
    """阶跃窗口内 `vout_target - min(Vout)`（V），非正时记 0（无欠冲）。与
    `overshoot` 对称，同样的窗口/无滤波/`no_step_detected` 规则。"""

    metric_id = "undershoot"
    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="no_step_detected",
        )
    if windowed is None or len(windowed[0]) == 0:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="window_empty",
        )
    _, data = windowed
    delta = float(vout_target_v - np.min(data))
    return MetricResult(run_id=run_id, metric_id=metric_id, value=max(delta, 0.0), valid=True)


def _find_settle_index(
    win_time: np.ndarray, win_data: np.ndarray, lower: float, upper: float
) -> int | None:
    """在窗口内找到第一个「从此刻起持续落在 `[lower, upper]` 内直到窗口末尾」
    的采样点索引；不存在这种「进入后不再离开」的时刻时返回 `None`。

    抽出为独立函数是为了让 `settling_time()` 与
    `_pred_not_settled_within_window()` 共用同一份结算点搜索逻辑而不重复实现
    该循环（见模块 docstring「`INVALID_PREDICATES`」一节）；但
    `_pred_not_settled_within_window()` 因签名不携带 `vout_target_v`、无法
    自行算出 `lower`/`upper`，实际恒不调用本函数、恒返回 `False`——本函数目前
    只有 `settling_time()` 这一个实际调用方，保留「供两处共用」的命名与签名是
    为了一旦 `vout_target_v` 缺口在未来被接线闭合，`_pred_not_settled_within_window`
    可以直接复用本函数而不需要再抽一次。
    """

    inside = (win_data >= lower) & (win_data <= upper)
    for i in range(len(inside)):
        if bool(np.all(inside[i:])):
            return i
    return None


def settling_time(
    w: Waveform, spec: TimeDomainMetricSpec, *, run_id: str, vout_target_v: float
) -> MetricResult:
    """负载阶跃后回到 `vout_target ± band` 并保持到窗口末尾所需时间（us）。
    窗口 `[step_trigger, step_trigger_plus_500us]`。若窗口内存在某个时刻之后
    信号持续（直到窗口末尾）落在误差带内，取该时刻相对 `step_trigger` 的偏移；
    若从未出现这种「进入后不再离开」的时刻，记 `not_settled_within_window`。
    阶跃触发点未识别 ⟹ `no_step_detected`。只读 `spec.signal` / `spec.window` /
    `spec.band`。"""

    metric_id = "settling_time"
    if w.step_trigger_s is None:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="no_step_detected",
        )

    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="no_step_detected",
        )
    if windowed is None or len(windowed[0]) == 0:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="window_empty",
        )
    win_time, win_data = windowed

    band = _resolve_settling_band(spec.band, vout_target_v)
    lower, upper = vout_target_v - band, vout_target_v + band
    settle_index = _find_settle_index(win_time, win_data, lower, upper)

    if settle_index is None:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="not_settled_within_window",
        )

    settle_time_s = float(win_time[settle_index]) - w.step_trigger_s
    value_us = settle_time_s * 1e6
    return MetricResult(run_id=run_id, metric_id=metric_id, value=value_us, valid=True)


def phase_peak_current(
    w: Waveform, spec: TimeDomainMetricSpec, *, run_id: str
) -> MetricResult:
    """全仿真窗口内 Iphase 绝对值的最大值（A），按 `aggregation:
    max_over_phases` 聚合为单值——`Iphase` 数据为形状 `(n_time, n_phase)` 的
    2 维数组时，`np.max(np.abs(...))` 同时完成「跨相」与「跨时间窗口」两层聚合。
    窗口 `[sim_start, sim_end]`。只读 `spec.signal` / `spec.window`。"""

    metric_id = "phase_peak_current"
    if spec.signal not in w.signals:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="signal_missing",
        )
    try:
        windowed = _window_signal(w, spec.signal, spec.window)
    except _StepNotDetected:
        windowed = None
    if windowed is None or len(windowed[0]) == 0:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="window_empty",
        )
    _, data = windowed
    value = float(np.max(np.abs(data)))
    return MetricResult(run_id=run_id, metric_id=metric_id, value=value, valid=True)


# ===========================================================================
# `constraint_observables` 的统一实现
# ===========================================================================


def observable(
    w: Waveform, spec: ConstraintObservableSpec, *, run_id: str, metric_id: str
) -> MetricResult:
    """`constraint_observables` 的统一实现：窗口 + `aggregation(min|max)`，
    无滤波、无阶跃识别、不检查 `invalid_if`（`ConstraintObservableSpec` 本身不
    含 `invalid_if` 字段）。`metric_id` 由调用方传入（`"obs.vout_min"` /
    `"obs.vout_max"`），本函数不硬编码具体观测量名。恒定计算，不受
    `active_metrics` 开关影响——调用方（`compute_metrics()`）对两项观测量总是
    调用本函数。"""

    if spec.signal not in w.signals:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="signal_missing",
        )
    windowed = _window_signal(w, spec.signal, spec.window)
    if windowed is None or len(windowed[0]) == 0:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason="window_empty",
        )
    _, data = windowed
    value = float(np.min(data)) if spec.aggregation == "min" else float(np.max(data))
    return MetricResult(run_id=run_id, metric_id=metric_id, value=value, valid=True)


# ===========================================================================
# compute_metrics：同一次波形遍历产出两组结果
# ===========================================================================

# active_metrics 中有对应计算函数的 metric_id 集合；phase_margin/gain_margin 由
# margin.py（任务 11.x）提供，不在本模块处理范围内（design.md §6.5.1 docstring
# 「phase_margin / gain_margin 由 margin.py 提供」）。
_TIME_DOMAIN_METRIC_IDS = frozenset(
    {"settling_time", "output_ripple", "overshoot", "undershoot", "phase_peak_current"}
)

# 公开别名：供 `controller/preflight.py`（任务 10.5）的 `metric_not_implemented`
# 断言判断「哪些 metric_id 在本模块有对应具名计算函数」，不重复定义同一份
# 5 元素集合（避免与上面的 `_TIME_DOMAIN_METRIC_IDS` 在两处维护而产生漂移）。
# 这是对本模块既有私有集合的一个新增公开别名，不改变 `_TIME_DOMAIN_METRIC_IDS`
# 本身的可见性与用法，向后兼容。
IMPLEMENTED_TIME_DOMAIN_METRICS = _TIME_DOMAIN_METRIC_IDS

# 需要 vout_target_v 才能计算的三项指标（见模块 docstring 的「已知架构缺口」）。
_NEEDS_VOUT_TARGET = frozenset({"settling_time", "overshoot", "undershoot"})


def _compute_one_time_domain_metric(
    metric_id: str,
    w: Waveform,
    spec: TimeDomainMetricSpec,
    *,
    run_id: str,
    guard: DivergenceGuard,
    vout_target_v: float,
) -> MetricResult:
    """先做 `_check_invalid()` 的通用无效判定（需要 `guard`），命中即直接产出
    无效结果、不调用具体指标函数；否则分发给对应的具名指标函数（该函数内部还会
    判定 `no_step_detected` / `not_settled_within_window` 这两个指标计算本身
    固有的条件）。"""

    invalid_reason = _check_invalid(w, spec, guard)
    if invalid_reason is not None:
        return MetricResult(
            run_id=run_id, metric_id=metric_id, value=None, valid=False,
            invalid_reason=invalid_reason,
        )

    if metric_id == "output_ripple":
        return output_ripple(w, spec, run_id=run_id)
    if metric_id == "phase_peak_current":
        return phase_peak_current(w, spec, run_id=run_id)
    if metric_id == "overshoot":
        return overshoot(w, spec, run_id=run_id, vout_target_v=vout_target_v)
    if metric_id == "undershoot":
        return undershoot(w, spec, run_id=run_id, vout_target_v=vout_target_v)
    if metric_id == "settling_time":
        return settling_time(w, spec, run_id=run_id, vout_target_v=vout_target_v)
    raise AssertionError(f"unreachable metric_id: {metric_id!r}")  # pragma: no cover


def compute_metrics(
    waveform_ref: str,
    scenario: ScenarioSpec,
    metrics_cfg: MetricsConfig,
    *,
    run_id: str,
    guard: DivergenceGuard,
    vout_target_v: float,
) -> list[MetricResult]:
    """同一次波形遍历中产出两组结果（design.md §6.5.1）：

    - `active_metrics` 中每个时域指标恰好一条以 `(run_id, metric_id)` 唯一
      标识的 `MetricResult`（受开关控制，`metric_id` 不在
      `_TIME_DOMAIN_METRIC_IDS` 内的 `active_metrics` 项——如 `phase_margin`/
      `gain_margin`/未实现的指标——本函数天然不产出结果，`metric_
      not_implemented` 的 preflight 断言是任务 10.5 的职责，本函数不重复做
      防御性检查）。
    - `metrics_cfg.constraint_observables` 的两项观测量恰各一条
      `metric_id` 带 `obs.` 前缀的 `MetricResult`（`obs.vout_min` /
      `obs.vout_max`），恒定计算，不受 `active_metrics` 开关影响。

    `vout_target_v` 为本模块相对 `design.md` §6.5.1 签名新增的关键字参数
    （见模块 docstring 的「已知架构缺口」一节），与既有的 `guard` 参数同构：
    均是从 `model.yaml` 单独摘出、以关键字参数形式传入的物理值。

    `waveform_ref` 只加载一次（`_load_waveform()`），后续全部指标与观测量的
    计算共享同一个 `Waveform` 实例，满足「同一次波形遍历」的要求。
    """

    w = _load_waveform(waveform_ref)
    results: list[MetricResult] = []

    for metric_id in metrics_cfg.active_metrics:
        if metric_id not in _TIME_DOMAIN_METRIC_IDS:
            continue
        spec: TimeDomainMetricSpec = getattr(metrics_cfg.metrics, metric_id)
        results.append(
            _compute_one_time_domain_metric(
                metric_id, w, spec, run_id=run_id, guard=guard, vout_target_v=vout_target_v
            )
        )

    for obs_metric_id, obs_spec in (
        ("obs.vout_min", metrics_cfg.constraint_observables.vout_min),
        ("obs.vout_max", metrics_cfg.constraint_observables.vout_max),
    ):
        results.append(observable(w, obs_spec, run_id=run_id, metric_id=obs_metric_id))

    return results


# ===========================================================================
# 私有：波形加载（本模块唯一触碰文件 I/O 的函数）
# ===========================================================================


def _load_waveform(waveform_ref: str) -> Waveform:
    """把 `waveform_ref`（指向已落盘产物的路径字符串）加载为结构化的
    `Waveform`。这是本模块**唯一**触碰文件 I/O 的函数，且只读取「已经是数值
    数组」的产物内容——不涉及 `.slx`、`SimulationInput`、`logsout` 等仿真态
    概念（那些是 MATLAB 侧 `matlab/+pa/simulate_once.m` /
    `collect_signals.m` 的职责，运行期早已结束）。

    **已知架构缺口（不在本任务 T10.1 范围内解决）**：`collect_signals.m`
    落盘的 MAT 文件里唯一变量是 `logsout`，其类型为
    `Simulink.SimulationData.Dataset`——一个 MATLAB 类对象；`scipy.io.loadmat`
    读取这类对象通常得到不透明的结构化数组，不能直接还原成
    `{signal_name: (time, data)}` 映射。本函数按一个**假定的、更简单的 MAT
    数据形状**实现（每个信号名是一个顶层变量，值为含 `time`/`data` 字段的
    结构），这与 `collect_signals.m` 当前实际写出的格式不一致。真正弥合这个
    差距——无论是新增一个 MATLAB 侧转换步骤，还是引入能解析
    `Simulink.SimulationData.Dataset` 的 Python 库——不属于本任务范围，本函数
    只是一个占位实现，供尚未搭建的上游波形加载步骤（可能落在
    `sim/simulate.py` 或一个专门的 loader 模块）最终替换。`compute_metrics()`
    的核心数值逻辑（本任务的实际交付物）不依赖这个占位实现的正确性——验证时
    直接构造 `Waveform` 实例、绕开本函数（见任务报告的验证部分）。
    """

    import scipy.io as sio

    raw = sio.loadmat(waveform_ref, squeeze_me=True, struct_as_record=False)

    signals: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    step_trigger_s: float | None = None
    sim_start_s = 0.0
    sim_end_s: float | None = None

    for key, value in raw.items():
        if key.startswith("__"):
            continue
        if key == "step_trigger_s":
            step_trigger_s = float(value)
            continue
        if key == "sim_start_s":
            sim_start_s = float(value)
            continue
        if key == "sim_end_s":
            sim_end_s = float(value)
            continue
        time_arr = np.asarray(getattr(value, "time", value))
        data_arr = np.asarray(getattr(value, "data", value))
        signals[str(key)] = (time_arr, data_arr)

    return Waveform(
        signals=signals,
        step_trigger_s=step_trigger_s,
        sim_start_s=sim_start_s,
        sim_end_s=sim_end_s,
    )
