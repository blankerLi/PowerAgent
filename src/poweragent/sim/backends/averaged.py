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

为什么必须限制 max_step
-----------------------
`solve_ivp` 的初始步长是按积分区间长度启发式选的，而 `t_eval` 只控制输出采样点、
不约束内部步长——落在两个内部步之间的输出点由稠密插值给出。

后果实测过：负载阶跃的下冲峰值出现在阶跃后几微秒内，而最后一段的区间长度随
`stop_time` 变化（400 µs 时约 359 µs，1 ms 时约 899 µs）。区间越长、初始步长越大，
下冲峰值就越容易被长距插值抹平——同一候选同一工况下，下冲从 34.6 mV 变成
30.96 mV，**结果依赖仿真总时长**。这直接违反"同一候选同一场景结果唯一"，而缓存
命中判等、复现性回归门禁、双模型一致性核对全都建立在那个前提上。

因此把步长上限锁到输出网格间距：每个输出点都由真实计算的步给出，而不是插值。
代价是最长那一段的步数由区间长度除以 0.2 µs 决定（约数千步），换来的是结果与
`stop_time` 无关。
"""

from __future__ import annotations

import numpy as np
from scipy.integrate import solve_ivp

from poweragent.sim.backends.buck_model import (
    STEP_TRIGGER_FRACTION,
    BuckSpec,
    OperatingCondition,
    SimStatus,
    TimeDomainRun,
    current_command,
    ramp_duration_s,
    steady_state,
)

__all__ = ["simulate_averaged"]


def _output_grid(stop_time_s: float, output_dt_s: float) -> np.ndarray:
    """均匀输出时间网格，含终点。

    输出分辨率与求解器内部步长解耦：求解器自适应步进以控制误差，输出按固定
    网格采样以保证指标计算有确定的时间分辨率（`settling_time` 的比较容差是
    0.5 µs，网格必须比它细）。
    """
    n = int(round(stop_time_s / output_dt_s)) + 1
    return np.linspace(0.0, stop_time_s, n)


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
    t_delay = spec.modulator_delay_s

    t_step = STEP_TRIGGER_FRACTION * stop_time_s
    t_ramp = ramp_duration_s(condition)

    def vout_of(t: float, y: np.ndarray) -> float:
        il, vc = y[0], y[1]
        return float(vc + esr * (il - condition.load_current_a(t, t_step)))

    def rhs(t: float, y: np.ndarray) -> np.ndarray:
        il, vc, xc = y
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

        # 电流内环逐周期精确跟踪指令，唯一动态是采样保持延迟 Td。
        d_il = (il_cmd - il) / t_delay
        d_vc = (il - iload) / cout
        return np.array([d_il, d_vc, d_xc])

    def event_vout_diverged(t: float, y: np.ndarray) -> float:
        return guard_vout_abs_max - abs(vout_of(t, y))

    def event_iphase_diverged(t: float, y: np.ndarray) -> float:
        return guard_iphase_abs_max - abs(float(y[0]) / n_phase)

    event_vout_diverged.terminal = True  # type: ignore[attr-defined]
    event_vout_diverged.direction = -1.0  # type: ignore[attr-defined]
    event_iphase_diverged.terminal = True  # type: ignore[attr-defined]
    event_iphase_diverged.direction = -1.0  # type: ignore[attr-defined]

    grid = _output_grid(stop_time_s, output_dt_s)
    # steady_state() 返回四个分量，第四个是给开关模型分相用的电流初值副本；
    # 平均模型的状态向量只有 (iL, vc, xc) 三项。
    y_steady = np.asarray(steady_state(spec, condition)[:3], dtype=float)

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
    atol = np.array(
        [rel_tol * max(spec.iout_nom_a, 1.0), rel_tol * vref, rel_tol * vref]
    )

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
            # 步长上限锁到输出网格间距，见下方"为什么必须限制 max_step"。
            max_step=output_dt_s,
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
