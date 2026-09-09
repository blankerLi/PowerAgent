"""poweragent/eval/constraints.py

硬约束判定（`design.md` §6.5.3；`tasks.md` 任务 12.1；需求追溯 R9.1, R9.2, R9.3,
R9.4, R9.10）。

本模块只实现 `judge()`；`aggregate.py` 的 `worst_case()` / `sort_key()` / `rank()`
是任务 12.2 / 12.3 的范围，不在本文件。

## `judge()` 的四条硬约束绑定（R9.1）

`constraints.yaml` 的 `hard_constraints` 恰四条（`vout_min` / `vout_max` /
`peak_current_max` / `phase_margin_min`），绑定关系固定为：

| 约束名 | `observable`（取自 `constraints.yaml`） | `sense` |
| --- | --- | --- |
| `vout_min` | `obs.vout_min` | `lower` |
| `vout_max` | `obs.vout_max` | `upper` |
| `peak_current_max` | `phase_peak_current` | `upper` |
| `phase_margin_min` | `phase_margin` | `lower` |

这四行绑定**不是**在本模块硬编码的字符串映射——`HardConstraintEntry.observable`
本身就是 `constraints.yaml` 里的一个配置字段，其取值已经等于上表右列（见
`configs/constraints.yaml` 模板）。本函数只读取 `entry.observable` 作为
`metrics_by_id` 的查找键，四路绑定由配置内容自然产生，不在 Python 代码里重复
声明这份对应关系。这样做的好处是绑定关系的唯一权威来源始终是配置文件，代码
不会与配置产生第二份可能漂移的副本。

## 不读 `model_variant`（R9.2）

`HardConstraintEntry` 自任务 5.1 起已删除 `model_variant` 字段（`config/schema.py`），
`judge()` 的实现因此没有任何字段可读——判定所使用的模型变体由该 run 所在场景行
声明的 `model_variant` 与该 `observable` 的可得性共同决定，这一决定发生在
调用 `judge()` 之前（由 `sim`/`controller` 层决定采集哪个变体的观测量），不是
`judge()` 自身的职责。

## `Violation.unit` 的取数来源：硬编码表，而非扩宽 `judge()` 签名

`design.md` §6.5.3 给出的 `judge()` 签名是
`judge(metrics, scenario, constraints_cfg, *, candidate_id, run_id)`——不含
`metrics_cfg` 参数。但 `Violation.unit` 需要一个单位字符串，而 `HardConstraintEntry`
（`config/schema.py` 任务 5.1/5.8）按 R9.1 的「恰四字段」约束没有自己的 `unit`
字段，唯一的结构化单位来源是 `metrics.yaml`（经 `constraint_observables.obs.*.unit`
或 `metrics.<metric_id>.unit`）。

两个候选方案：(a) 给 `judge()` 增加 `metrics_cfg: MetricsConfig` 形参以读取真实
单位；(b) 在本模块内以四个固定字符串常量镜像这些单位。选择 (b)，理由：

- 这四个约束名到物理单位的对应关系是**封闭且固定**的（`vout_min`/`vout_max` → V、
  `peak_current_max` → A、`phase_margin_min` → deg），不随任务或候选变化，不是
  需要动态解析的配置值。
- `design.md` 给出的签名是显式契约，为了一个不会变化的映射去扩宽它，属于本可
  避免的接口改动。
- `config/schema.py` 顶部「与 `unit_mismatch` 的边界划分」一节已经指出：任务
  12.6（`controller/preflight.py`）的 `unit_mismatch` 断言会在配置加载期强制
  `metrics.yaml` 与这四个约束语义对应的单位逐字符一致，一旦不一致就直接拒绝
  启动、不进入寻优循环。这意味着此处硬编码的四个单位**不可能**在运行期悄悄
  与 `metrics.yaml` 漂移——preflight 已经把这条路堵死，硬编码在这个前提下是
  安全的，且不比读取配置更脆弱。

若这四个约束名与物理单位的对应关系将来变化（例如新增第五条硬约束），需要同时
修改本文件与 `config/schema.py`；本模块不会静默地对未登记的约束名给出错误单位
——`judge()` 只遍历 `HardConstraints` 已声明的四个具名字段，不存在第五个未登记
名称流入 `_HARD_CONSTRAINT_UNITS` 查找的路径。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from poweragent.config.schema import ConstraintsConfig, HardConstraintEntry
from poweragent.store.repo import ConstraintResult, MetricResult, ScenarioSpec, Violation

__all__ = ["judge"]

# 硬约束名 → 物理单位。镜像（而非读取）metrics.yaml 中对应观测量/指标的单位，
# 见本文件顶部「`Violation.unit` 的取数来源」一节的完整论证。
_HARD_CONSTRAINT_UNITS: Mapping[str, str] = {
    "vout_min": "V",
    "vout_max": "V",
    "peak_current_max": "A",
    "phase_margin_min": "deg",
}

# HardConstraints 的四个具名字段，遍历顺序固定（与 violations 列表的输出顺序
# 一致，便于测试断言与报告展示）。
_HARD_CONSTRAINT_NAMES: tuple[str, ...] = (
    "vout_min",
    "vout_max",
    "peak_current_max",
    "phase_margin_min",
)


def judge(
    metrics: Sequence[MetricResult],
    scenario: ScenarioSpec,
    constraints_cfg: ConstraintsConfig,
    *,
    candidate_id: str,
    run_id: str,
) -> ConstraintResult:
    """按 `hard_constraint.observable` 取值、按 `sense` 比较、按
    `applies_to_tier` 门控（`design.md` §6.5.3）。

    `lower` ⟹ 值 >= `value` 判满足；`upper` ⟹ 值 <= `value` 判满足；取等判满足
    （R9.10）。不读 `model_variant`（该字段已删除，见本文件顶部说明，R9.2）。
    只读 `MetricResult`，不重新计算物理量（R9.3）。支撑指标 `valid=False`
    或找不到对应 `metric_id` ⟹ `feasible=False` 且 `violations` 含对应条目，
    `actual=None`，不以 0、边界值或替代数值填充（R9.4）。
    """
    metrics_by_id: dict[str, MetricResult] = {m.metric_id: m for m in metrics}

    violations: list[Violation] = []

    hard_constraints = constraints_cfg.hard_constraints
    for name in _HARD_CONSTRAINT_NAMES:
        entry: HardConstraintEntry = getattr(hard_constraints, name)

        # 门控：只判定该 run 场景的 tier 落在该约束的 applies_to_tier 内的条目；
        # 其余条目不参与判定、不写 violations、不影响 feasible（R9.2）。
        if scenario.tier not in entry.applies_to_tier:
            continue

        metric = metrics_by_id.get(entry.observable)

        # 找不到支撑指标，或该指标 valid=False（或 value 意外为 None）：
        # 记一条 violation，actual=None，不填充任何替代数值（R9.4）。
        if metric is None or not metric.valid or metric.value is None:
            violations.append(
                Violation(
                    constraint=name,
                    limit=entry.value,
                    actual=None,
                    unit=_HARD_CONSTRAINT_UNITS[name],
                )
            )
            continue

        value = metric.value
        if entry.sense == "lower":
            satisfied = value >= entry.value
        else:
            satisfied = value <= entry.value

        if not satisfied:
            violations.append(
                Violation(
                    constraint=name,
                    limit=entry.value,
                    actual=value,
                    unit=_HARD_CONSTRAINT_UNITS[name],
                )
            )

    return ConstraintResult(
        candidate_id=candidate_id,
        scenario_id=scenario.scenario_id,
        run_id=run_id,
        feasible=not violations,
        violations=violations,
    )
