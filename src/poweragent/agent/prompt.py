"""上下文构建：波形压缩与 prompt 生成。

两条硬规则
----------
**一、波形采样点不得进入上下文。** 单次仿真产出 5001 个时间点 × 三个信号（相电流
还是四列），一轮多个候选、多个场景。这些数据既装不进上下文窗口，也没有信息价值：
模型要判断的是"这个设计点表现如何、下一步往哪走"，而那由六个特征决定。压缩由
`compress_waveform()` 确定性完成，不经过模型。

**二、相同状态必须产出逐字节相同的 prompt。** 这是"一次 Agent 行为可复现"的前提。
做法是把状态经 `canonical_json`（排序键、固定数值格式）序列化后再拼文本，而不是
依赖 dict 的遍历顺序或 f-string 对浮点数的默认格式化。`context_hash` 取自同一份
规范化 JSON，因此它标识的是"模型看到的状态"，而不是"文本长什么样"——两者一一对应，
但前者才是有语义的那个。

为什么不把 prompt 做成模板文件
------------------------------
`prompts/` 目录曾预留给外置模板。这里选择留在代码里：模板的价值是让非开发者改文案，
而本项目的 prompt 与 `agent/schema.py` 的校验规则、`SearchState` 的字段是强耦合的——
改一处必须同时改另一处。外置成文件只是把这种耦合从"同一个文件里两段代码"变成
"代码与文件之间的隐式约定"，而后者没有任何检查会失败。
"""

from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

import numpy as np

from poweragent.agent.state import (
    ConstraintSpec,
    DomainSpec,
    FailedRegion,
    SearchState,
    TestedPoint,
    WaveformFeatures,
)
from poweragent.config.hashing import canonical_json
from poweragent.store.repo import MetricResult

__all__ = [
    "SYSTEM_PROMPT",
    "compress_waveform",
    "count_oscillations",
    "build_prompt",
    "state_as_context",
]

SYSTEM_PROMPT = (
    "你是多相 Buck 变换器电压环补偿网络整定的助手。\n"
    "\n"
    "你的职责只有一个：根据给定的搜索状态提出下一批候选参数，并说明理由。你**不**判定"
    "候选是否可行、**不**决定是否停止搜索（只能给建议）、**不**修改约束或预算——这些"
    "由确定性代码完成。你提出的候选会先经过一个确定性校验器（检查合法域、是否精确命中"
    "档位、与已测点的重复度与距离），不合规的会被拒绝并连同原因记录。校验器不会替你"
    "修正取值：偏离档位或越界的候选被直接丢弃，因此请精确照抄给定的档位数值。\n"
    "\n"
    "补偿网络是 Type-II 跨导型，两个设计变量的物理作用："
    "Rcomp 决定电压外环穿越频率（正比关系），增大它加快瞬态、减小负载阶跃下冲，"
    "但高频开环增益随之上升，过大会使增益裕量不足乃至失稳；"
    "Ccomp 决定补偿零点位置 fz = 1/(2π·Rcomp·Ccomp)，零点需落在穿越频率之下才能补足"
    "相位，Ccomp 过小会把零点推到穿越频率之上、导致相位裕量不足。\n"
    "\n"
    "因此两个变量各自约束一个方向，可行域被夹在中间。请利用这个结构做有方向的搜索，"
    "而不是均匀撒点。"
)

# 稳态窗口占仿真末段的比例，与 `eval/metrics.py` 的窗口惯例同口径。
_STEADY_TAIL_FRACTION = 0.10

# 判定一次振荡的电压偏移死区（V）。小于它的波动视为纹波或数值噪声，不计入振荡次数。
# 取值为误差带（±1% × 0.8 V = 8 mV）的四分之一：既能滤掉约 2 mV 的开关纹波，
# 又不会漏掉真正的环路振铃。
_OSCILLATION_DEADBAND_V = 2.0e-3


def count_oscillations(
    time_s: np.ndarray, vout_v: np.ndarray, *, target_v: float, step_trigger_s: float
) -> int:
    """数阶跃后输出电压穿越目标值的次数。

    这是六个特征里唯一无法从已有指标读出的一项——超调、下冲、恢复时间、纹波都已由
    `eval/metrics.py` 算过，但"振荡了几次"没有对应指标，而它恰好是区分"响应快"与
    "响应欠阻尼"的关键：两者的下冲和恢复时间可能相近，穿越次数却相差很多。

    带死区：只有偏离目标超过 `_OSCILLATION_DEADBAND_V` 之后再回穿，才算一次。没有
    死区的话开关纹波会让计数变成"采样点数除以二"，完全失去意义。
    """
    if time_s.size == 0 or vout_v.size != time_s.size:
        return 0

    after_step = time_s >= step_trigger_s
    signal = vout_v[after_step] - target_v
    if signal.size == 0:
        return 0

    crossings = 0
    # 从"当前处于哪一侧"出发，只在离开死区后才更新侧别，避免在死区内反复翻转。
    side = 0
    for value in signal:
        if value > _OSCILLATION_DEADBAND_V:
            if side == -1:
                crossings += 1
            side = 1
        elif value < -_OSCILLATION_DEADBAND_V:
            if side == 1:
                crossings += 1
            side = -1

    return crossings


def compress_waveform(
    metrics: Sequence[MetricResult],
    *,
    time_s: np.ndarray | None = None,
    vout_v: np.ndarray | None = None,
    target_v: float | None = None,
    step_trigger_s: float | None = None,
    diverged: bool = False,
) -> WaveformFeatures:
    """把一次仿真结果压缩为六维特征。

    前五项从已算好的 `MetricResult` 读取，不重算——指标计算是 `eval` 的职责，这里
    重算一遍就出现了两个可能不一致的实现。无效指标（`valid=False`）如实留 `None`，
    不用 0 填充：把"没测出来"伪装成"测出来是 0"会让模型据此推理。

    振荡次数需要波形本身，因此 `time_s` / `vout_v` / `target_v` / `step_trigger_s`
    四个参数成组可选：给全了就算，缺任一即留 `None`。这样调用方在只有指标、拿不到
    波形数组时仍能构造特征，而不必伪造一个振荡次数。
    """
    by_id = {m.metric_id: m for m in metrics}

    def value_of(metric_id: str) -> float | None:
        result = by_id.get(metric_id)
        if result is None or not result.valid or result.value is None:
            return None
        return float(result.value)

    have_waveform = (
        time_s is not None
        and vout_v is not None
        and target_v is not None
        and step_trigger_s is not None
    )
    oscillations = (
        count_oscillations(
            time_s, vout_v, target_v=target_v, step_trigger_s=step_trigger_s
        )
        if have_waveform
        else None
    )

    return WaveformFeatures(
        overshoot_v=value_of("overshoot"),
        undershoot_v=value_of("undershoot"),
        settling_time_us=value_of("settling_time"),
        ripple_v=value_of("output_ripple"),
        oscillation_count=oscillations,
        diverged=diverged,
    )


def _domain_as_context(domain: Mapping[str, DomainSpec]) -> dict[str, object]:
    return {
        name: {
            "unit": spec.unit,
            "range": [spec.low, spec.high],
            "scale": spec.scale,
            "ticks": list(spec.ticks),
        }
        for name, spec in domain.items()
    }


def _constraints_as_context(
    constraints: Mapping[str, ConstraintSpec],
) -> dict[str, object]:
    return {
        name: {"limit": spec.value, "sense": spec.sense, "unit": spec.unit}
        for name, spec in constraints.items()
    }


def _features_as_context(features: WaveformFeatures | None) -> dict[str, object] | None:
    if features is None:
        return None
    return {
        "overshoot_v": features.overshoot_v,
        "undershoot_v": features.undershoot_v,
        "settling_time_us": features.settling_time_us,
        "ripple_v": features.ripple_v,
        "oscillation_count": features.oscillation_count,
        "diverged": features.diverged,
    }


def _tested_as_context(points: Sequence[TestedPoint]) -> list[dict[str, object]]:
    return [
        {
            "parameters": dict(point.parameters_si),
            "feasible": point.feasible,
            "objective_value": point.objective_value,
            "violations": list(point.violations),
            "features": _features_as_context(point.features),
        }
        for point in points
    ]


def _failed_as_context(regions: Sequence[FailedRegion]) -> list[dict[str, object]]:
    return [
        {
            "bounds": {k: list(v) for k, v in region.bounds.items()},
            "failure_type": region.failure_type,
            "sample_count": region.sample_count,
        }
        for region in regions
    ]


def state_as_context(state: SearchState) -> dict[str, object]:
    """把搜索状态转成待序列化的纯数据结构。

    刻意不包含 `run_ids` 与 `candidate_id`：它们是内部标识，对模型的判断毫无用处，
    却会占用上下文并让 `context_hash` 对无关变化敏感（同一组物理状态换一次运行就
    得到不同的哈希，缓存与复现性判等都会失效）。
    """
    best = state.current_best
    return {
        "legal_domain": _domain_as_context(state.legal_domain),
        "hard_constraints": _constraints_as_context(state.hard_constraints),
        # 只放目标值，不放 candidate_id：后者是内部标识，对判断无用，却会让
        # context_hash 对"换一次运行"这种无关变化敏感。
        "current_best": None if best is None else {"objective_value": best.value},
        "tested_candidates": _tested_as_context(state.tested_candidates),
        "failed_regions": _failed_as_context(state.failed_regions),
        "remaining_budget": state.remaining_budget,
    }


def _render_domain(domain: Mapping[str, DomainSpec]) -> str:
    lines = []
    for name, spec in sorted(domain.items()):
        ticks = ", ".join(spec.ticks)
        lines.append(
            f"- {name}（{spec.unit}，{spec.scale} 刻度）："
            f"合法区间 [{spec.low}, {spec.high}]\n"
            f"  可用档位：{ticks}"
        )
    return "\n".join(lines)


def _render_constraints(constraints: Mapping[str, ConstraintSpec]) -> str:
    return "\n".join(
        f"- {name}：{spec.describe()}" for name, spec in sorted(constraints.items())
    )


def _render_features(features: WaveformFeatures | None) -> str:
    if features is None:
        return "无波形特征"
    parts = []
    if features.undershoot_v is not None:
        parts.append(f"下冲 {features.undershoot_v * 1e3:.1f} mV")
    if features.overshoot_v is not None:
        parts.append(f"超调 {features.overshoot_v * 1e3:.1f} mV")
    if features.settling_time_us is not None:
        parts.append(f"恢复 {features.settling_time_us:.1f} µs")
    if features.ripple_v is not None:
        parts.append(f"纹波 {features.ripple_v * 1e3:.2f} mV")
    if features.oscillation_count is not None:
        parts.append(f"穿越 {features.oscillation_count} 次")
    if features.diverged:
        parts.append("已发散")
    return "，".join(parts) if parts else "指标均无效"


def _render_tested(points: Sequence[TestedPoint]) -> str:
    if not points:
        return "（还没有评估过任何候选）"

    lines = []
    for point in points:
        params = ", ".join(
            f"{key}={value:.6g}" for key, value in sorted(point.parameters_si.items())
        )
        verdict = "可行" if point.feasible else "不可行"
        if point.violations:
            verdict += f"（违反：{', '.join(point.violations)}）"
        objective = (
            f"目标值 {point.objective_value:.2f}"
            if point.objective_value is not None
            else "目标值不可用"
        )
        lines.append(f"- {params} → {verdict}，{objective}；{_render_features(point.features)}")
    return "\n".join(lines)


def _render_failed(regions: Sequence[FailedRegion]) -> str:
    if not regions:
        return "（暂无已知失败区域）"

    lines = []
    for region in regions:
        bounds = ", ".join(
            f"{key} ∈ [{low:.6g}, {high:.6g}]"
            for key, (low, high) in sorted(region.bounds.items())
        )
        lines.append(f"- {bounds}：{region.failure_type}（{region.sample_count} 个样本）")
    return "\n".join(lines)


def build_prompt(state: SearchState) -> tuple[str, str]:
    """构建用户消息，返回 `(prompt_text, context_hash)`。

    两次以相同 `state` 调用产出逐字节相同的文本与哈希。保证来自两处：文本里的数值
    一律用显式格式串（`:.6g` / `:.1f`）而非默认 `str()`；哈希取自
    `canonical_json(state_as_context(state))` 而不是文本本身。

    哈希取自规范化状态而非文本，是因为它要回答的问题是"模型看到的是哪个状态"。若
    取自文本，任何纯排版调整（多一个空行、换一句提示语）都会改变哈希，于是同一状态
    在改过文案之后就不再被认为是同一次上下文——而缓存与复现性判等依赖的正是那个
    等价关系。
    """
    context = state_as_context(state)
    context_hash = hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()

    best = state.current_best
    best_line = (
        "（还没有可行候选）"
        if best is None
        else f"目标值 {best.value:.2f}（越小越好）"
    )

    text = (
        "## 设计变量与合法域\n"
        f"{_render_domain(state.legal_domain)}\n"
        "\n"
        "候选取值必须**精确取自**上列档位，照抄档位数值即可。校验器不会把偏离档位的"
        "取值吸附到最近档位——偏离即拒绝，该候选连同拒绝原因被记录，并且不消耗仿真预算。"
        "落在合法区间之外的候选同样被直接拒绝，不会被裁剪到边界。\n"
        "\n"
        "## 硬约束（全部必须满足）\n"
        f"{_render_constraints(state.hard_constraints)}\n"
        "\n"
        "## 当前最佳可行候选\n"
        f"{best_line}\n"
        "\n"
        "## 已评估的候选\n"
        f"{_render_tested(state.tested_candidates)}\n"
        "\n"
        "## 已知失败区域\n"
        f"{_render_failed(state.failed_regions)}\n"
        "\n"
        "## 剩余预算\n"
        f"还可发起 {state.remaining_budget} 次仿真评估。\n"
        "\n"
        "## 任务\n"
        "提出下一批候选。若已评估的点显示继续搜索不会带来改善，"
        "可把 stop_recommendation 置为 true —— 但最终是否停止由控制器决定。"
    )

    return text, context_hash
