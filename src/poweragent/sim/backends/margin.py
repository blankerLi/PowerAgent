"""频域稳定性裕量：开环 Bode 与增益/相位裕量。

对应 `metrics.yaml` 的 `margin_extraction.primary_method =
linear_analysis_on_averaged`：在平均模型的稳态工作点上把电压外环线性化，求开环
频响，取增益裕量与相位裕量。

开环传递函数
------------
在误差信号处断环（输入 u = verr，输出 vout），由平均模型的方程逐项写出：

    补偿器      vcomp/u    = gm * (1 + s*Rc*Cc) / (s*Cc)
    电流检测    iL_cmd     = vcomp / Ri
    采样保持    iL/iL_cmd  = exp(-s*Td),  Td = 0.5/(N*fsw)
    输出阻抗    vout/iL    = (1 + s*C*ESR) / (s*C)

    T(s) = gm*(1+s*Rc*Cc)*(1+s*C*ESR) / (Ri*Cc*C*s^2) * exp(-s*Td)

两个积分（补偿器与输出电容）给出 -180° 的低频相位，两个零点各回补最多 +90°，
纯延迟则让相位无界下降。

为什么频域用纯延迟、时域用一阶滞后
----------------------------------
时域求解不能带纯延迟——那会把常微分方程变成延迟微分方程，需要维护历史缓存，
求解器与可复现性都要重做。所以时域用一阶滞后 1/(1+s*Td) 近似它。

但频域**必须**用精确的纯延迟，否则增益裕量根本不存在：一阶滞后的相位下界是
-90°，加上两个积分与两个零点，总相位最低只到 -180° 的渐近线而永不穿越，
按定义就得不到有限的增益裕量。落到下游更糟——`eval/margin.py` 对非有限的
`GainMargin` 会判为 `extraction_failed`，而相位裕量与增益裕量在那里是同步成败的，
于是一个本来算得出来的相位裕量会被一并丢弃。

换用纯延迟后相位无界下降，增益裕量有限，而且它揭示了一个真实的失稳机理：输出
电容 ESR 零点使高频开环增益不再滚降，趋于常数 Rc*gm*ESR/Ri。当 Rcomp 大到让这个
常数超过 1 时，相位穿越 -180° 处的增益仍大于 1，环路不稳定。这正是搜索空间在
Rcomp 方向上的物理上界，与 Ccomp 方向上由补偿零点位置决定的相位裕量下界互相独立。

两条独立计算路径
----------------
`margins_from_state_space()` 从数值线性化的状态空间求频响；
`margins_from_analytic()` 从解析多项式求频响。两者是同一个数学对象的不同实现
路径，用于 `margin_extraction.cross_check_method = manual_bode_reference` 的交叉
核对——核对的是实现是否有误（矩阵搭错、多项式系数写反、单位换算漏项），不是两种
建模假设是否一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import savemat

from poweragent.sim.backends.buck_model import BuckSpec, OperatingCondition

__all__ = [
    "MarginResult",
    "open_loop_state_space",
    "open_loop_analytic",
    "margins_from_state_space",
    "margins_from_analytic",
    "save_freq_response_mat",
    "DEFAULT_FREQ_HZ",
]

# 频率网格：1 Hz ~ 10 MHz，每十倍频 240 点。下限要足够低——积分器使低频增益很大，
# 幅值穿越点必须落在网格内；上限要覆盖相位穿越 -180° 的位置（约 1/(2*Td) 量级）。
DEFAULT_FREQ_HZ = np.logspace(0.0, 7.0, 7 * 240 + 1)


@dataclass(frozen=True, slots=True)
class MarginResult:
    """一次裕量提取的结果。

    `gain_margin_linear` 是**线性比值**而非 dB：`eval/margin.py` 期望 MAT 中的
    `GainMargin` 为线性值并自行做 20*log10 换算，这里保持同一口径，避免在两处
    各换算一次。
    """

    phase_margin_deg: float
    gain_margin_linear: float
    crossover_hz: float
    phase_crossover_hz: float
    freq_hz: np.ndarray
    magnitude: np.ndarray
    phase_deg: np.ndarray

    @property
    def gain_margin_db(self) -> float:
        return 20.0 * math.log10(self.gain_margin_linear)


def open_loop_state_space(
    spec: BuckSpec,
    condition: OperatingCondition,
    *,
    rcomp_ohm: float,
    ccomp_f: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """开环有理部分的状态空间 `(A, B, C, D)`，输入 verr、输出 vout。

    状态取 `[xc, vc]`——补偿积分状态与输出电容电压。电感电流不作为状态：峰值电流
    模式下它逐周期跟踪指令，其唯一动态是采样保持，而采样保持在本模块中以纯延迟
    形式单独乘在频响上（见模块 docstring），不进入这个有理部分。

    工作点相关的量只有电流检测增益 `Ri`（随温度变化），因此不同温度下的 `A/B/C/D`
    不同——这也是评价工况取高温的作用之一。
    """
    ri = spec.sense_gain_ohm(condition.temp_c)
    gm = spec.gm_s
    cout = spec.cout_f
    esr = spec.cout_esr_ohm

    # xc' = gm*u/Cc
    # iL  = (xc + Rc*gm*u)/Ri            （代数关系，非状态）
    # vc' = iL/Cout
    # vout = vc + ESR*iL
    a = np.array([[0.0, 0.0], [1.0 / (ri * cout), 0.0]])
    b = np.array([gm / ccomp_f, rcomp_ohm * gm / (ri * cout)])
    c = np.array([esr / ri, 1.0])
    d = esr * rcomp_ohm * gm / ri

    return a, b, c, d


def open_loop_analytic(
    spec: BuckSpec,
    condition: OperatingCondition,
    *,
    rcomp_ohm: float,
    ccomp_f: float,
) -> tuple[np.ndarray, np.ndarray]:
    """开环有理部分的分子/分母多项式系数（降幂序，与 `numpy.polyval` 一致）。

        num = gm * (1 + s*Rc*Cc) * (1 + s*Cout*ESR)
        den = Ri * Cc * Cout * s^2

    与 `open_loop_state_space()` 描述同一个传递函数，但由手写的传递函数式直接
    给出，作为交叉核对的独立路径。
    """
    ri = spec.sense_gain_ohm(condition.temp_c)
    gm = spec.gm_s
    cout = spec.cout_f
    esr = spec.cout_esr_ohm

    zero_comp = np.array([rcomp_ohm * ccomp_f, 1.0])  # 1 + s*Rc*Cc
    zero_esr = np.array([cout * esr, 1.0])  # 1 + s*Cout*ESR
    num = gm * np.convolve(zero_comp, zero_esr)
    den = np.array([ri * ccomp_f * cout, 0.0, 0.0])  # Ri*Cc*Cout*s^2

    return num, den


def _apply_delay(freq_hz: np.ndarray, response: np.ndarray, delay_s: float) -> np.ndarray:
    """在有理部分的频响上乘精确的纯延迟 exp(-j*2*pi*f*Td)。"""
    return response * np.exp(-1j * 2.0 * np.pi * freq_hz * delay_s)


def _stability_margins(
    freq_hz: np.ndarray, response: np.ndarray
) -> tuple[float, float, float, float]:
    """从频响求 `(相位裕量 deg, 增益裕量 线性比值, 幅值穿越 Hz, 相位穿越 Hz)`。

    - 幅值穿越点：|T| 由大于 1 变为小于 1 处，在对数频率上线性插值。存在多个穿越
      时取**最小**相位裕量，即最坏情形。
    - 相位穿越点：相位由高于 -180° 变为低于 -180° 处，同样取最坏（最小）增益裕量。

    找不到穿越点即抛 `ValueError`，由调用方决定如何转译——不返回一个编造的默认值。
    本模型下两种穿越都必然存在：低频有两个积分使 |T| 很大而高频趋于常数
    `Rc*gm*ESR/Ri`（幅值穿越），纯延迟使相位无界下降（相位穿越）。
    """
    magnitude = np.abs(response)
    phase_deg = np.degrees(np.unwrap(np.angle(response)))

    def _interp_crossings(values: np.ndarray, level: float) -> list[tuple[float, int]]:
        """返回 `values` 由上往下穿过 `level` 的 (频率, 左侧索引) 列表。"""
        crossings: list[tuple[float, int]] = []
        above = values > level
        for i in range(len(values) - 1):
            if above[i] and not above[i + 1]:
                y0, y1 = values[i], values[i + 1]
                # 在 log10(f) 上线性插值，与对数频率网格一致
                x0, x1 = math.log10(freq_hz[i]), math.log10(freq_hz[i + 1])
                t = (y0 - level) / (y0 - y1)
                crossings.append((10.0 ** (x0 + t * (x1 - x0)), i))
        return crossings

    gain_crossings = _interp_crossings(magnitude, 1.0)
    if not gain_crossings:
        raise ValueError("幅值未穿越 1，无法定义相位裕量")

    phase_margins: list[tuple[float, float]] = []
    for f_c, _ in gain_crossings:
        phase_at_fc = float(np.interp(math.log10(f_c), np.log10(freq_hz), phase_deg))
        phase_margins.append((180.0 + phase_at_fc, f_c))
    phase_margin_deg, crossover_hz = min(phase_margins, key=lambda pair: pair[0])

    phase_crossings = _interp_crossings(phase_deg, -180.0)
    if not phase_crossings:
        raise ValueError("相位未穿越 -180°，无法定义增益裕量")

    gain_margins: list[tuple[float, float]] = []
    for f_180, _ in phase_crossings:
        mag_at_f180 = float(
            10.0
            ** np.interp(
                math.log10(f_180), np.log10(freq_hz), np.log10(np.maximum(magnitude, 1e-300))
            )
        )
        gain_margins.append((1.0 / mag_at_f180, f_180))
    gain_margin_linear, phase_crossover_hz = min(gain_margins, key=lambda pair: pair[0])

    return phase_margin_deg, gain_margin_linear, crossover_hz, phase_crossover_hz


def _build_result(
    freq_hz: np.ndarray, response: np.ndarray
) -> MarginResult:
    pm, gm_linear, f_gain, f_phase = _stability_margins(freq_hz, response)
    return MarginResult(
        phase_margin_deg=pm,
        gain_margin_linear=gm_linear,
        crossover_hz=f_gain,
        phase_crossover_hz=f_phase,
        freq_hz=freq_hz,
        magnitude=np.abs(response),
        phase_deg=np.degrees(np.unwrap(np.angle(response))),
    )


def margins_from_state_space(
    spec: BuckSpec,
    condition: OperatingCondition,
    *,
    rcomp_ohm: float,
    ccomp_f: float,
    freq_hz: np.ndarray = DEFAULT_FREQ_HZ,
) -> MarginResult:
    """主方法：数值线性化的状态空间求频响，乘纯延迟后取裕量。"""
    a, b, c, d = open_loop_state_space(
        spec, condition, rcomp_ohm=rcomp_ohm, ccomp_f=ccomp_f
    )
    s = 1j * 2.0 * np.pi * freq_hz
    identity = np.eye(a.shape[0])

    # 逐频点解 (sI - A)x = B，再取 C·x + D。频点数为几千，直接求解 2x2 足够快。
    response = np.empty(freq_hz.shape, dtype=complex)
    for k, s_k in enumerate(s):
        x = np.linalg.solve(s_k * identity - a, b)
        response[k] = c @ x + d

    return _build_result(freq_hz, _apply_delay(freq_hz, response, spec.modulator_delay_s))


def margins_from_analytic(
    spec: BuckSpec,
    condition: OperatingCondition,
    *,
    rcomp_ohm: float,
    ccomp_f: float,
    freq_hz: np.ndarray = DEFAULT_FREQ_HZ,
) -> MarginResult:
    """交叉核对方法：解析多项式求频响，乘纯延迟后取裕量。"""
    num, den = open_loop_analytic(
        spec, condition, rcomp_ohm=rcomp_ohm, ccomp_f=ccomp_f
    )
    s = 1j * 2.0 * np.pi * freq_hz
    response = np.polyval(num, s) / np.polyval(den, s)

    return _build_result(freq_hz, _apply_delay(freq_hz, response, spec.modulator_delay_s))


def save_freq_response_mat(path: str | Path, result: MarginResult) -> Path:
    """按 `eval/margin.py::_extract_from_averaged_linearization()` 期望的布局落盘。

    该函数读取 MAT 中的 `margin_data` 结构，取 `PhaseMargin`（度）与 `GainMargin`
    （**线性比值**，它自行做 20*log10 换算）。同时写入频响曲线本身，供报告绘制
    Bode 图与事后复核——这些额外变量不影响提取（它只按名取 `margin_data`）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    savemat(
        str(target),
        {
            "margin_data": {
                "PhaseMargin": float(result.phase_margin_deg),
                "GainMargin": float(result.gain_margin_linear),
                "PMFrequency": float(result.crossover_hz),
                "GMFrequency": float(result.phase_crossover_hz),
            },
            "freq_hz": result.freq_hz,
            "magnitude": result.magnitude,
            "phase_deg": result.phase_deg,
        },
        do_compression=True,
    )
    return target
