"""用解析已知的合成波形核对 `eval/metrics.py` 的指标实现。

为什么用合成波形而不是仿真输出
--------------------------------
指标层是整条判分链的地基：约束判定、worst-case 聚合、候选排序全部建立在它的
输出上。它算错了，后面每一步都错，而且错得很隐蔽——曲线看起来正常，数字量级也
不荒谬，只是系统性偏了。

仿真输出无法用来核对这件事：它的"正确指标值"本身就得靠某个指标实现算出来，
拿它验证指标实现是循环论证。所以这里构造一条分段线性的波形，它的五项指标都能
用初中几何手算出精确值，再拿实现去比对。

期望值的来源是 `metrics.yaml` 的文字定义，不是读实现反推
--------------------------------------------------------
下面每个期望值都是先按配置里写的定义手算，然后才跑实现比对的。反过来做（先读
实现、再写出与实现一致的期望）等于验证"实现符合我对实现的理解"，抓不到任何问题。

波形设计（时间单位 µs，vout_target = 0.8 V，误差带 ±1% = ±8 mV ⟹ [0.792, 0.808]）：

    [0,   100)   0.800 恒定                      阶跃前
    [100, 110]   0.800 → 0.750 线性              下冲，谷底 50 mV
    [110, 130]   0.750 → 0.810 线性              回升并超调 10 mV
    [130, 150]   0.810 → 0.804 线性              穿过误差带上沿后落入带内
    [150, 1000]  0.804 恒定                      稳定

由此手算：

    undershoot       = 0.800 - 0.750 = 0.050 V
    overshoot        = 0.810 - 0.800 = 0.010 V
    settling_time    最后一次离开误差带的时刻：段 4 上 0.810 降到 0.808 处，
                     t = 130 + 20 × (0.810-0.808)/(0.810-0.804) = 136.667 µs
                     相对阶跃触发点 ⟹ 36.667 µs
    output_ripple    稳态窗口（仿真末 10%，即 [900, 1000] µs）内恒为 0.804
                     ⟹ 峰峰值 0 V
    phase_peak_current  相电流峰值 40.0 A（见下方构造）
    obs.vout_min     全窗口最小 0.750 V
    obs.vout_max     全窗口最大 0.810 V
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from poweragent.config.loader import load_all
from poweragent.eval.metrics import compute_metrics
from poweragent.sim.backends.waveform_io import save_waveform_mat
from poweragent.store.repo import ScenarioSpec

pytestmark = pytest.mark.metric

VOUT_TARGET = 0.8
DT_S = 0.2e-6
STOP_TIME_S = 1000e-6
STEP_TRIGGER_S = 100e-6

# 手算期望值（依据见模块 docstring）
EXPECTED = {
    "undershoot": 0.050,
    "overshoot": 0.010,
    "settling_time": 36.667,  # µs
    "output_ripple": 0.0,
    "phase_peak_current": 40.0,
    "obs.vout_min": 0.750,
    "obs.vout_max": 0.810,
}

# 采样网格为 0.2 µs，误差带穿越点 136.667 µs 不落在网格上：最后一个带外样本在
# 136.6 µs、第一个带内样本在 136.8 µs。「取哪一侧」在 metrics.yaml 的文字定义下
# 都成立，因此允许一个采样间隔的偏差（也远小于该指标 0.5 µs 的比较容差）。
SETTLING_TOL_US = 0.3


def _synthetic_vout(t_s: np.ndarray) -> np.ndarray:
    """分段线性的 vout 波形，各拐点见模块 docstring。"""
    t_us = t_s * 1e6
    return np.interp(
        t_us,
        [0.0, 100.0, 110.0, 130.0, 150.0, 1000.0],
        [0.800, 0.800, 0.750, 0.810, 0.804, 0.804],
    )


def _synthetic_iphase(t_s: np.ndarray, n_phase: int) -> np.ndarray:
    """相电流：阶跃前 25 A，线性升到 40 A 峰值，之后恒定 37.5 A。

    刻意让峰值出现在中段而不是末段，这样 `max_over_phases` 聚合若错误地只取
    末值或首值都会被抓到。各相取相同值（均流），二维形状仍需正确。
    """
    t_us = t_s * 1e6
    single = np.where(
        t_us < 100.0,
        25.0,
        np.where(t_us <= 150.0, 25.0 + (40.0 - 25.0) * (t_us - 100.0) / 50.0, 37.5),
    )
    return np.repeat(single[:, None], n_phase, axis=1)


@pytest.fixture(scope="module")
def bundle(config_dir: Path):
    return load_all(config_dir)


@pytest.fixture(scope="module")
def waveform_ref(tmp_path_factory, bundle) -> str:
    """把合成波形写成 MAT 产物，返回其路径。

    刻意经过真实的落盘与加载往返，而不是直接构造 `Waveform` 对象：
    `_load_waveform()` 的格式假设是否与 `save_waveform_mat()` 写出的一致，
    本身就是需要被测的契约。
    """
    n = int(round(STOP_TIME_S / DT_S)) + 1
    t = np.linspace(0.0, STOP_TIME_S, n)

    signals = bundle.model.io_contract.output_signals
    path = tmp_path_factory.mktemp("waveform") / "synthetic.mat"

    save_waveform_mat(
        path,
        time_s=t,
        signals={
            signals.vout.logsout_name: _synthetic_vout(t),
            signals.iout.logsout_name: np.full(n, 150.0),
            signals.iphase.logsout_name: _synthetic_iphase(t, 4),
        },
        step_trigger_s=STEP_TRIGGER_S,
        sim_start_s=0.0,
        sim_end_s=STOP_TIME_S,
    )
    return str(path)


@pytest.fixture(scope="module")
def scenario() -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id="synthetic_eval",
        tier="evaluation",
        model_variant="switching",
        require_margin=False,
        vin_v=10.8,
        temp_c=85.0,
        load_start_a=100.0,
        load_end_a=150.0,
        slew_a_per_us=200.0,
        spec_version="test",
    )


@pytest.fixture(scope="module")
def results(waveform_ref, scenario, bundle) -> dict[str, object]:
    computed = compute_metrics(
        waveform_ref,
        scenario,
        bundle.metrics,
        run_id="run_synthetic",
        guard=bundle.model.io_contract.divergence_guard,
        vout_target_v=bundle.model.io_contract.vout_target_v,
    )
    return {r.metric_id: r for r in computed}


# --------------------------------------------------------------------------
# 产物往返
# --------------------------------------------------------------------------


def test_waveform_roundtrip_preserves_shapes(waveform_ref, bundle) -> None:
    """写出的 MAT 能被 `_load_waveform()` 读回，且信号形状与时间轴一致。

    这条测试守护的是生产侧与消费侧的格式契约。`_load_waveform()` 的 docstring
    把自己标注为「与 MATLAB 侧实际格式不一致的占位实现」——本项目的做法是让
    Python 后端写出它假定的格式，于是该实现转正。格式一旦漂移，这里先失败。
    """
    from poweragent.eval.metrics import _load_waveform

    w = _load_waveform(waveform_ref)
    signals = bundle.model.io_contract.output_signals

    assert w.step_trigger_s == pytest.approx(STEP_TRIGGER_S)
    assert w.sim_start_s == pytest.approx(0.0)
    assert w.sim_end_s == pytest.approx(STOP_TIME_S)

    vout_time, vout_data = w.signals[signals.vout.logsout_name]
    assert vout_time.shape == vout_data.shape

    _, iphase_data = w.signals[signals.iphase.logsout_name]
    assert iphase_data.ndim == 2, "相电流必须保持 (n_time, n_phase) 二维形状"
    assert iphase_data.shape == (vout_time.size, 4)


# --------------------------------------------------------------------------
# 结果集完备性
# --------------------------------------------------------------------------


def test_all_time_domain_metrics_and_observables_are_produced(results, bundle) -> None:
    """五项时域指标各一条，两项约束支撑观测量各一条。

    裕量类指标（`phase_margin` / `gain_margin`）来自频响而非波形，`compute_metrics`
    天然不产出它们——若这里出现了它们，说明时域与频域的职责边界被破坏了。
    """
    expected_time_domain = {
        "output_ripple", "overshoot", "undershoot", "settling_time", "phase_peak_current"
    }
    assert expected_time_domain <= set(results)
    assert {"obs.vout_min", "obs.vout_max"} <= set(results)
    assert "phase_margin" not in results
    assert "gain_margin" not in results


def test_every_metric_is_valid_on_a_well_formed_waveform(results) -> None:
    """一条形状完好、未越过发散判据、含阶跃触发点的波形上，全部指标应有效。

    任一条落 invalid 都说明某个无效谓词误判——这类误判会让好候选被当成无效丢弃，
    而且因为不报错所以很难发现。
    """
    for metric_id, result in results.items():
        assert result.valid, f"{metric_id} 意外落 invalid: {result.invalid_reason}"
        assert result.value is not None


# --------------------------------------------------------------------------
# 数值核对：期望值全部由 metrics.yaml 的定义手算
# --------------------------------------------------------------------------


def test_undershoot_matches_hand_computed_value(results) -> None:
    """下冲 = vout_target − 窗口内最小值 = 0.800 − 0.750 = 0.050 V。"""
    assert results["undershoot"].value == pytest.approx(EXPECTED["undershoot"], abs=1e-6)


def test_overshoot_matches_hand_computed_value(results) -> None:
    """超调 = 窗口内最大值 − vout_target = 0.810 − 0.800 = 0.010 V。"""
    assert results["overshoot"].value == pytest.approx(EXPECTED["overshoot"], abs=1e-6)


def test_settling_time_matches_hand_computed_value(results) -> None:
    """恢复时间 = 最后一次离开 ±8 mV 误差带的时刻 − 阶跃触发时刻。

    段 4 上 0.810 线性降到 0.804，穿过 0.808 于 130 + 20×(0.002/0.006) = 136.667 µs，
    相对触发点 100 µs 得 36.667 µs。

    同时校验单位：`metrics.yaml` 声明 `unit: us`，因此返回值应是以微秒计的数值，
    而不是秒。这一条能抓住"忘记换算单位"这个最常见也最致命的错误——若返回秒，
    数值会是 3.67e-5，与期望相差 6 个数量级。
    """
    assert results["settling_time"].value == pytest.approx(
        EXPECTED["settling_time"], abs=SETTLING_TOL_US
    )


def test_output_ripple_matches_hand_computed_value(results) -> None:
    """稳态窗口（仿真末 10%）内电压恒定 ⟹ 峰峰值为零。

    该窗口内信号恒为 0.804 V，因此无论 `filter` 取何带宽，滤波后仍是常数，
    峰峰值都应为零。
    """
    assert results["output_ripple"].value == pytest.approx(
        EXPECTED["output_ripple"], abs=1e-9
    )


def test_phase_peak_current_matches_hand_computed_value(results) -> None:
    """相电流峰值 = 40.0 A，出现在阶跃后中段而非首末端。

    `aggregation: max_over_phases` 需要同时跨相与跨时间聚合。峰值刻意放在中段：
    若实现只取末值（37.5 A）或首值（25 A），这里就会失败。
    """
    assert results["phase_peak_current"].value == pytest.approx(
        EXPECTED["phase_peak_current"], abs=1e-6
    )


def test_constraint_observables_match_hand_computed_values(results) -> None:
    """两项观测量取全仿真窗口的极值，不是阶跃窗口的极值。

    窗口是 `[sim_start, sim_end]`：阶跃前的 0.800 段也计入，因此最小值仍为谷底
    0.750、最大值仍为峰值 0.810。
    """
    assert results["obs.vout_min"].value == pytest.approx(
        EXPECTED["obs.vout_min"], abs=1e-6
    )
    assert results["obs.vout_max"].value == pytest.approx(
        EXPECTED["obs.vout_max"], abs=1e-6
    )


def test_output_ripple_measures_a_known_sinusoidal_ripple(
    tmp_path: Path, bundle, scenario
) -> None:
    """在恒定电压上叠加峰峰值已知的正弦纹波，指标应测回该峰峰值。

    上一条测试证明了滤波器不会在恒定信号上凭空造出纹波，但那只是一半——还要确认
    它没有把真实纹波也一起抹平。若滤波窗口取得过宽，真实纹波会被削掉，指标恒为
    接近零的值，同样是不可用的。

    纹波取 100 kHz：远低于 `filter` 声明的 2 MHz 带宽，因此应基本无衰减地通过。
    采样 0.2 µs 给出每周期 50 点，离散化造成的峰值低估约 0.2%，滤波窗口
    （2 个样本）带来的衰减约 0.2%，故用 3% 的相对容差。
    """
    ripple_pp_v = 4.0e-3
    ripple_hz = 100.0e3

    n = int(round(STOP_TIME_S / DT_S)) + 1
    t = np.linspace(0.0, STOP_TIME_S, n)
    vout = VOUT_TARGET + (ripple_pp_v / 2.0) * np.sin(2.0 * np.pi * ripple_hz * t)

    signals = bundle.model.io_contract.output_signals
    path = tmp_path / "ripple.mat"
    save_waveform_mat(
        path,
        time_s=t,
        signals={
            signals.vout.logsout_name: vout,
            signals.iout.logsout_name: np.full(n, 150.0),
            signals.iphase.logsout_name: np.full((n, 4), 37.5),
        },
        # 这条波形没有负载阶跃，因此不写触发时刻——引用 step_trigger 系窗口的指标
        # 会如实落 no_step_detected，而 output_ripple 的窗口不依赖它。
        step_trigger_s=None,
        sim_start_s=0.0,
        sim_end_s=STOP_TIME_S,
    )

    computed = {
        r.metric_id: r
        for r in compute_metrics(
            str(path),
            scenario,
            bundle.metrics,
            run_id="run_ripple",
            guard=bundle.model.io_contract.divergence_guard,
            vout_target_v=bundle.model.io_contract.vout_target_v,
        )
    }

    assert computed["output_ripple"].valid
    assert computed["output_ripple"].value == pytest.approx(ripple_pp_v, rel=0.03)

    # 没有阶跃触发点时，依赖它的指标必须如实报告原因，而不是猜一个时刻。
    assert computed["settling_time"].invalid_reason == "no_step_detected"


def test_observables_bracket_the_undershoot_and_overshoot(results) -> None:
    """观测量与时域指标在同一条波形上必须自洽。

    `obs.vout_min` 与 `undershoot` 描述同一个谷底、`obs.vout_max` 与 `overshoot`
    描述同一个峰值，只是表达方式不同（绝对电压 vs 相对目标的偏差）。两者不自洽
    说明窗口解析或聚合方式在某一侧出了错。
    """
    assert results["obs.vout_min"].value == pytest.approx(
        VOUT_TARGET - results["undershoot"].value, abs=1e-6
    )
    assert results["obs.vout_max"].value == pytest.approx(
        VOUT_TARGET + results["overshoot"].value, abs=1e-6
    )
