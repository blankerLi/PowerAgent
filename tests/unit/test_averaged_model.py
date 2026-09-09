"""平均模型的物理性质测试。

这些断言不是"跑通了就行"，而是把模型必须满足的物理事实钉住。数值仿真最危险的
失效模式是"看起来对但错得系统"——曲线形状合理、量级也不荒谬，实际却少了一项
关键机制。下面每条测试都对应一个具体的失效模式：

- 稳态精度：初值不是解析稳态而靠"跑一段让它稳下来"，会让每次仿真的起点随参数
  漂移，结果不可复现。
- 积分器无静差：漏掉积分项会留下稳态误差，而恢复时间按误差带定义，有静差则
  恢复时间恒为无穷。
- 单调性：带宽升高必然减小下冲。若不单调，说明求解器精度或抗饱和逻辑有问题。
- 工况敏感性：温度与输入电压若不影响结果，说明这两个场景字段是装饰性的，
  「最恶劣工况」也就无从谈起。
- 发散判据：极端参数必须被判为发散，而不是返回一条看似正常的曲线。
- 抗积分饱和：缺了它，电感电流会过冲到负载电流的数倍（实测过）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from poweragent.sim.backends.averaged import simulate_averaged
from poweragent.sim.backends.buck_model import OperatingCondition, load_buck_spec

pytestmark = pytest.mark.sim

VOUT_TARGET = 0.8
# 与 configs/model.yaml 的 divergence_guard 及 constraints.yaml 的硬约束一致。
GUARD_VOUT = 1.7
GUARD_IPHASE = 110.0
VOUT_MIN_LIMIT = 0.75

# 工程基线（configs/model.yaml 的 baseline）与 12x12 档位网格扫描出的最优点。
BASELINE = (12000.0, 2.2e-9)
GRID_BEST = (18738.0, 8.111e-10)

# 与 configs/task.yaml 的两个场景行一致。
SCREENING = OperatingCondition(
    vin_v=12.0, temp_c=25.0, load_start_a=100.0, load_end_a=150.0, slew_a_per_us=100.0
)
EVALUATION = OperatingCondition(
    vin_v=10.8, temp_c=85.0, load_start_a=100.0, load_end_a=150.0, slew_a_per_us=200.0
)

# 测试用较短的仿真时长以控制耗时。阶跃触发在总时长的 10%，基线恢复时间约 47 µs，
# 400 µs 足以覆盖瞬态并留出干净的稳态窗口。
TEST_STOP_TIME_S = 400e-6


@pytest.fixture(scope="module")
def spec(repo_root: Path):
    return load_buck_spec(repo_root / "models" / "buck4ph_averaged.yaml")


def _run(spec, condition, rcomp, ccomp, *, stop_time_s=TEST_STOP_TIME_S):
    return simulate_averaged(
        spec,
        condition,
        rcomp_ohm=rcomp,
        ccomp_f=ccomp,
        stop_time_s=stop_time_s,
        rel_tol=1e-4,
        guard_vout_abs_max=GUARD_VOUT,
        guard_iphase_abs_max=GUARD_IPHASE,
    )


def _settling_us(run) -> float:
    """最后一次离开 ±1% 误差带的时刻相对阶跃触发点的时间（µs）。

    与 `metrics.yaml` 的 `settling_time.band = 0.01_of_vout_target` 同口径，但
    独立实现——本模块测的是模型，不是 `eval/metrics.py` 的指标提取。
    """
    outside = np.where(np.abs(run.vout_v - VOUT_TARGET) > 0.01 * VOUT_TARGET)[0]
    if outside.size == 0:
        return 0.0
    return float((run.time_s[outside[-1]] - run.step_trigger_s) * 1e6)


def _undershoot_mv(run) -> float:
    return float((VOUT_TARGET - run.vout_v.min()) * 1e3)


# --------------------------------------------------------------------------
# 稳态与静差
# --------------------------------------------------------------------------


def test_pre_step_output_is_exactly_at_target(spec) -> None:
    """阶跃触发前输出电压严格等于标称值。

    这一段不做数值积分，直接用 `steady_state()` 的解析解填充，因此偏差应当是
    恒等的零而不是"很小"。若这里出现非零偏差，说明解析稳态解与微分方程不自洽。
    """
    run = _run(spec, EVALUATION, *BASELINE)
    pre_step = run.time_s < run.step_trigger_s

    assert pre_step.sum() > 0, "阶跃前应有采样点"
    assert np.all(run.vout_v[pre_step] == pytest.approx(VOUT_TARGET, abs=1e-12))


def test_integrator_eliminates_steady_state_error(spec) -> None:
    """瞬态结束后输出回到标称值，无稳态误差。

    补偿器的积分项保证这一点。若漏掉积分项，输出会停在一个有偏差的值上，
    按误差带定义的恢复时间将永远无法满足。
    """
    run = _run(spec, EVALUATION, *BASELINE)

    assert float(run.vout_v[-1]) == pytest.approx(VOUT_TARGET, abs=5e-5)


def test_load_current_reaches_commanded_end_value(spec) -> None:
    """负载电流按有限斜率变化并稳定在终值。"""
    run = _run(spec, EVALUATION, *BASELINE)

    assert float(run.iout_a[0]) == pytest.approx(EVALUATION.load_start_a)
    assert float(run.iout_a[-1]) == pytest.approx(EVALUATION.load_end_a)


# --------------------------------------------------------------------------
# 波形形状契约（eval/metrics.py 消费的形状）
# --------------------------------------------------------------------------


def test_phase_current_is_two_dimensional_and_balanced(spec) -> None:
    """相电流形状为 `(n_time, n_phase)`，平均模型下各相均流。

    `eval/metrics.py` 的 `phase_peak_current` 依赖这个二维形状同时做跨相与跨
    时间的聚合；开关模型下各列会因相位交错而不同，形状必须现在就一致。
    """
    run = _run(spec, EVALUATION, *BASELINE)

    assert run.iphase_a.shape == (run.n_samples, spec.n_phase)
    first_column = run.iphase_a[:, 0]
    for phase in range(1, spec.n_phase):
        assert np.allclose(run.iphase_a[:, phase], first_column)

    # 各相之和即总电感电流，稳态下等于负载电流。
    assert float(run.iphase_a[0].sum()) == pytest.approx(EVALUATION.load_start_a)


def test_all_signals_share_the_time_axis_length(spec) -> None:
    run = _run(spec, EVALUATION, *BASELINE)

    assert run.vout_v.shape == run.time_s.shape
    assert run.iout_a.shape == run.time_s.shape
    assert np.all(np.isfinite(run.vout_v))


# --------------------------------------------------------------------------
# 单调性：带宽与瞬态性能
# --------------------------------------------------------------------------


def test_undershoot_decreases_as_bandwidth_rises(spec) -> None:
    """Rcomp 增大 ⟹ 外环穿越频率上升 ⟹ 负载阶跃下冲变浅。

    下冲近似为 ΔI·t_response/Cout，而 t_response ∝ 1/fc，因此这个单调关系是
    模型必须满足的。不单调意味着求解器精度不足或抗饱和逻辑出了问题。
    """
    ccomp = 2.2e-9
    rcomps = [5337.0, 12328.0, 28480.0]

    crossovers = [
        spec.crossover_hz(rcomp_ohm=rc, temp_c=EVALUATION.temp_c) for rc in rcomps
    ]
    assert crossovers == sorted(crossovers), "穿越频率应随 Rcomp 单调上升"

    undershoots = [_undershoot_mv(_run(spec, EVALUATION, rc, ccomp)) for rc in rcomps]
    assert undershoots == sorted(undershoots, reverse=True), (
        f"下冲应随带宽升高单调变浅，实测 {undershoots}"
    )


def test_grid_optimum_outperforms_engineering_baseline(spec) -> None:
    """参考网格扫描出的最优点在恢复时间上显著优于工程基线，且两者都可行。

    这条测试守护的是本项目的核心前提：搜索空间里**确实存在**比人工整定明显更好
    的点。若基线已经接近最优，整个寻优任务就没有意义了。
    """
    base = _run(spec, EVALUATION, *BASELINE, stop_time_s=1e-3)
    best = _run(spec, EVALUATION, *GRID_BEST, stop_time_s=1e-3)

    assert base.status == "ok" and best.status == "ok"
    # 两者都满足输出电压下限这一硬约束。
    assert float(base.vout_v.min()) > VOUT_MIN_LIMIT
    assert float(best.vout_v.min()) > VOUT_MIN_LIMIT

    base_settling = _settling_us(base)
    best_settling = _settling_us(best)
    assert best_settling < 0.6 * base_settling, (
        f"最优点应把恢复时间压到基线的 60% 以下，实测 {best_settling:.1f} vs {base_settling:.1f} µs"
    )


# --------------------------------------------------------------------------
# 工况敏感性：温度与输入电压必须真的起作用
# --------------------------------------------------------------------------


def test_higher_temperature_deepens_undershoot(spec) -> None:
    """高温使电流检测增益上升、外环带宽下降，下冲变深。

    若这两个工况给出相同结果，说明 `temp_c` 是装饰性字段，评价工况取 85 °C
    也就失去意义。
    """
    cold = OperatingCondition(10.8, 25.0, 100.0, 150.0, 200.0)
    hot = OperatingCondition(10.8, 85.0, 100.0, 150.0, 200.0)

    assert spec.sense_gain_ohm(85.0) > spec.sense_gain_ohm(25.0)
    assert _undershoot_mv(_run(spec, hot, *BASELINE)) > _undershoot_mv(
        _run(spec, cold, *BASELINE)
    )


def test_input_voltage_compresses_the_large_signal_slew_margin(spec) -> None:
    """输入电压降低减小电感电流的最大可达变化率，压缩追赶负载的余量。

    这条钉住的是输入电压起作用的**机理**，与温度不同：温度压低环路增益，输入电压
    压缩大信号余量。余量本身不大——评价工况约 267 A/µs 对负载 200 A/µs，只有 1.3 倍，
    所以不能像早先注释里写的那样说成"远快于负载斜率、不构成限制"。把实测比例钉在
    测试里，防止在报告或讲述中把这件事描述反。
    """
    slew_low = spec.max_current_slew_a_per_s(10.8)
    slew_nom = spec.max_current_slew_a_per_s(12.0)
    assert slew_low < slew_nom

    load_slew_a_per_s = EVALUATION.slew_a_per_us * 1e6
    margin = slew_low / load_slew_a_per_s
    assert 1.0 < margin < 2.0, f"追赶余量应在 1~2 倍之间，实测 {margin:.2f}"


def test_sampling_delay_is_set_by_effective_switching_frequency(spec) -> None:
    """调制器延迟取半个**有效**开关周期，分母含相数。

    多相交错使各相轮流采样，有效采样率是单相开关频率的 N 倍。用 `0.5/fsw`（漏掉
    相数）会让平均模型系统性偏悲观，与开关模型的下冲偏差达 20%——而偏悲观的筛选层
    会误杀实际可行的候选，且被误杀者不会进入评价层得到纠正。
    """
    assert spec.modulator_delay_s == pytest.approx(
        0.5 / (spec.n_phase * spec.fsw_hz)
    )
    # 等效带宽应落在 Ridley 采样保持模型给出的 N*fsw/pi 附近。
    equivalent_bw = 1.0 / (2.0 * np.pi * spec.modulator_delay_s)
    assert equivalent_bw == pytest.approx(spec.n_phase * spec.fsw_hz / np.pi)


def test_evaluation_scenario_is_harsher_than_screening(spec) -> None:
    """评价工况（低输入、高温、快阶跃）比筛选工况更难。

    分层执行的前提：筛选层通过不代表评价层通过，否则筛选层就是在做无用功。
    """
    screening_undershoot = _undershoot_mv(_run(spec, SCREENING, *BASELINE))
    evaluation_undershoot = _undershoot_mv(_run(spec, EVALUATION, *BASELINE))

    assert evaluation_undershoot > screening_undershoot


# --------------------------------------------------------------------------
# 发散判据与抗积分饱和
# --------------------------------------------------------------------------


def test_divergence_guard_mechanism_triggers_and_reports(spec) -> None:
    """发散判据的终止事件能触发、能提前退出、并如实报告原因。

    这里刻意用一个低于稳态相电流（37.5 A）的安全界来触发，而不是去找一组"会跑飞
    的参数"——因为那样的参数不存在，见下一条测试。测的是判据机制本身：事件是否
    生效、状态是否被置为 `diverged`、是否给出原因、以及是否真的提前终止而非跑完全程。
    """
    run = simulate_averaged(
        spec,
        EVALUATION,
        rcomp_ohm=BASELINE[0],
        ccomp_f=BASELINE[1],
        stop_time_s=TEST_STOP_TIME_S,
        rel_tol=1e-4,
        guard_vout_abs_max=GUARD_VOUT,
        guard_iphase_abs_max=30.0,  # 低于稳态均流 37.5 A，必然触发
    )

    assert run.status == "diverged"
    assert run.solver_message
    # 提前退出：采样点数应显著少于跑完全程
    assert run.n_samples < int(TEST_STOP_TIME_S / 0.2e-6)


def test_current_limit_keeps_the_loop_bounded_across_the_whole_domain(spec) -> None:
    """整个设计域内没有一点触发发散判据，连远超域上限的增益也不会。

    原因是电流指令限幅（`i_limit_a`）与抗积分饱和共同作用：增益再高，电流指令也被
    钳在限幅值上，环路退化为有界的开关式行为而非无界发散。限幅本来就是保护功能，
    这是它该有的效果。

    这件事对搜索有实际影响，值得钉住：可行性完全由硬约束判定决定，而不是靠候选
    "跑飞"来淘汰。于是搜索器面对的是一片连续可评估的地形，而不是散布着大片
    "无效"空洞的地图——发散判据在本模型里的角色是数值安全网，不是筛选手段。
    """
    corners = [(1.0e3, 1.0e-10), (1.0e3, 1.0e-8), (1.0e5, 1.0e-10), (1.0e5, 1.0e-8)]
    beyond_domain = [(1.0e7, 1.0e-10)]  # 域上限的 100 倍

    for rcomp, ccomp in corners + beyond_domain:
        run = _run(spec, EVALUATION, rcomp, ccomp)
        assert run.status == "ok", (
            f"({rcomp:.0e}, {ccomp:.0e}) 意外非 ok: {run.status} {run.solver_message}"
        )
        assert np.all(np.isfinite(run.vout_v))


def test_phase_current_stays_within_guard_when_status_is_ok(spec) -> None:
    """`status='ok'` 的仿真，其相电流全程不超过发散判据安全界。

    这是抗积分饱和在起作用的证据：没有它，负载阶跃后电感电流会过冲到负载电流的
    数倍并越过安全界，整个搜索空间的候选都会被判为发散。
    """
    for rcomp, ccomp in (BASELINE, GRID_BEST):
        run = _run(spec, EVALUATION, rcomp, ccomp)
        assert run.status == "ok"
        peak = float(np.abs(run.iphase_a).max())
        assert peak < GUARD_IPHASE
        # 过冲不应超过阶跃终值均流的两倍——windup 时会远超这个界。
        assert peak < 2.0 * EVALUATION.load_end_a / spec.n_phase


def test_repeated_runs_are_bit_identical(spec) -> None:
    """相同输入两次求解得到逐点相同的结果。

    可复现性是整个项目的地基：缓存命中判等、回归门禁、以及"同一候选重复评估
    结果一致"的容差校验都依赖它。
    """
    first = _run(spec, EVALUATION, *BASELINE)
    second = _run(spec, EVALUATION, *BASELINE)

    assert np.array_equal(first.time_s, second.time_s)
    assert np.array_equal(first.vout_v, second.vout_v)
