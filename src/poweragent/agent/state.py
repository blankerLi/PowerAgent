"""搜索状态：喂给 LLM 的只读输入。

这些类型定义"提案器能看到什么"，因此它们的形状就是 LLM 域的权限边界的具体化。
全部 frozen——`propose()` 拿到的是快照，不能改动任何事实状态。

为什么这里 import controller
----------------------------
`BestRecord` 定义在 `controller/stop.py`。本模块导入它，看似是 LLM 域依赖确定性域，
但信任域约束的是**权限**（谁能判分、谁能写状态、谁能决定校验严格度），不是 import
方向。导入一个 frozen dataclass 不会让 LLM 域获得任何写入能力。

真正要避免的是反向依赖：`controller` 可以 import `agent`（主循环调用提案器是设计的
一部分），但 `agent` 不得 import `controller` 里任何**有副作用**的东西。当前依赖链是
`controller.run_task → agent.propose → agent.state → controller.stop`，而
`controller.stop` 不 import `agent`，因此没有循环。

与 `controller/stop.py` 里同名类的关系
--------------------------------------
`controller/stop.py` 有一个也叫 `SearchState` 的最小化占位，只含 `current_best`——
`should_stop()` 实际只读这一个字段。本模块的完整版有 `current_best` 同名字段，
因此可以直接喂给 `should_stop()`，不需要转换。两者并存是有意的：让停止判定只依赖它
真正需要的那一个字段，比让它接收一个七字段的大对象更能说明它读了什么。

检索证据字段已删除
------------------
`design.md` §6.6 的 `SearchState` 有第七个字段 `evidence: EvidenceTriple`。工程知识
检索模块在范围裁剪中被移除，该字段因此恒为空。按项目自己的裁剪原则（能删就删，
保留一个永不被填充的字段就是死代码），这里不保留它。未来若加回检索，加字段比
维护一个恒空字段更清晰。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from poweragent.controller.stop import BestRecord

__all__ = [
    "DomainSpec",
    "ConstraintSpec",
    "WaveformFeatures",
    "TestedPoint",
    "FailedRegion",
    "SearchState",
]


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """一个设计变量的合法域。

    `ticks` 是**已展开并规范化**的档位字面量，不是配置里的 `{count, spacing}` 简写。
    展开发生在确定性域（`config.ticks.expand_all_ticks`），提案器看到的是最终取值列表——
    让模型自己按 count 和 spacing 推算档位，等于把一个确定性计算交给它去猜。
    """

    unit: str
    low: float
    high: float
    scale: Literal["log", "linear"]
    ticks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConstraintSpec:
    """一条硬约束的阈值与方向。

    `design.md` §6.6 把 `hard_constraints` 写成 `Mapping[str, float]`，这里带上
    `sense` 与 `unit`。只给数值不够：模型无法从 `0.75` 判断这是下限还是上限，而
    "输出电压不低于 0.75 V"与"不高于 0.75 V"是完全相反的要求。少这一项信息，
    提案质量会系统性变差，而原因很难从输出上看出来。
    """

    value: float
    sense: Literal["lower", "upper"]
    unit: str

    def describe(self) -> str:
        relation = "≥" if self.sense == "lower" else "≤"
        return f"{relation} {self.value} {self.unit}"


@dataclass(frozen=True, slots=True)
class WaveformFeatures:
    """一次仿真波形压缩后的六维特征（`design.md` §6.6）。

    这是**唯一**允许进入 prompt 的波形信息形态。原始采样点（单次仿真 5001 点 × 三个
    信号，其中相电流还是四列）不得进入上下文：既装不进去，也没有信息价值——模型要
    判断的是"这个设计点表现如何、下一步往哪走"，而那由这六个数决定。

    全部字段可为 `None`：对应指标无效时如实留空，不用 0 或边界值填充。把"没测出来"
    伪装成"测出来是 0"会让模型据此推理，而它无从知道那是缺失值。
    """

    overshoot_v: float | None
    undershoot_v: float | None
    settling_time_us: float | None
    ripple_v: float | None
    oscillation_count: int | None
    diverged: bool


@dataclass(frozen=True, slots=True)
class TestedPoint:
    """一个已评估过的候选，及其结果摘要。

    带上 `violations`（违反了哪几条约束）而不只是 `feasible` 布尔值：知道"因为相位
    裕量不够而被拒"与"因为下冲超限而被拒"，模型才能判断该往哪个方向调整。只给
    可行/不可行，等于让它在二值反馈上做梯度估计。
    """

    candidate_id: str
    parameters_si: Mapping[str, float]
    feasible: bool
    objective_value: float | None
    features: WaveformFeatures | None
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FailedRegion:
    """一片已知会失败的参数区域（`design.md` §6.6）。

    `failure_type` 直接复用 `runs.cause` 的取值域，不新增枚举——失败原因在事实源里
    已有一套登记，再造一套只会带来两者何时不同步的问题。
    """

    bounds: Mapping[str, tuple[float, float]]
    failure_type: str
    sample_count: int
    run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchState:
    """提案器的完整只读输入。

    由 `controller` 在每轮开始时从 SQLite 重建，因此提案器看到的永远是事实源的当前
    快照，而不是某个进程内累积的状态。这也是"状态完全从数据库重建"这条性质在 LLM
    域的体现：中断后续跑，提案器看到的输入与未中断时一致。
    """

    legal_domain: Mapping[str, DomainSpec]
    hard_constraints: Mapping[str, ConstraintSpec]
    current_best: BestRecord | None
    tested_candidates: Sequence[TestedPoint]
    failed_regions: Sequence[FailedRegion]
    remaining_budget: int
