"""上下文构建：波形压缩、prompt 可复现性、以及采样点不泄露。

两条被守护的硬规则：

1. **波形采样点不得进入 prompt。** 单次仿真 5001 点 × 三个信号，装不进上下文也没有
   信息价值。压缩成六个特征由确定性代码完成，不经过模型。
2. **相同状态产出逐字节相同的 prompt 与 context_hash。** 这是"一次 Agent 行为可复现"
   的前提，也是缓存判等的基础。

第二条最容易在不知不觉中破掉：dict 遍历顺序、浮点默认格式化、集合的迭代顺序，任何
一处泄漏出非确定性，都会让同一状态产出不同文本，而这不会报错，只会让复现性静默失效。
"""

from __future__ import annotations

import numpy as np
import pytest

from poweragent.agent.prompt import (
    build_prompt,
    compress_waveform,
    count_oscillations,
    state_as_context,
)
from poweragent.agent.state import (
    ConstraintSpec,
    DomainSpec,
    FailedRegion,
    SearchState,
    TestedPoint,
    WaveformFeatures,
)
from poweragent.controller.stop import BestRecord
from poweragent.store.repo import MetricResult

pytestmark = pytest.mark.agent

TARGET_V = 0.8


def _metric(metric_id: str, value: float | None, *, valid: bool = True) -> MetricResult:
    return MetricResult(
        run_id="run_x",
        metric_id=metric_id,
        value=value,
        valid=valid,
        invalid_reason=None if valid else "extraction_failed",
    )


def _domain() -> dict[str, DomainSpec]:
    return {
        "rcomp": DomainSpec(
            unit="ohm", low=1e3, high=1e5, scale="log", ticks=("1000", "12328", "100000")
        ),
        "ccomp": DomainSpec(
            unit="F", low=1e-10, high=1e-8, scale="log",
            ticks=("1e-10", "2.2e-09", "1e-08"),
        ),
    }


def _constraints() -> dict[str, ConstraintSpec]:
    return {
        "vout_min": ConstraintSpec(value=0.75, sense="lower", unit="V"),
        "peak_current_max": ConstraintSpec(value=55.0, sense="upper", unit="A"),
        "phase_margin_min": ConstraintSpec(value=45.0, sense="lower", unit="deg"),
    }


def _state(**overrides) -> SearchState:
    base = {
        "legal_domain": _domain(),
        "hard_constraints": _constraints(),
        "current_best": BestRecord(candidate_id="cand_a", value=47.0),
        "tested_candidates": (
            TestedPoint(
                candidate_id="cand_a",
                parameters_si={"rcomp": 12000.0, "ccomp": 2.2e-9},
                feasible=True,
                objective_value=47.0,
                features=WaveformFeatures(
                    overshoot_v=0.0,
                    undershoot_v=0.0416,
                    settling_time_us=47.0,
                    ripple_v=0.0012,
                    oscillation_count=1,
                    diverged=False,
                ),
            ),
            TestedPoint(
                candidate_id="cand_b",
                parameters_si={"rcomp": 1000.0, "ccomp": 2.2e-9},
                feasible=False,
                objective_value=None,
                features=None,
                violations=("phase_margin_min",),
            ),
        ),
        "failed_regions": (
            FailedRegion(
                bounds={"rcomp": (1000.0, 3511.0)},
                failure_type="candidate_rejected",
                sample_count=3,
                run_ids=("run_1", "run_2", "run_3"),
            ),
        ),
        "remaining_budget": 194,
    }
    base.update(overrides)
    return SearchState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 振荡次数
# --------------------------------------------------------------------------


def test_monotonic_recovery_counts_no_oscillation() -> None:
    """单调恢复的波形穿越次数为 0。

    从下冲一路回到目标值、不过冲，这是过阻尼响应，没有振荡。
    """
    t = np.linspace(0.0, 200e-6, 2001)
    vout = TARGET_V - 0.04 * np.exp(-t / 20e-6)

    assert count_oscillations(t, vout, target_v=TARGET_V, step_trigger_s=0.0) == 0


def test_ringing_waveform_counts_each_crossing() -> None:
    """欠阻尼振铃的每次穿越都被计入。

    构造一个衰减正弦：它在死区之外来回穿越目标值若干次，次数应大于 1。这是区分
    "响应快"与"响应欠阻尼"的关键——两者的下冲和恢复时间可能相近，穿越次数相差很多。
    """
    t = np.linspace(0.0, 200e-6, 4001)
    vout = TARGET_V - 0.04 * np.exp(-t / 40e-6) * np.cos(2 * np.pi * 40e3 * t)

    assert count_oscillations(t, vout, target_v=TARGET_V, step_trigger_s=0.0) >= 3


def test_switching_ripple_does_not_inflate_the_count() -> None:
    """稳态开关纹波不产生虚假的振荡计数。

    纹波幅值约 2 mV 峰峰，落在死区内。没有死区的话计数会变成"采样点数除以二"，
    这个特征就完全失去意义。
    """
    t = np.linspace(0.0, 200e-6, 4001)
    vout = TARGET_V + 0.001 * np.sin(2 * np.pi * 2e6 * t)

    assert count_oscillations(t, vout, target_v=TARGET_V, step_trigger_s=0.0) == 0


def test_only_samples_after_the_step_are_counted() -> None:
    """阶跃触发前的采样点不计入。

    阶跃前处于稳态，任何穿越都是噪声；把它算进来会让"振荡了几次"这个特征依赖
    阶跃前那段的长度。
    """
    t = np.linspace(0.0, 200e-6, 2001)
    vout = np.where(t < 100e-6, TARGET_V + 0.02 * np.sign(np.sin(2 * np.pi * 50e3 * t)), TARGET_V)

    assert count_oscillations(t, vout, target_v=TARGET_V, step_trigger_s=100e-6) == 0


# --------------------------------------------------------------------------
# 波形压缩
# --------------------------------------------------------------------------


def test_features_are_read_from_metrics_not_recomputed() -> None:
    """前五项特征取自已算好的指标，不重算。

    指标计算是 `eval` 的职责。这里重算一遍就出现了两个可能不一致的实现，而不一致
    时哪个是对的无从判断。
    """
    metrics = [
        _metric("overshoot", 0.001),
        _metric("undershoot", 0.0416),
        _metric("settling_time", 47.0),
        _metric("output_ripple", 0.0012),
    ]

    features = compress_waveform(metrics)

    assert features.overshoot_v == 0.001
    assert features.undershoot_v == 0.0416
    assert features.settling_time_us == 47.0
    assert features.ripple_v == 0.0012


def test_invalid_metrics_become_none_not_zero() -> None:
    """无效指标如实留空，不用 0 或边界值填充。

    把"没测出来"伪装成"测出来是 0"会让模型据此推理，而它无从知道那是缺失值。
    一个下冲为 0 的候选看起来是完美设计。
    """
    metrics = [
        _metric("undershoot", None, valid=False),
        _metric("settling_time", 47.0),
    ]

    features = compress_waveform(metrics)

    assert features.undershoot_v is None
    assert features.settling_time_us == 47.0


def test_oscillation_count_is_none_without_waveform_arrays() -> None:
    """拿不到波形数组时振荡次数留空，而不是伪造一个值。

    调用方只有指标、没有波形时仍能构造特征——但不该因此得到一个编造的振荡次数。
    """
    features = compress_waveform([_metric("undershoot", 0.04)])

    assert features.oscillation_count is None
    assert features.undershoot_v == 0.04


def test_oscillation_count_is_computed_when_waveform_is_given() -> None:
    t = np.linspace(0.0, 200e-6, 2001)
    vout = TARGET_V - 0.04 * np.exp(-t / 20e-6)

    features = compress_waveform(
        [_metric("undershoot", 0.04)],
        time_s=t,
        vout_v=vout,
        target_v=TARGET_V,
        step_trigger_s=0.0,
    )

    assert features.oscillation_count == 0


# --------------------------------------------------------------------------
# prompt 可复现性
# --------------------------------------------------------------------------


def test_same_state_yields_byte_identical_prompt_and_hash() -> None:
    """相同状态两次构建产出逐字节相同的文本与哈希。

    这是"一次 Agent 行为可复现"的前提。破掉它的途径很隐蔽：dict 遍历顺序、浮点
    默认格式化、集合迭代顺序——任何一处泄漏非确定性都不会报错，只会让复现性静默失效。
    """
    first_text, first_hash = build_prompt(_state())
    second_text, second_hash = build_prompt(_state())

    assert first_text == second_text
    assert first_hash == second_hash


def test_context_hash_changes_when_the_state_changes() -> None:
    """状态变化必须改变 `context_hash`。

    否则不同状态会被判为同一次上下文，缓存会返回错误的结果。
    """
    _, base_hash = build_prompt(_state())
    _, budget_hash = build_prompt(_state(remaining_budget=100))
    _, best_hash = build_prompt(
        _state(current_best=BestRecord(candidate_id="cand_a", value=26.0))
    )

    assert base_hash != budget_hash
    assert base_hash != best_hash


def test_context_hash_ignores_internal_identifiers() -> None:
    """`context_hash` 不受候选 id 与 run id 影响。

    它们是内部标识，对模型判断毫无用处。若计入哈希，同一组物理状态换一次运行就得到
    不同哈希，缓存与复现性判等都会失效。
    """
    state_a = _state()
    tested = state_a.tested_candidates
    renamed = (
        TestedPoint(
            candidate_id="totally_different_id",
            parameters_si=tested[0].parameters_si,
            feasible=tested[0].feasible,
            objective_value=tested[0].objective_value,
            features=tested[0].features,
            violations=tested[0].violations,
        ),
        tested[1],
    )
    regions = state_a.failed_regions
    reregioned = (
        FailedRegion(
            bounds=regions[0].bounds,
            failure_type=regions[0].failure_type,
            sample_count=regions[0].sample_count,
            run_ids=("completely", "different", "runs"),
        ),
    )

    _, hash_a = build_prompt(state_a)
    _, hash_b = build_prompt(
        _state(tested_candidates=renamed, failed_regions=reregioned)
    )

    assert hash_a == hash_b


def test_hash_is_taken_from_normalised_state_not_from_the_text() -> None:
    """哈希取自规范化状态，因此与文本排版无关。

    若取自文本，任何纯排版调整都会改变哈希，同一状态在改过文案之后就不再被认为是
    同一次上下文——而缓存与复现性判等依赖的正是那个等价关系。这里通过"状态相同时
    上下文字典也相同"来间接确认哈希的输入是状态而非文本。
    """
    context_a = state_as_context(_state())
    context_b = state_as_context(_state())

    assert context_a == context_b


# --------------------------------------------------------------------------
# 采样点不泄露
# --------------------------------------------------------------------------


def test_prompt_contains_no_waveform_samples() -> None:
    """prompt 里不出现任何采样点数组。

    检查方式是长度与内容双管：五千点的波形无论怎么格式化都会让文本膨胀到数万字符，
    因此对 prompt 长度设一个上界；同时确认压缩后的特征值确实出现了（说明信息是
    以特征形式传递的，而不是被整体丢弃）。
    """
    text, _ = build_prompt(_state())

    assert len(text) < 4000, f"prompt 长度 {len(text)} 异常，可能夹带了采样点"
    # 六维特征应当以人可读的形式出现。
    assert "下冲" in text
    assert "恢复" in text
    assert "穿越" in text
    # numpy 数组的字符串形式特征
    assert "array(" not in text
    assert "..." not in text, "省略号通常来自 numpy 的截断打印"


def test_prompt_states_the_permission_boundary() -> None:
    """系统提示必须写明提案器没有判分权与停止决定权。

    这不只是文案：让模型以为自己能决定可行性或停止，会让它在输出里夹带越权断言，
    而那些断言可能被人误读成系统的结论。
    """
    from poweragent.agent.prompt import SYSTEM_PROMPT

    assert "不" in SYSTEM_PROMPT
    assert "确定性校验器" in SYSTEM_PROMPT
    assert "由确定性代码完成" in SYSTEM_PROMPT


def test_prompt_conveys_the_physics_structure() -> None:
    """系统提示要说明两个变量各自约束哪个方向。

    可行域被夹在中间是这个问题的结构。不说明它，模型只能均匀撒点，LLM 相对随机
    搜索的优势也就无从体现。
    """
    from poweragent.agent.prompt import SYSTEM_PROMPT

    assert "穿越频率" in SYSTEM_PROMPT
    assert "增益裕量" in SYSTEM_PROMPT
    assert "相位裕量" in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# 上下文内容完整性
# --------------------------------------------------------------------------


def test_prompt_lists_ticks_and_constraint_directions() -> None:
    """档位与约束方向都要出现在 prompt 里。

    档位是展开后的最终取值：让模型自己按 count 和 spacing 推算，等于把一个确定性
    计算交给它猜。约束方向同理——只给数值 `0.75`，模型无法判断那是下限还是上限。
    """
    text, _ = build_prompt(_state())

    assert "12328" in text, "应列出展开后的档位"
    assert "≥ 0.75 V" in text, "下限约束应标明方向"
    assert "≤ 55.0 A" in text, "上限约束应标明方向"


def test_prompt_reports_violated_constraints_not_just_infeasibility() -> None:
    """不可行的已测点要说明违反了哪条约束。

    只给可行/不可行，等于让模型在二值反馈上做梯度估计；知道是"相位裕量不够"还是
    "下冲超限"，它才能判断该往哪个方向调整。
    """
    text, _ = build_prompt(_state())

    assert "phase_margin_min" in text
    assert "不可行" in text


def test_empty_state_renders_without_placeholders_that_look_like_data() -> None:
    """首轮（无已测点、无失败区、无最佳）也要渲染得清楚。

    这一轮最容易出问题：若渲染成空白或 `[]`，模型会以为是数据缺失而不是"还没开始"。
    """
    text, _ = build_prompt(
        _state(
            current_best=None,
            tested_candidates=(),
            failed_regions=(),
        )
    )

    assert "还没有可行候选" in text
    assert "还没有评估过任何候选" in text
    assert "暂无已知失败区域" in text
