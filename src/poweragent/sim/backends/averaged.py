"""平均模型时域仿真：负载阶跃响应。

按开关周期平均，不显式建模 PWM，因此不含开关纹波。这是筛选层用的低成本保真度，
也是频域裕量提取（`margin.py`）的对象。电路方程与控制架构见
`buck_model.py` 的模块 docstring。

分段积分
--------
负载电流波形有两处导数不连续：阶跃触发时刻、以及电流斜升到位的时刻。自适应
步长求解器跨过不连续点时会产生虚假的数值振荡或强行缩步，因此按不连续点把时间
轴切成三段分别积分，每段内部 `iload(t)` 都是光滑的：

    [0, t_step)                 阶跃前，恒定负载 —— 解析稳态，不积分
    [t_step, t_step + t_ramp)   电流斜升
    [t_step + t_ramp, t_end]    到位后恒定负载

第一段不积分：`steady_state()` 给出的就是这一段的精确解（补偿器含积分项，
稳态无静差），跑数值积分只会引入误差并浪费时间。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.integrate import solve_ivp

from poweragent.sim.backends.buck_model import (
    BuckSpec,
    OperatingCondition,
    current_command,
    steady_state,
)

__all__ = ["TimeDomainRun", "simulate_averaged", "STEP_TRIGGER_FRACTION"]

# 阶跃触发时刻占总仿真时长的比例。取 10%：前面留出足以让 `obs.vout_min/max`
# 看到一段正常电压，后面留出 90% 供瞬态恢复与稳态重建。
STEP_TRIGGER_FRACTION = 0.10

SimStatus = Literal["ok", "diverged", "solver_error"]


@dataclass(frozen=True, slots=True)
class TimeDomainRun:
    """一次时域仿真的输出，与具体后端无关。

    `iphase_a` 形状为 `(n_time, n_phase)`——`eval/metrics.py` 的
    `phase_peak_current` 按这个二维形状同时做跨相与跨时间的聚合。平均模型下
    各相均流，每一列相同；开关模型下各列因相位交错而不同。
    """

    time_s: np.ndarray
    vout_v: np.ndarray
    iout_a: np.ndarray
    iphase_a: np.ndarray
    step_trigger_s: float
    status: SimStatus
    solver_message: str = ""

    @property
    def n_samples(self) -> int:
        return int(self.time_s.size)


def _output_grid(stop_time_s: float, output_dt_s: float) -> np.ndarray:
    """均匀输出时间网格，含终点。

    输出分辨率与求解器内部步长解耦：求解器自适应步进以控制误差，输出按固定
    网格采样以保证指标计算有确定的时间分辨率（`settling_time` 的比较容差是
    0.5 µs，网格必须比它细）。
    """
    n = int(round(stop_time_s / output_dt_s)) + 1
    return np.linspace(0.0, stop_time_s, n)


def _ramp_duration_s(condition: OperatingCondition) -> float:
    """负载电流从起点变到终点所需时间；`slew` 为零或无阶跃时返回 0。"""
    delta = abs(condition.load_end_a - condition.load_start_a)
    if delta == 0.0 or condition.slew_a_per_us <= 0.0:
        return 0.0
    return delta / (condition.slew_a_per_us * 1e6)


def simulate_averaged(
    spec: BuckSpec,
    condition: OperatingCondition,
    *,
    rcomp_ohm: float,
    ccomp_f: float,
    stop_time_s: float,
    rel_tol: float,
    guard_vout_abs_max: float,
    guard_iphase_abs_max: float,
    output_dt_s: float = 0.2e-6,
) -> TimeDomainRun:
    """求解一次负载阶跃响应。

    `guard_*` 是发散判据的安全界（来自 `model.yaml` 的 `divergence_guard`），
    与评价阈值分离：越界表示数值解跑飞了，直接终止并报 `diverged`，而不是让
    求解器继续在无意义的数值上耗时间。用 `solve_ivp` 的终止事件实现，因此
    发散的仿真是提前退出而非跑完全程。
    """
    vref = spec.vout_nom_v
    n_phase = spec.n_phase
    cout = spec.cout_f
    esr = spec.cout_esr_ohm
    gm = spec.gm_s
    ri = spec.sense_gain_ohm(condition.temp_c)
    omega_ci = 2.0 * np.pi * spec.current_loop_bw_hz(condition.vin_v)
    t_delay = spec.modulator_delay_s

    t_step = STEP_TRIGGER_FRACTION * stop_time_s
    t_ramp = _ramp_duration_s(condition)

    def vout_of(t: float, y: np.ndarray) -> float:
        il, vc = y[0], y[1]
        return float(vc + esr * (il - condition.load_current_a(t, t_step)))

    def rhs(t: float, y: np.ndarray) -> np.ndarray:
        il, vc, xc, il_cmd_delayed = y
        iload = condition.load_current_a(t, t_step)
        vout = vc + esr * (il - iload)
        verr = vref - vout
        vcomp = xc + rcomp_ohm * gm * verr
        il_cmd, limited_high, limited_low = current_command(vcomp, spec, ri)

        # 抗积分饱和：指令已触限且误差仍在往该方向推时冻结积分。
        if (limited_high and verr > 0.0) or (limited_low and verr < 0.0):
            d_xc = 0.0
        else:
            d_xc = gm * verr / ccomp_f

        # 电流内环跟踪的是经 PWM 采样延迟后的指令，不是补偿器当前输出。
        d_il = (il_cmd_delayed - il) * omega_ci
        d_vc = (il - iload) / cout
        d_cmd_delayed = (il_cmd - il_cmd_delayed) / t_delay
        return np.array([d_il, d_vc, d_xc, d_cmd_delayed])

    def event_vout_diverged(t: float, y: np.ndarray) -> float:
        return guard_vout_abs_max - abs(vout_of(t, y))

    def event_iphase_diverged(t: float, y: np.ndarray) -> float:
        return guard_iphase_abs_max - abs(float(y[0]) / n_phase)

    event_vout_diverged.terminal = True  # type: ignore[attr-defined]
    event_vout_diverged.direction = -1.0  # type: ignore[attr-defined]
    event_iphase_diverged.terminal = True  # type: ignore[attr-defined]
    event_iphase_diverged.direction = -1.0  # type: ignore[attr-defined]

    grid = _output_grid(stop_time_s, output_dt_s)
    y_steady = np.asarray(steady_state(spec, condition), dtype=float)

    # ---- 第一段：阶跃前，解析稳态，不积分 ----
    pre_mask = grid < t_step
    times: list[np.ndarray] = [grid[pre_mask]]
    states: list[np.ndarray] = [np.tile(y_steady[:, None], (1, int(pre_mask.sum())))]

    # ---- 第二、三段：斜升与到位后，分别积分 ----
    boundaries = [t_step, t_step + t_ramp, stop_time_s]
    segments = [(a, b) for a, b in zip(boundaries, boundaries[1:]) if b > a]

    y0 = y_steady.copy()
    # 各状态量级差异达 3 个数量级（电流 ~1e2 A、电压 ~1e0 V、积分状态 ~1e-1 V），
    # 统一的绝对容差会让某个状态被过度或不足约束，故按量级分别给出。
    i_scale = rel_tol * max(spec.iout_nom_a, 1.0)
    atol = np.array([i_scale, rel_tol * vref, rel_tol * vref, i_scale])

    status: SimStatus = "ok"
    message = ""

    for a, b in segments:
        seg_mask = (grid > a) & (grid <= b) if a > 0.0 else (grid >= a) & (grid <= b)
        t_eval = grid[seg_mask]

        sol = solve_ivp(
            rhs,
            (a, b),
            y0,
            method="BDF",
            t_eval=t_eval if t_eval.size else None,
            rtol=rel_tol,
            atol=atol,
            events=(event_vout_diverged, event_iphase_diverged),
        )

        if sol.t.size:
            times.append(sol.t)
            states.append(sol.y)

        if not sol.success:
            status, message = "solver_error", str(sol.message)
            break
        if any(ev.size > 0 for ev in sol.t_events):
            status, message = "diverged", "state exceeded divergence_guard"
            break

        if sol.y.shape[1]:
            y0 = sol.y[:, -1]

    time_s = np.concatenate(times)
    state = np.concatenate(states, axis=1)
    il = state[0]
    vc = state[1]

    iout = np.array([condition.load_current_a(float(t), t_step) for t in time_s])
    vout = vc + esr * (il - iout)
    # 平均模型下各相均流：把总电流均分到 N 列，形状与开关模型一致。
    iphase = np.repeat((il / n_phase)[:, None], n_phase, axis=1)

    return TimeDomainRun(
        time_s=time_s,
        vout_v=vout,
        iout_a=iout,
        iphase_a=iphase,
        step_trigger_s=t_step,
        status=status,
        solver_message=message,
    )
