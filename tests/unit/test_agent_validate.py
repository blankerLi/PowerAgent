"""确定性候选校验器。

这是 LLM 域与仿真域之间唯一的闸门，没有旁路。它算错的后果分两种，都不容易发现：
放过一个不该放的候选，会让一次仿真预算花在无意义的点上；错拒一个该放的候选，会让搜索
永远看不到那片区域，而且不报错。

三条被反复守护的性质：

- **不静默吸附。** 不命中档位就拒绝，不改成最近档位。吸附会让模型学不会精确输出，
  而且被吸附的点与模型意图的点是两个不同设计，落库却记成后者，搜索轨迹就此失真。
- **不静默裁剪。** 越界就拒绝，不夹到边界。同上。
- **拒绝原因是具名的、格式稳定的。** 它们要落 `rejections` 表并被聚合统计，
  距离数值的格式化规则若不稳定，同一原因会出现多种写法。
"""

from __future__ import annotations

import math

import pytest

from poweragent.agent.state import DomainSpec, TestedPoint
from poweragent.agent.validate import (
    decide_mode,
    snap_to_tick,
    tick_distance,
    validate,
)

pytestmark = pytest.mark.unit

TICK_TOL = 1e-6
NOVELTY_MIN = 1.0


def _log_ticks(low: float, high: float, count: int = 12) -> tuple[str, ...]:
    """等比档位，与 `config.ticks.expand_ticks` 同一算法。"""
    ratio = high / low
    return tuple(
        format(low * (ratio ** (i / (count - 1))), ".12g") for i in range(count)
    )


RCOMP_TICKS = _log_ticks(1e3, 1e5)
CCOMP_TICKS = _log_ticks(1e-10, 1e-8)


def _domain() -> dict[str, DomainSpec]:
    return {
        "rcomp": DomainSpec(unit="ohm", low=1e3, high=1e5, scale="log", ticks=RCOMP_TICKS),
        "ccomp": DomainSpec(unit="F", low=1e-10, high=1e-8, scale="log", ticks=CCOMP_TICKS),
    }


def _on_tick(index_r: int, index_c: int) -> dict[str, float]:
    return {"rcomp": float(RCOMP_TICKS[index_r]), "ccomp": float(CCOMP_TICKS[index_c])}


def _tested(*points: dict[str, float]) -> tuple[TestedPoint, ...]:
    return tuple(
        TestedPoint(
            candidate_id=f"cand_{i}",
            parameters_si=params,
            feasible=True,
            objective_value=47.0,
            features=None,
        )
        for i, params in enumerate(points)
    )


def _validate(raw, *, mode="explore", tested=()):
    return validate(
        raw,
        domain=_domain(),
        tick_match_rel_tol=TICK_TOL,
        novelty_min_ticks=NOVELTY_MIN,
        tested=tested,
        mode=mode,
    )


# --------------------------------------------------------------------------
# 档位命中
# --------------------------------------------------------------------------


def test_exact_tick_is_accepted_and_normalised_to_the_tick_value() -> None:
    """命中档位时返回档位值本身，消掉输入的浮点表示误差。

    模型输出 18738.0 与展开出的 18738.000000000004 在容差内相等，落库时必须统一——
    否则同一个设计点会生成两个不同的 `candidate_id`，缓存判等与重复检测都失效。
    """
    tick = float(RCOMP_TICKS[7])
    spec = _domain()["rcomp"]

    assert snap_to_tick(tick, spec, rel_tol=TICK_TOL) == tick
    # 相对容差内的微小偏差同样命中，且返回的是档位值而非输入值。
    perturbed = tick * (1.0 + TICK_TOL / 2.0)
    assert snap_to_tick(perturbed, spec, rel_tol=TICK_TOL) == tick


def test_off_tick_value_returns_none_rather_than_the_nearest_tick() -> None:
    """不命中档位返回 `None`，不返回最近档位。

    这是"不静默吸附"在函数层面的体现。返回最近档位会让调用方无从区分"命中了"与
    "被我们改过了"。
    """
    spec = _domain()["rcomp"]

    assert snap_to_tick(18000.0, spec, rel_tol=TICK_TOL) is None
    assert snap_to_tick(20000.0, spec, rel_tol=TICK_TOL) is None


def test_off_tick_candidate_is_rejected_with_the_variable_name() -> None:
    """偏离档位的候选被拒，原因指明是哪个变量。

    只报 `off_tick` 不指明变量，后续分析无法判断模型在哪一维上不够精确。
    """
    outcome = _validate([{"rcomp": 18000.0, "ccomp": float(CCOMP_TICKS[5])}])

    assert outcome.accepted == ()
    assert len(outcome.rejected) == 1
    raw, reason = outcome.rejected[0]
    assert reason == "off_tick:rcomp"
    # 拒绝记录保留原始取值，供落 `rejections` 表。
    assert raw["rcomp"] == 18000.0


def test_accepted_candidate_carries_a_deterministic_id() -> None:
    """接受的候选带一个由参数决定的 id，两次校验得到同一个。"""
    first = _validate([_on_tick(7, 5)])
    second = _validate([_on_tick(7, 5)])

    assert len(first.accepted) == 1
    assert first.accepted[0].candidate_id == second.accepted[0].candidate_id
    assert first.accepted[0].parameters_si == _on_tick(7, 5)


# --------------------------------------------------------------------------
# 形状与合法域
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_reason"),
    [
        ({"rcomp": 1.0e4}, "key_mismatch"),
        ({"rcomp": 1.0e4, "ccomp": 1e-9, "extra": 1.0}, "key_mismatch"),
        ({"rcomp": float("nan"), "ccomp": 1e-9}, "non_finite_value"),
        ({"rcomp": float("inf"), "ccomp": 1e-9}, "non_finite_value"),
        ({"rcomp": -1.0e4, "ccomp": 1e-9}, "non_positive_value"),
        ({"rcomp": 0.0, "ccomp": 1e-9}, "non_positive_value"),
    ],
)
def test_shape_problems_are_rejected_with_named_reasons(raw, expected_reason) -> None:
    """键集合与数值合法性问题各有具名原因。

    这些本该被 `agent/schema.py` 拦在更早，但校验器不能假设上游一定正确——它是唯一
    的闸门，直接构造调用（例如参考网格扫描）同样要过这里。
    """
    outcome = _validate([raw])

    assert outcome.accepted == ()
    assert outcome.rejected[0][1] == expected_reason


def test_out_of_domain_is_rejected_not_clamped() -> None:
    """越界候选被拒，不夹到边界。

    夹到边界属于静默修正语义：落库会记成模型提了边界点，而它其实提了一个域外点。
    """
    outcome = _validate([{"rcomp": 1.0e6, "ccomp": float(CCOMP_TICKS[5])}])

    assert outcome.accepted == ()
    assert outcome.rejected[0][1] == "out_of_domain:rcomp"
    # 原始取值保持原样，未被改写为边界值。
    assert outcome.rejected[0][0]["rcomp"] == 1.0e6


# --------------------------------------------------------------------------
# 档位距离
# --------------------------------------------------------------------------


def test_adjacent_ticks_are_exactly_one_apart() -> None:
    """相邻档位的距离恰为 1.0。

    这是"档位距离"这个单位的定义，也是 `grid` 模式必须存在的理由：参考网格的相邻点
    距离正是 1.0，若 `novelty_min_ticks` 冻结为大于 1.0，144 个点会被整批拒绝。
    """
    domain = _domain()
    a = _on_tick(5, 5)
    b = _on_tick(6, 5)

    assert tick_distance(a, b, domain) == pytest.approx(1.0, abs=1e-9)


def test_identical_points_have_zero_distance() -> None:
    domain = _domain()

    assert tick_distance(_on_tick(3, 4), _on_tick(3, 4), domain) == pytest.approx(0.0)


def test_distance_takes_the_maximum_across_dimensions_not_the_norm() -> None:
    """各维取最大值而非欧氏范数。

    要回答的问题是"这两个点是否足够不同"：只要有一维差了一整档就算不同。欧氏距离会
    把两维各差半档判成 0.71 而误当作近邻。
    """
    domain = _domain()
    two_dims = tick_distance(_on_tick(5, 5), _on_tick(6, 6), domain)

    assert two_dims == pytest.approx(1.0, abs=1e-9), "两维各差一档，最大值仍是 1.0"


def test_distance_is_measured_on_a_log_scale() -> None:
    """距离在对数尺度上度量。

    两个变量都是 log 刻度，线性尺度下 1k→2k 与 99k→100k 的绝对差相同但搜索意义相差
    极大。这里用"跨越同样档位数的两对点距离相同"来确认。
    """
    domain = _domain()
    low_pair = tick_distance(_on_tick(0, 0), _on_tick(2, 0), domain)
    high_pair = tick_distance(_on_tick(9, 0), _on_tick(11, 0), domain)

    assert low_pair == pytest.approx(high_pair, abs=1e-9)
    assert low_pair == pytest.approx(2.0, abs=1e-9)


# --------------------------------------------------------------------------
# 三种 mode 的差异
# --------------------------------------------------------------------------


def test_explore_rejects_points_too_close_to_tested_ones() -> None:
    """explore 模式拒绝档位距离小于阈值的点，原因带距离数值。

    距离要写进原因：只报 `low_novelty` 无法判断模型是提了个几乎重合的点还是差了
    0.9 档。格式固定为四位小数，使同一原因在聚合统计时不会分裂成多种写法。
    """
    tested = _tested(_on_tick(5, 5))
    # 与已测点在 ccomp 上差一档、rcomp 相同 ⟹ 距离恰为 1.0，不小于阈值，应通过。
    ok = _validate([_on_tick(5, 6)], tested=tested, mode="explore")
    assert len(ok.accepted) == 1

    # 构造一个距离在 0 与 1 之间的点：直接给一个不在档位上的值不行（会先被 off_tick
    # 拒掉），因此改用一个更大的新颖度阈值来触发。
    outcome = validate(
        [_on_tick(5, 6)],
        domain=_domain(),
        tick_match_rel_tol=TICK_TOL,
        novelty_min_ticks=2.0,
        tested=tested,
        mode="explore",
    )
    assert outcome.accepted == ()
    reason = outcome.rejected[0][1]
    assert reason.startswith("low_novelty:")
    assert reason == "low_novelty:1.0000", f"距离格式应为四位小数，实际 {reason}"


def test_local_refine_allows_near_neighbours_and_records_the_distance() -> None:
    """local_refine 允许近邻，并把距离写入 `notes`。

    `notes` 存在的理由很具体：近邻标注需要一个输出去处，而 `validate()` 按契约不得写
    数据库。
    """
    tested = _tested(_on_tick(5, 5))

    outcome = validate(
        [_on_tick(5, 6)],
        domain=_domain(),
        tick_match_rel_tol=TICK_TOL,
        novelty_min_ticks=2.0,  # 在 explore 下会被拒
        tested=tested,
        mode="local_refine",
    )

    assert len(outcome.accepted) == 1
    candidate_id = outcome.accepted[0].candidate_id
    assert outcome.notes[candidate_id] == "near_tested:1.0000"


def test_explore_leaves_notes_empty() -> None:
    """只有 local_refine 会写 `notes`。"""
    outcome = _validate([_on_tick(5, 6)], tested=_tested(_on_tick(5, 5)))

    assert len(outcome.accepted) == 1
    assert outcome.notes == {}


def test_grid_mode_skips_duplicate_and_novelty_checks_but_keeps_the_rest() -> None:
    """grid 模式跳过重复与新颖度，但仍查键集合、合法域与档位。

    参考网格的 144 点必须全部经过校验（不存在绕过校验的仿真路径），同时不能受
    `novelty_min_ticks` 取值影响——相邻网格点距离恰为 1.0。
    """
    tested = _tested(_on_tick(5, 5))

    # 与已测点完全重合，grid 下仍接受。
    duplicate = validate(
        [_on_tick(5, 5)],
        domain=_domain(),
        tick_match_rel_tol=TICK_TOL,
        novelty_min_ticks=5.0,
        tested=tested,
        mode="grid",
    )
    assert len(duplicate.accepted) == 1
    assert duplicate.notes == {}

    # 但档位与合法域检查依然生效。
    still_checked = validate(
        [{"rcomp": 18000.0, "ccomp": float(CCOMP_TICKS[5])}, {"rcomp": 1e6, "ccomp": 1e-9}],
        domain=_domain(),
        tick_match_rel_tol=TICK_TOL,
        novelty_min_ticks=5.0,
        tested=tested,
        mode="grid",
    )
    assert still_checked.accepted == ()
    assert {reason for _, reason in still_checked.rejected} == {
        "off_tick:rcomp",
        "out_of_domain:rcomp",
    }


# --------------------------------------------------------------------------
# 重复检测
# --------------------------------------------------------------------------


def test_duplicate_of_a_tested_point_is_rejected() -> None:
    outcome = _validate([_on_tick(5, 5)], tested=_tested(_on_tick(5, 5)))

    assert outcome.accepted == ()
    assert outcome.rejected[0][1] == "duplicate_of_tested"


def test_duplicate_within_the_same_batch_is_rejected() -> None:
    """同一批内的重复也要拒绝。

    模型偶尔在一次输出里给出两个相同候选。若只与历史比对，这两个会双双通过并各消耗
    一次仿真预算——而它们是同一个设计点。
    """
    outcome = _validate([_on_tick(5, 5), _on_tick(5, 5)])

    assert len(outcome.accepted) == 1
    assert len(outcome.rejected) == 1
    assert outcome.rejected[0][1] == "duplicate_in_batch"


def test_mixed_batch_partitions_into_accepted_and_rejected() -> None:
    """一批里合格与不合格的分别归入两个序列，互不影响。

    一个坏候选不该让整批作废——那会让模型的一次小失误浪费整轮预算。
    """
    outcome = _validate(
        [
            _on_tick(7, 3),  # 合格
            {"rcomp": 18000.0, "ccomp": 1e-9},  # 偏离档位
            _on_tick(9, 4),  # 合格
            {"rcomp": 1e6, "ccomp": 1e-9},  # 越界
        ]
    )

    assert len(outcome.accepted) == 2
    assert len(outcome.rejected) == 2
    assert {reason for _, reason in outcome.rejected} == {
        "off_tick:rcomp",
        "out_of_domain:rcomp",
    }


# --------------------------------------------------------------------------
# mode 的决定权
# --------------------------------------------------------------------------


def test_mode_is_decided_by_deterministic_rules() -> None:
    """校验模式由无改善轮数与是否已有最佳候选决定，规则确定。

    规则本身可以公开，但调用它的是 `controller`——提案器不得决定自己被校验的严格度，
    否则被校验方就在挑选校验标准。
    """
    assert decide_mode(no_improvement_rounds=0, has_current_best=False) == "explore"
    assert decide_mode(no_improvement_rounds=0, has_current_best=True) == "explore"
    assert decide_mode(no_improvement_rounds=1, has_current_best=True) == "local_refine"
    assert decide_mode(no_improvement_rounds=3, has_current_best=True) == "local_refine"
    # 还没有最佳候选时不进精化模式：没有可精化的对象。
    assert decide_mode(no_improvement_rounds=2, has_current_best=False) == "explore"


def test_first_round_with_no_history_accepts_on_tick_candidates() -> None:
    """首轮没有已测点时，新颖度检查无对象，合格候选应全部通过。

    否则首轮会因为"与空集比对"而误拒所有候选，搜索根本无法启动。
    """
    outcome = _validate([_on_tick(3, 3), _on_tick(7, 7), _on_tick(11, 0)])

    assert len(outcome.accepted) == 3
    assert outcome.rejected == ()


def test_distance_helper_handles_degenerate_domains() -> None:
    """档位不足两个时该维度不参与距离计算，不抛异常。

    这类退化输入不该由调用方去防——校验器是唯一闸门，直接构造的调用同样会到这里。
    """
    degenerate = {
        "rcomp": DomainSpec(unit="ohm", low=1e3, high=1e3, scale="log", ticks=("1000",)),
    }

    assert tick_distance({"rcomp": 1e3}, {"rcomp": 1e3}, degenerate) == 0.0
    assert math.isfinite(tick_distance({"rcomp": 1e3}, {"rcomp": 2e3}, degenerate))


def test_exactly_one_tick_apart_passes_the_default_novelty_threshold() -> None:
    """恰好相差一整档的候选在阈值为 1.0 时必须通过。

    这条守护一个浮点边界。档位字面量按 `.12g` 格式化后再转回浮点，因此"相差一整档"
    算出来常是 0.999999999999 而非精确的 1.0。若判据写成朴素的 `距离 < 阈值`，
    阈值取 1.0 时相邻档位的候选会被整批误拒——而这类错拒不报错，只是让搜索永远看不到
    那片区域，极难发现。

    这里同时验证两个方向：相邻档位（距离 1.0）通过，重合点（距离 0）仍被拒。
    """
    tested = _tested(_on_tick(5, 5))
    domain = _domain()

    measured = tick_distance(_on_tick(5, 6), _on_tick(5, 5), domain)
    assert measured < 1.0, (
        f"前提：格式化误差应使实测距离略小于 1.0，实际 {measured!r}；"
        "若它精确等于 1.0，本测试就失去了守护对象"
    )

    adjacent = _validate([_on_tick(5, 6)], tested=tested, mode="explore")
    assert len(adjacent.accepted) == 1, "相邻档位不应被新颖度检查拒绝"

    identical = _validate([_on_tick(5, 5)], tested=tested, mode="explore")
    assert identical.accepted == ()
    assert identical.rejected[0][1] == "duplicate_of_tested"
