"""开关模型的物理性质，以及与平均模型的一致性。

开关模型是两个保真度里更高的那一个，因此它承担两重角色：既要自己物理正确，
又要作为校准平均模型的参照。下面的测试分成三组：

1. **开关特有的现象**：输出纹波、相电流三角波、相位交错。这些在平均模型里按定义
   不存在，是"开关模型确实在建模开关动作"的证据。
2. **与平均模型的一致性**：两者必须描述同一个电路。这组测试是低保真模型的校准
   基准——平均模型的采样延迟参数就是靠它定出来的。
3. **共有的物理事实**：稳态无静差、发散判据等，与平均模型同口径。

关于成本比：开关模型比平均模型慢一到两个数量级，这是分层执行的价值来源，但
"慢多少倍"依赖机器，不适合写成断言。这里只断言两者的输出采样点数一致（便于逐点
比较），耗时数据留给基准脚本。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from poweragent.sim.backends.averaged import simulate_averaged
from poweragent.sim.backends.buck_model import OperatingCondition, load_buck_spec
from poweragent.sim.backends.switching import simulate_switching

pytestmark = pytest.mark.sim

VOUT_TARGET = 0.8
GUARD_VOUT = 1.7
GUARD_IPHASE = 110.0

BASELINE = (12000.0, 2.2e-9)
GRID_BEST = (18738.0, 8.111e-10)

SCREENING = OperatingCondition(
    vin_v=12.0, temp_c=25.0, load_start_a=100.0, load_end_a=150.0, slew_a_per_us=100.0
)
EVALUATION = OperatingCondition(
    vin_v=10.8, temp_c=85.0, load_start_a=100.0, load_end_a=150.0, slew_a_per_us=200.0
)

TEST_STOP_TIME_S = 400e-6
# 固定积分步长，与 configs/model.yaml 的 solver.max_step 一致（每开关周期约 100 步）。
TIME_STEP_S = 2e-8


@pytest.fixture(scope="module")
def sw_spec(repo_root: Path):
    return load_buck_spec(repo_root / "models" / "buck4ph_switching.yaml")


@pytest.fixture(scope="module")
def avg_spec(repo_root: Path):
    return load_buck_spec(repo_root / "models" / "buck4ph_averaged.yaml")


def _run_switching(spec, condition, rcomp, ccomp, *, guard_iphase=GUARD_IPHASE):
    return simulate_switching(
        spec,
        condition,
        rcomp_ohm=rcomp,
        ccomp_f=ccomp,
        stop_time_s=TEST_STOP_TIME_S,
        time_step_s=TIME_STEP_S,
        guard_vout_abs_max=GUARD_VOUT,
        guard_iphase_abs_max=guard_iphase,
    )


def _run_averaged(spec, condition, rcomp, ccomp):
    return simulate_averaged(
        spec,
        condition,
        rcomp_ohm=rcomp,
        ccomp_f=ccomp,
        stop_time_s=TEST_STOP_TIME_S,
        rel_tol=1e-4,
        guard_vout_abs_max=GUARD_VOUT,
        guard_iphase_abs_max=GUARD_IPHASE,
    )


def _tail_mask(run, fraction: float = 0.10) -> np.ndarray:
    """稳态窗口：仿真末段。与 `eval/metrics.py` 的稳态窗口惯例同口径。"""
    span = run.time_s[-1] - run.time_s[0]
    return run.time_s >= run.time_s[-1] - fraction * span


# --------------------------------------------------------------------------
# 开关特有的现象
# --------------------------------------------------------------------------


def test_output_ripple_exists_and_averaged_model_has_none(sw_spec, avg_spec) -> None:
    """开关模型产生可测的输出纹波，平均模型按定义没有。

    这是两个保真度的分野。若开关模型的纹波也接近零，说明 PWM 根本没起作用；
    若平均模型出现明显纹波，说明它错误地引入了开关频率成分。
    """
    sw = _run_switching(sw_spec, SCREENING, *BASELINE)
    avg = _run_averaged(avg_spec, SCREENING, *BASELINE)
    assert sw.status == "ok" and avg.status == "ok"

    sw_ripple = np.ptp(sw.vout_v[_tail_mask(sw)])
    avg_ripple = np.ptp(avg.vout_v[_tail_mask(avg)])

    assert sw_ripple > 0.5e-3, f"开关模型稳态纹波过小: {sw_ripple * 1e3:.3f} mV"
    assert avg_ripple < 10e-6, f"平均模型不应有纹波: {avg_ripple * 1e6:.1f} µV"


def test_phase_current_ripple_matches_the_analytic_estimate(sw_spec) -> None:
    """相电流纹波峰峰值接近解析值 (Vin−Vout)·D·Tsw/L。

    容差取 40%：解析式假设固定占空比，而峰值电流模式的关断时刻由电流达到指令
    决定，两者本就有系统性差异。这条测试要抓的是量级错误（例如把 L 或 Tsw 用错、
    忘记除以相数），不是精确吻合。
    """
    sw = _run_switching(sw_spec, SCREENING, *BASELINE)
    assert sw.status == "ok"

    tail = _tail_mask(sw)
    measured = float(np.ptp(sw.iphase_a[tail][:, 0]))
    analytic = sw_spec.phase_current_ripple_a(SCREENING.vin_v)

    assert measured == pytest.approx(analytic, rel=0.4), (
        f"实测相电流纹波 {measured:.2f} A vs 解析 {analytic:.2f} A"
    )


def test_phases_are_interleaved_not_identical(sw_spec) -> None:
    """各相电流互不相同：载波错开 360/N 度。

    平均模型下各相恒等；开关模型下若各相也逐点相同，说明相位交错没有生效，
    输出纹波的抵消效应与纹波频率都会错。
    """
    sw = _run_switching(sw_spec, SCREENING, *BASELINE)
    tail = _tail_mask(sw)
    phases = sw.iphase_a[tail]

    assert phases.shape[1] == sw_spec.n_phase
    for k in range(1, sw_spec.n_phase):
        assert not np.allclose(phases[:, 0], phases[:, k]), f"相 0 与相 {k} 逐点相同"

    # 各相直流分量应基本一致（均流），差异远小于纹波幅值。
    means = phases.mean(axis=0)
    assert np.ptp(means) < 0.05 * means.mean(), f"各相直流分量失衡: {means}"


def test_interleaving_cancels_part_of_the_total_ripple(sw_spec) -> None:
    """总电流纹波小于各相纹波之和：交错产生部分抵消。

    本工况占空比仅约 7%，各相导通区间几乎不重叠，抵消有限但必须存在。断言取
    "总纹波小于单相纹波乘以相数"这个宽松形式，因为精确的抵消系数依赖占空比。
    """
    sw = _run_switching(sw_spec, SCREENING, *BASELINE)
    tail = _tail_mask(sw)
    phases = sw.iphase_a[tail]

    single = float(np.ptp(phases[:, 0]))
    total = float(np.ptp(phases.sum(axis=1)))

    assert total < single * sw_spec.n_phase, (
        f"总纹波 {total:.2f} A 未小于单相 {single:.2f} A × {sw_spec.n_phase}"
    )


def test_switching_peak_current_exceeds_averaged(sw_spec, avg_spec) -> None:
    """开关模型的相电流峰值高于平均模型：纹波叠加在均流之上。

    `peak_current_max` 这条硬约束因此只有在开关模型上才有实际约束力——筛选层用
    平均模型会系统性低估峰值。这是分层执行必须让评价层用开关模型的原因之一。
    """
    sw = _run_switching(sw_spec, EVALUATION, *BASELINE)
    avg = _run_averaged(avg_spec, EVALUATION, *BASELINE)

    sw_peak = float(np.abs(sw.iphase_a).max())
    avg_peak = float(np.abs(avg.iphase_a).max())

    assert sw_peak > avg_peak
    # 差值应与相电流纹波的半幅同量级。
    assert sw_peak - avg_peak < sw_spec.phase_current_ripple_a(EVALUATION.vin_v)


# --------------------------------------------------------------------------
# 与平均模型的一致性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition_name", ["screening", "evaluation"])
@pytest.mark.parametrize("params", [BASELINE, GRID_BEST], ids=["baseline", "grid-best"])
def test_two_models_agree_on_steady_state(
    sw_spec, avg_spec, condition_name: str, params
) -> None:
    """两个模型的稳态输出电压一致到 0.5% 以内。

    容差取自 `configs/model.yaml` 的 `dual_model_consistency.steady_tol`。稳态是
    最容易一致的部分（不含动态），若这里就对不上，说明两者的直流工作点或器件
    参数不同——也就是说它们不是同一个电路。
    """
    condition = {"screening": SCREENING, "evaluation": EVALUATION}[condition_name]
    sw = _run_switching(sw_spec, condition, *params)
    avg = _run_averaged(avg_spec, condition, *params)
    assert sw.status == "ok" and avg.status == "ok"

    # 开关模型含纹波，取窗口均值与平均模型比较。
    sw_mean = float(sw.vout_v[_tail_mask(sw)].mean())
    avg_mean = float(avg.vout_v[_tail_mask(avg)].mean())

    relative = abs(sw_mean - avg_mean) / abs(avg_mean)
    assert relative < 0.005, f"稳态相对偏差 {relative * 100:.4f}% 超过 0.5%"


@pytest.mark.parametrize("condition_name", ["screening", "evaluation"])
@pytest.mark.parametrize("params", [BASELINE, GRID_BEST], ids=["baseline", "grid-best"])
def test_two_models_agree_on_undershoot_depth(
    sw_spec, avg_spec, condition_name: str, params
) -> None:
    """两个模型的负载阶跃下冲深度一致到 10 mV 以内。

    用**绝对**容差而非相对容差：下冲本身只有几十毫伏，当某组参数把下冲压到十几
    毫伏时，几毫伏的建模差异就是 20% 的相对偏差，相对容差在这里会失去意义。
    10 mV 相对于 50 mV 的电压容限是有意义的一致性水平。

    这组测试是平均模型采样延迟参数的校准基准：早先用 `0.5/fsw`（漏掉相数）时
    这里的偏差达 8 mV 且方向系统性偏悲观，改为 `0.5/(N·fsw)` 后收敛。
    """
    condition = {"screening": SCREENING, "evaluation": EVALUATION}[condition_name]
    sw = _run_switching(sw_spec, condition, *params)
    avg = _run_averaged(avg_spec, condition, *params)

    sw_undershoot = VOUT_TARGET - float(sw.vout_v.min())
    avg_undershoot = VOUT_TARGET - float(avg.vout_v.min())

    assert abs(sw_undershoot - avg_undershoot) < 10e-3, (
        f"下冲 平均={avg_undershoot * 1e3:.1f} mV 开关={sw_undershoot * 1e3:.1f} mV"
    )


def test_two_models_share_the_step_trigger_time(sw_spec, avg_spec) -> None:
    """两个模型的阶跃触发时刻相同，波形在时间轴上可逐点比较。

    共用 `STEP_TRIGGER_FRACTION` 保证这一点。若不同，一致性核对与前后对比图都会
    错位，而这种错位很容易被误读成建模差异。
    """
    sw = _run_switching(sw_spec, EVALUATION, *BASELINE)
    avg = _run_averaged(avg_spec, EVALUATION, *BASELINE)

    assert sw.step_trigger_s == pytest.approx(avg.step_trigger_s)


# --------------------------------------------------------------------------
# 共有的物理事实
# --------------------------------------------------------------------------


def test_steady_state_mean_is_at_target(sw_spec) -> None:
    """含纹波的稳态输出，其均值仍落在标称值上：积分项消除静差。"""
    sw = _run_switching(sw_spec, EVALUATION, *BASELINE)
    mean = float(sw.vout_v[_tail_mask(sw)].mean())

    assert mean == pytest.approx(VOUT_TARGET, abs=1e-3)


def test_output_shape_contract(sw_spec) -> None:
    """输出形状与平均模型一致，`eval/metrics.py` 才能用同一条路径消费。"""
    sw = _run_switching(sw_spec, EVALUATION, *BASELINE)

    assert sw.iphase_a.shape == (sw.n_samples, sw_spec.n_phase)
    assert sw.vout_v.shape == sw.time_s.shape
    assert sw.iout_a.shape == sw.time_s.shape
    assert np.all(np.isfinite(sw.vout_v))
    assert float(sw.iout_a[0]) == pytest.approx(EVALUATION.load_start_a)
    assert float(sw.iout_a[-1]) == pytest.approx(EVALUATION.load_end_a)


def test_divergence_guard_mechanism_triggers_and_reports(sw_spec) -> None:
    """发散判据能触发、提前退出并报告原因。

    与平均模型同理：用低于稳态均流的安全界触发，测的是判据机制而非某组参数会失稳
    （电流限幅使全域有界，见平均模型对应测试）。
    """
    sw = _run_switching(sw_spec, EVALUATION, *BASELINE, guard_iphase=30.0)

    assert sw.status == "diverged"
    assert sw.solver_message
    assert sw.n_samples < int(TEST_STOP_TIME_S / 0.2e-6)


def test_repeated_runs_are_bit_identical(sw_spec) -> None:
    """固定步长积分本就应逐位可复现，这里确认没有引入顺序相关的状态。"""
    first = _run_switching(sw_spec, EVALUATION, *BASELINE)
    second = _run_switching(sw_spec, EVALUATION, *BASELINE)

    assert np.array_equal(first.time_s, second.time_s)
    assert np.array_equal(first.vout_v, second.vout_v)
    assert np.array_equal(first.iphase_a, second.iphase_a)


# --------------------------------------------------------------------------
# 双模型一致性记录（scripts/dual_model_consistency.py 的产物）
# --------------------------------------------------------------------------


def test_consistency_record_exists_and_reports_agreement(repo_root: Path) -> None:
    """一致性记录存在且结论为一致。

    `preflight` 的 `check_dual_model_consistency()` 要求 `ok` 为真才放行。分层执行
    的全部价值建立在"筛选层筛掉的候选在评价层也确实不好"这个假设上，这份记录就是
    给那个假设立的证据。
    """
    import json

    record_path = repo_root / "artifacts" / "dual_model_consistency.json"
    assert record_path.is_file(), (
        "缺少一致性记录；请运行 python scripts/dual_model_consistency.py"
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["ok"] is True
    assert record["checkpoints"], "记录应至少含一个核对点"

    for point in record["checkpoints"]:
        assert point["status"] == "compared"
        assert point["steady"]["within_tolerance"]
        assert point["transient"]["within_tolerance"]


def test_consistency_record_is_bound_to_the_current_model_package(
    repo_root: Path, config_dir: Path
) -> None:
    """记录内嵌的 `model_package_hash` 与当前依赖闭包的重算值一致。

    这是 preflight 的第三条判据。它的意思是：两个保真度是否一致的结论**只对当时
    那份模型成立**，改动任何模型文件都会使记录失效，必须重跑核对。
    """
    import json

    from poweragent.config.loader import load_all
    from poweragent.sim.hashing import model_package_hash, resolve_dependency_closure

    bundle = load_all(config_dir)
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    current_hash = model_package_hash(closure, model_dump)

    record = json.loads(
        (repo_root / "artifacts" / "dual_model_consistency.json").read_text("utf-8")
    )
    assert record["model_package_hash"] == current_hash, (
        "记录已与当前模型包脱钩；请重跑 python scripts/dual_model_consistency.py"
    )


def test_averaged_undershoot_is_independent_of_simulation_length(
    avg_spec, sw_spec
) -> None:
    """平均模型的下冲与仿真总时长无关。

    这条守护的是一个曾经被违反的正确性要求。`solve_ivp` 的初始步长按积分区间长度
    启发式选取，而 `t_eval` 只控制输出采样、不约束内部步长；下冲峰值出现在阶跃后
    几微秒内，区间越长、步长越大，峰值就越容易被稠密插值抹平。实测同一候选同一
    工况下，下冲随 `stop_time` 从 34.6 mV 漂到 30.96 mV。

    缓存命中判等、复现性回归门禁、双模型一致性核对全都建立在"同一候选同一场景结果
    唯一"之上，因此这不是精度问题而是正确性问题。修法是把步长上限锁到输出网格间距。
    """
    undershoots = []
    for stop_time_s in (200e-6, 400e-6, 800e-6):
        run = simulate_averaged(
            avg_spec,
            SCREENING,
            rcomp_ohm=BASELINE[0],
            ccomp_f=BASELINE[1],
            stop_time_s=stop_time_s,
            rel_tol=1e-4,
            guard_vout_abs_max=GUARD_VOUT,
            guard_iphase_abs_max=GUARD_IPHASE,
        )
        undershoots.append(VOUT_TARGET - float(run.vout_v.min()))

    spread = max(undershoots) - min(undershoots)
    assert spread < 1e-5, (
        f"下冲随 stop_time 变化 {spread * 1e3:.3f} mV，应当无关: "
        f"{[f'{u * 1e3:.3f}mV' for u in undershoots]}"
    )
