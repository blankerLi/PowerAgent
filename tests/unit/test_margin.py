"""频域裕量提取的正确性与两条计算路径的交叉核对。

裕量是四条硬约束里唯一来自频域的一条，也是唯一能给搜索空间提供**上界**的那条——
时域指标在两个设计维度上都单调偏好"大 Rcomp、小 Ccomp"，若没有裕量约束，寻优会
直接把两个参数推到域边界。所以这里算错的后果不是某个数字偏了，而是整个优化问题
失去意义。

测试分三组：

1. **两条路径一致**：数值线性化的状态空间 vs 手写解析传递函数。对应
   `metrics.yaml` 的 `cross_check_method = manual_bode_reference`。核对的是实现
   有无差错（矩阵搭错、多项式系数写反、单位漏换算），不是两种建模假设是否一致。
2. **裕量对参数的依赖关系**：这些关系可以从传递函数结构独立推出，因此是比"某个
   具体数值"更强的断言。
3. **纯延迟的必要性**：反面验证——若采样保持用时域那个一阶滞后近似，相位根本
   穿不过 -180°，增益裕量无从定义。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from poweragent.sim.backends.buck_model import OperatingCondition, load_buck_spec
from poweragent.sim.backends.margin import (
    DEFAULT_FREQ_HZ,
    margins_from_analytic,
    margins_from_state_space,
    open_loop_analytic,
    save_freq_response_mat,
)

pytestmark = pytest.mark.sim

BASELINE = (12000.0, 2.2e-9)
GRID_BEST = (18738.0, 8.111e-10)

EVALUATION = OperatingCondition(
    vin_v=10.8, temp_c=85.0, load_start_a=100.0, load_end_a=150.0, slew_a_per_us=200.0
)

# 与 configs/constraints.yaml 的 phase_margin_min 一致。
PHASE_MARGIN_MIN_DEG = 45.0
# 与 configs/metrics.yaml 的 margin_extraction.cross_check_tolerance 一致。
CROSS_CHECK_PHASE_TOL_DEG = 3.0
CROSS_CHECK_GAIN_TOL_DB = 1.0


@pytest.fixture(scope="module")
def spec(repo_root: Path):
    return load_buck_spec(repo_root / "models" / "buck4ph_averaged.yaml")


# --------------------------------------------------------------------------
# 两条路径的交叉核对
# --------------------------------------------------------------------------


@pytest.mark.parametrize("params", [BASELINE, GRID_BEST], ids=["baseline", "grid-best"])
def test_two_paths_agree_within_configured_tolerance(spec, params) -> None:
    """状态空间路径与解析路径的裕量落在配置声明的核对容差内。

    容差取自 `metrics.yaml` 的 `cross_check_tolerance`（3° / 1 dB）。两条路径描述
    同一个数学对象，实际偏差应远小于该容差——容差是留给"两种方法"的，而这里两种
    实现只应差在浮点舍入上。
    """
    primary = margins_from_state_space(spec, EVALUATION, rcomp_ohm=params[0], ccomp_f=params[1])
    reference = margins_from_analytic(spec, EVALUATION, rcomp_ohm=params[0], ccomp_f=params[1])

    assert abs(primary.phase_margin_deg - reference.phase_margin_deg) < CROSS_CHECK_PHASE_TOL_DEG
    assert abs(primary.gain_margin_db - reference.gain_margin_db) < CROSS_CHECK_GAIN_TOL_DB

    # 实际应该好得多：同一对象的两种算法，只差浮点舍入。
    assert primary.phase_margin_deg == pytest.approx(reference.phase_margin_deg, abs=1e-6)
    assert primary.gain_margin_db == pytest.approx(reference.gain_margin_db, abs=1e-6)
    assert primary.crossover_hz == pytest.approx(reference.crossover_hz, rel=1e-6)


def test_analytic_transfer_function_has_expected_structure(spec) -> None:
    """解析传递函数的零极点结构与推导一致：两个零点、双重原点极点。

    分子 = gm(1+s·Rc·Cc)(1+s·Cout·ESR) ⟹ 两个实零点在 1/(Rc·Cc) 与 1/(Cout·ESR)。
    分母 = Ri·Cc·Cout·s² ⟹ 两个原点极点（补偿器积分 + 输出电容积分）。

    结构写错（比如漏掉一个积分）会让低频相位差 90°，而低频相位决定了相位裕量的
    基准，这类错误在单点数值上不容易看出来。
    """
    rcomp, ccomp = BASELINE
    num, den = open_loop_analytic(spec, EVALUATION, rcomp_ohm=rcomp, ccomp_f=ccomp)

    assert len(num) == 3, "分子应为二次多项式（两个零点）"
    assert len(den) == 3 and den[1] == 0.0 and den[2] == 0.0, "分母应为纯 s^2"

    zeros = np.roots(num)
    expected = sorted([-1.0 / (rcomp * ccomp), -1.0 / (spec.cout_f * spec.cout_esr_ohm)])
    assert sorted(zeros.real) == pytest.approx(expected, rel=1e-9)
    assert np.allclose(zeros.imag, 0.0), "两个零点应为实数"


# --------------------------------------------------------------------------
# 裕量对参数的依赖关系
# --------------------------------------------------------------------------


def test_gain_margin_depends_on_rcomp_but_not_on_ccomp(spec) -> None:
    """增益裕量只由 Rcomp 决定，与 Ccomp 无关。

    可从结构推出：高频下两个零点都已生效，开环增益趋于常数 Rc·gm·ESR/Ri，而相位
    穿越 -180° 的位置由纯延迟决定（约 1/(2·Td)），也与 Ccomp 无关。因此增益裕量
    只跟 Rcomp 走。

    这条比断言某个具体 dB 值更有力：它检验的是传递函数各项是否接到了正确的位置。
    """
    rcomp = GRID_BEST[0]
    margins = [
        margins_from_state_space(spec, EVALUATION, rcomp_ohm=rcomp, ccomp_f=cc).gain_margin_db
        for cc in (1e-10, 1e-9, 1e-8)
    ]

    assert margins[0] == pytest.approx(margins[1], abs=0.05)
    assert margins[1] == pytest.approx(margins[2], abs=0.05)


def test_gain_margin_decreases_as_rcomp_rises(spec) -> None:
    """Rcomp 增大使高频开环增益上升，增益裕量单调下降。

    这是搜索空间在 Rcomp 方向的物理上界：高频增益 Rc·gm·ESR/Ri 超过 1 时，相位
    穿越 -180° 处的增益仍大于 1，环路不稳定。输出电容 ESR 零点使高频增益不再滚降
    是这个机理的根源。
    """
    ccomp = 2.2e-9
    rcomps = [5337.0, 12328.0, 28480.0, 65793.0]
    margins = [
        margins_from_state_space(spec, EVALUATION, rcomp_ohm=rc, ccomp_f=ccomp).gain_margin_db
        for rc in rcomps
    ]

    assert margins == sorted(margins, reverse=True), f"增益裕量应随 Rcomp 单调下降: {margins}"
    assert margins[-1] < 6.0, "域上限附近的增益裕量应已低到工程上不可接受"


def test_unstable_loop_has_no_definable_margin(spec) -> None:
    """Rcomp 取域上限时高频增益超过 1，幅值不再穿越，裕量无从定义。

    此时抛 `ValueError` 而不是返回一个编造的数字。`eval/margin.py` 会把它转译成
    `extraction_failed`，两条裕量指标同步失效，候选因约束无法判定而被拒——这是
    正确的处理：不稳定的设计不该因为"算不出裕量"而蒙混过关。
    """
    with pytest.raises(ValueError, match="幅值未穿越"):
        margins_from_state_space(spec, EVALUATION, rcomp_ohm=1.0e5, ccomp_f=2.2e-9)


def test_phase_margin_rises_as_compensation_zero_moves_below_crossover(spec) -> None:
    """Ccomp 增大使补偿零点下移到穿越频率之下，相位裕量提高。

    零点在 fc 之上时提供不了相位提升，相位裕量随之不足——这正是低 Rcomp 配大 Ccomp
    组合不可行的原因（此时 fz = 1/(2π·Rc·Cc) 被推到 fc 之上）。
    """
    rcomp = GRID_BEST[0]
    ccomps = [1e-10, 5e-10, 2e-9, 1e-8]
    margins = [
        margins_from_state_space(spec, EVALUATION, rcomp_ohm=rcomp, ccomp_f=cc).phase_margin_deg
        for cc in ccomps
    ]

    assert margins == sorted(margins), f"相位裕量应随 Ccomp 单调上升: {margins}"


def test_low_rcomp_with_large_ccomp_violates_phase_margin(spec) -> None:
    """低 Rcomp 配大 Ccomp 的相位裕量低于约束下限：搜索空间的下界。

    与上界（增益裕量）机理不同：这里 fz = 1/(2π·Rc·Cc) 落在穿越频率之上，零点来不及
    补相位。两条约束因此各管一个方向，可行域被夹在中间。
    """
    result = margins_from_state_space(spec, EVALUATION, rcomp_ohm=1000.0, ccomp_f=2.2e-9)

    assert result.phase_margin_deg < PHASE_MARGIN_MIN_DEG


def test_baseline_and_grid_best_both_satisfy_phase_margin(spec) -> None:
    """工程基线与网格最优点都满足相位裕量约束。

    若基线本身就违反约束，M1 出口的基线门禁会拿一个不可行的参考点去做前后对比，
    改进量也就无从谈起。
    """
    for rcomp, ccomp in (BASELINE, GRID_BEST):
        result = margins_from_state_space(spec, EVALUATION, rcomp_ohm=rcomp, ccomp_f=ccomp)
        assert result.phase_margin_deg >= PHASE_MARGIN_MIN_DEG
        assert result.gain_margin_db > 0.0


def test_higher_temperature_shifts_the_crossover(spec) -> None:
    """温度升高使电流检测增益上升、开环增益下降，穿越频率随之下移。

    这是评价工况取高温的频域体现：与时域侧"下冲变深"是同一个原因的两种表现。
    """
    cold = OperatingCondition(10.8, 25.0, 100.0, 150.0, 200.0)
    hot = EVALUATION

    fc_cold = margins_from_state_space(spec, cold, rcomp_ohm=BASELINE[0], ccomp_f=BASELINE[1])
    fc_hot = margins_from_state_space(spec, hot, rcomp_ohm=BASELINE[0], ccomp_f=BASELINE[1])

    assert fc_hot.crossover_hz < fc_cold.crossover_hz


# --------------------------------------------------------------------------
# 纯延迟的必要性（反面验证）
# --------------------------------------------------------------------------


def test_pure_delay_makes_the_phase_cross_minus_180(spec) -> None:
    """纯延迟使相位无界下降，因而存在相位穿越点、增益裕量有限。"""
    result = margins_from_state_space(
        spec, EVALUATION, rcomp_ohm=BASELINE[0], ccomp_f=BASELINE[1]
    )

    assert result.phase_deg.min() < -180.0, "相位应穿越 -180°"
    assert math.isfinite(result.gain_margin_db)
    # 穿越点应在 1/(2*Td) 附近——纯延迟贡献 180° 滞后所需的频率。
    assert result.phase_crossover_hz == pytest.approx(
        1.0 / (2.0 * spec.modulator_delay_s), rel=0.3
    )


def test_first_order_lag_approximation_never_crosses_minus_180(spec) -> None:
    """反面验证：若采样保持用一阶滞后近似，相位永远到不了 -180°。

    结构上：两个原点极点给 -180°，两个零点在高频回补 +180°，一阶滞后最多再贡献
    -90°，高频渐近相位是 -90°。因此增益裕量在数学上无界，落到 `eval/margin.py`
    会被判为 `extraction_failed`，还会把本来算得出来的相位裕量一并拖失效。

    这解释了为什么时域与频域刻意采用不同的采样保持表达：时域必须用有理近似（纯延迟
    会让 ODE 变成延迟微分方程），频域必须用精确延迟（否则拿不到增益裕量）。
    """
    rcomp, ccomp = BASELINE
    num, den = open_loop_analytic(spec, EVALUATION, rcomp_ohm=rcomp, ccomp_f=ccomp)

    s = 1j * 2.0 * np.pi * DEFAULT_FREQ_HZ
    rational = np.polyval(num, s) / np.polyval(den, s)
    with_lag = rational / (1.0 + s * spec.modulator_delay_s)

    phase_deg = np.degrees(np.unwrap(np.angle(with_lag)))
    assert phase_deg.min() > -180.0, "一阶滞后近似下相位不应穿越 -180°"


# --------------------------------------------------------------------------
# 产物落盘与下游消费
# --------------------------------------------------------------------------


def test_freq_response_artefact_is_readable_by_eval_margin(
    spec, tmp_path: Path, config_dir: Path
) -> None:
    """落盘的频响产物能被 `eval/margin.py::extract_margin()` 正确读取。

    这条打通生产侧与消费侧的契约：`GainMargin` 必须写成**线性比值**，因为消费侧
    会自行做 20·log10 换算。写成 dB 会让报告里的增益裕量凭空变成 20·log10(dB 值)，
    数值仍然"看起来正常"，非常难发现。
    """
    from poweragent.config.loader import load_all
    from poweragent.eval.margin import extract_margin

    result = margins_from_state_space(
        spec, EVALUATION, rcomp_ohm=BASELINE[0], ccomp_f=BASELINE[1]
    )
    path = save_freq_response_mat(tmp_path / "freq_response.mat", result)
    assert path.is_file()

    bundle = load_all(config_dir)
    phase_margin, gain_margin = extract_margin(
        str(path), bundle.metrics, run_id="run_margin"
    )

    assert phase_margin.valid and gain_margin.valid
    assert phase_margin.metric_id == "phase_margin"
    assert gain_margin.metric_id == "gain_margin"
    assert phase_margin.value == pytest.approx(result.phase_margin_deg, abs=1e-6)
    # 消费侧做了 20*log10 换算，应还原出与生产侧一致的 dB 值。
    assert gain_margin.value == pytest.approx(result.gain_margin_db, abs=1e-6)


# --------------------------------------------------------------------------
# 交叉核对记录（scripts/margin_cross_check.py 的产物）
# --------------------------------------------------------------------------


def test_cross_check_record_exists_and_matches_configured_hash(
    repo_root: Path, config_dir: Path
) -> None:
    """核对记录存在，且其 sha256 与 `metrics.yaml` 声明的一致。

    这与 `preflight` 的 `check_margin_extraction_ready()` 检查同一件事，但在测试里
    失败得更早、报错更直白。记录不入库时这条会失败——它确实曾经会失败，因为
    `artifacts/` 整个被 gitignore 掉了；核对记录是一次性证据资产而非可重建产物，
    现已单独放行入库。
    """
    import hashlib

    from poweragent.config.loader import load_all

    bundle = load_all(config_dir)
    record_path = repo_root / bundle.metrics.margin_extraction.cross_check_record.path

    assert record_path.is_file(), (
        f"缺少核对记录 {record_path}；请运行 python scripts/margin_cross_check.py"
    )
    digest = hashlib.sha256(record_path.read_bytes()).hexdigest()
    assert digest == bundle.metrics.margin_extraction.cross_check_record.sha256


def test_cross_check_record_reports_agreement(repo_root: Path, config_dir: Path) -> None:
    """记录的结论是"两法一致"，且最差偏差在配置容差内。

    `ok=false` 的记录即使哈希对得上也不该放行——那表示核对本身没通过。
    """
    import json

    from poweragent.config.loader import load_all

    bundle = load_all(config_dir)
    record_path = repo_root / bundle.metrics.margin_extraction.cross_check_record.path
    record = json.loads(record_path.read_text(encoding="utf-8"))

    tolerance = bundle.metrics.margin_extraction.cross_check_tolerance
    assert record["ok"] is True
    assert record["worst_delta"]["phase_deg"] <= tolerance.phase_deg
    assert record["worst_delta"]["gain_db"] <= tolerance.gain_db
    assert record["primary_method"] == bundle.metrics.margin_extraction.primary_method
    assert record["cross_check_method"] == bundle.metrics.margin_extraction.cross_check_method


def test_cross_check_record_still_reflects_current_code(
    spec, repo_root: Path, config_dir: Path
) -> None:
    """重算记录中每个点的裕量，结果应与记录内的数值一致。

    这条防的是记录与代码漂移：哈希门禁只能保证"记录没被改过"，不能保证"记录仍然
    描述当前的模型"。若改了模型或裕量算法却忘了重跑核对脚本，哈希照样匹配、
    preflight 照样放行，而记录里的数字已经过期。
    """
    import json

    from poweragent.config.loader import load_all

    bundle = load_all(config_dir)
    record_path = repo_root / bundle.metrics.margin_extraction.cross_check_record.path
    record = json.loads(record_path.read_text(encoding="utf-8"))

    scenario = next(s for s in bundle.task.scenarios if s.require_margin)
    assert record["scenario_id"] == scenario.scenario_id
    condition = OperatingCondition(
        vin_v=scenario.vin_v,
        temp_c=scenario.temp_c,
        load_start_a=scenario.load_start_a,
        load_end_a=scenario.load_end_a,
        slew_a_per_us=scenario.slew_a_per_us,
    )

    for point in record["points"]:
        rcomp, ccomp = point["rcomp_ohm"], point["ccomp_f"]

        if point["status"] == "margin_undefined":
            with pytest.raises(ValueError):
                margins_from_state_space(
                    spec, condition, rcomp_ohm=rcomp, ccomp_f=ccomp
                )
            continue

        recomputed = margins_from_state_space(
            spec, condition, rcomp_ohm=rcomp, ccomp_f=ccomp
        )
        assert recomputed.phase_margin_deg == pytest.approx(
            point["primary"]["phase_margin_deg"], abs=1e-5
        ), f"{point['label']} 的相位裕量已与记录不符，请重跑 scripts/margin_cross_check.py"
        assert recomputed.gain_margin_db == pytest.approx(
            point["primary"]["gain_margin_db"], abs=1e-5
        ), f"{point['label']} 的增益裕量已与记录不符，请重跑 scripts/margin_cross_check.py"
