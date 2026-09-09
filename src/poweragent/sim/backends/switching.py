"""开关模型时域仿真：显式 PWM 与相位交错。

与平均模型描述同一个电路，区别只在是否把开关动作平均掉。代价与收益都明确：
步长受开关频率约束，比平均模型慢 1~2 个数量级；换来真实的输出纹波、相电流
三角波，以及交错后的纹波抵消效应——这些在平均模型里根本不存在。

峰值电流模式的开关级实现
------------------------
每相独立的 PWM，载波相位错开 360/N 度：

1. 相 k 的开关周期起点：开通上管，并**采样保持**当时的电流指令
2. 该相电感电流达到保持的指令值时关断上管，下管同步整流
3. 保持关断至下一个周期起点

采样延迟在这里是天然的：指令每周期只被采样一次，无需像平均模型那样用一阶滞后
去近似。两者的残余偏差正是双模型一致性核对要检验的对象。

电流检测按匹配网络处理，不加低通
--------------------------------
比较用的是瞬时电感电流。DCR 检测的 RC 网络是**匹配网络**：时间常数配成 L/DCR
后它无滞后地重建电流波形（含直流与纹波），而不是一个低通滤波器。

试过给它加一个 fsw/5 量级的低通极点，结果是错的：本工况占空比仅 7.4%，导通时间
0.148 µs 远短于该滤波器的 1.59 µs 时间常数，检测值在整个导通期内几乎不变，逐周期
峰值比较彻底失效——相电流冲到 72~109 A（负载均流只有 37.5 A）。峰值电流模式要求
检测通路的带宽远高于开关频率，这是该架构的前提而非可调项。

限制外环带宽的是采样保持效应，不是检测滤波：指令每有效开关周期才更新一次，
其等效极点在 N*fsw/pi 附近（Ridley 采样保持模型）。这一效应在开关模型里由逐周期
比较天然产生，在平均模型里由 Td = 0.5/(N*fsw) 的一阶滞后表达，两者是同一件事的
两种写法。

状态量为 N 个相电流 + 输出电容电压 + 补偿积分状态，共 N+2 个。

固定步长而非自适应
------------------
开关动作使右端项每个周期内多次不连续，自适应步长求解器会在每个开关点反复缩步
重试，比固定小步长更慢也更不可靠。这里用固定步长显式欧拉，步长取
`model.yaml` 的 `solver.max_step`（20 ns，即每开关周期约 100 步）。

显式欧拉在这里足够：一个开关状态内电感两端电压近似恒定，电流轨迹近似直线，
截断误差随步长线性下降且不累积成偏差；输出电容电压的变化尺度比开关周期慢三个
数量级。用高阶方法只会增加每步成本，不改善纹波幅值的精度。

数值精度与已知偏差
------------------
显式欧拉在 20 ns 步长（`model.yaml` 的 `solver.max_step`，约每开关周期 100 步）下
**未完全收敛**：负载阶跃下冲为 33.52 mV，细化到 10 ns 得 32.83 mV、5 ns 得 32.51 mV。
即 20 ns 相对收敛值高估下冲约 3%。方向是保守的（高估下冲使约束判定更严），且把步长
细化 4 倍换 3% 精度并不划算，因此保留 20 ns 并在此记录该偏差，也在
`model.yaml` 的 `dual_model_consistency.transient_tol` 里为它留了余量。

另有初始瞬态：初始开关状态取"全部关断、指令为稳态均流值"，不是精确的稳态相位与
纹波，需要若干开关周期衰减。因此下冲结果对阶跃触发时刻之前的建立时间有依赖——
`stop_time` 为 200 µs（触发前 10 个开关周期）时下冲读到 38.65 mV，600 µs（30 周期）
时 33.68 mV，1 ms（50 周期）时 33.52 mV。配置取 1 ms，残余影响低于 0.5%。

有意的简化
----------
死区（`dead_time_s`）不建模。死区期间电流走体二极管，影响的是导通损耗与效率，
对电流波形形状与输出纹波幅值的影响在本项目关心的量级上可以忽略。本项目不评估
效率指标（`efficiency` 未激活），因此这个简化不影响任何被判定的量。
"""

from __future__ import annotations

import numpy as np

from poweragent.sim.backends.buck_model import (
    STEP_TRIGGER_FRACTION,
    BuckSpec,
    OperatingCondition,
    SimStatus,
    TimeDomainRun,
    current_command,
    steady_state,
)

__all__ = ["simulate_switching"]


def simulate_switching(
    spec: BuckSpec,
    condition: OperatingCondition,
    *,
    rcomp_ohm: float,
    ccomp_f: float,
    stop_time_s: float,
    time_step_s: float,
    guard_vout_abs_max: float,
    guard_iphase_abs_max: float,
    output_dt_s: float = 0.2e-6,
) -> TimeDomainRun:
    """逐步推进开关级仿真，返回与平均模型同形的结果。

    `time_step_s` 是固定积分步长（取自 `model.yaml` 的 `solver.max_step`）；
    `output_dt_s` 是输出下采样间隔，两者解耦：积分要细到能分辨开关动作，输出只
    需细到能满足指标的时间分辨率，全量保留会让产物大出一个数量级而无收益。
    """
    n_phase = spec.n_phase
    tsw = spec.switching_period_s()
    dt = time_step_s

    l_h = spec.l_per_phase_h
    cout = spec.cout_f
    esr = spec.cout_esr_ohm
    r_loop = spec.conduction_resistance_ohm(condition.temp_c)
    ri = spec.sense_gain_ohm(condition.temp_c)
    gm = spec.gm_s
    vref = spec.vout_nom_v
    vin = condition.vin_v

    t_step = STEP_TRIGGER_FRACTION * stop_time_s

    # 每相载波的相位偏移（归一化到一个开关周期）与导通时间钳位。
    phase_offset = np.arange(n_phase, dtype=float) / n_phase
    min_on_time = spec.duty_min * tsw
    max_on_time = spec.duty_max * tsw

    il_ss, vc_ss, xc_ss, _ = steady_state(spec, condition)
    i_ph = np.full(n_phase, il_ss / n_phase)
    vc = float(vc_ss)
    xc = float(xc_ss)

    # 初始全部关断、指令保持为稳态均流值。第一个周期起点即开通；由此产生的
    # 初始瞬态在阶跃触发前（约 50 个开关周期）早已衰减。
    switch_on = np.zeros(n_phase, dtype=bool)
    i_cmd_held = np.full(n_phase, il_ss / n_phase)
    on_since = np.zeros(n_phase)

    n_steps = int(round(stop_time_s / dt))
    record_every = max(1, int(round(output_dt_s / dt)))

    times: list[float] = []
    vout_samples: list[float] = []
    iout_samples: list[float] = []
    iphase_samples: list[np.ndarray] = []

    status: SimStatus = "ok"
    message = ""
    frac_prev = np.mod(-phase_offset, 1.0)

    for step in range(n_steps + 1):
        t = step * dt
        iload = condition.load_current_a(t, t_step)
        i_total = float(i_ph.sum())
        vout = vc + esr * (i_total - iload)

        if step % record_every == 0:
            times.append(t)
            vout_samples.append(vout)
            iout_samples.append(iload)
            iphase_samples.append(i_ph.copy())

        if abs(vout) > guard_vout_abs_max or np.abs(i_ph).max() > guard_iphase_abs_max:
            status = "diverged"
            message = "state exceeded divergence_guard"
            break
        if not np.isfinite(vout) or not np.all(np.isfinite(i_ph)):
            status = "solver_error"
            message = "non-finite state"
            break
        if step == n_steps:
            break

        # ---- 电压外环（连续）：补偿器与电流指令 ----
        verr = vref - vout
        vcomp = xc + rcomp_ohm * gm * verr
        il_cmd, limited_high, limited_low = current_command(vcomp, spec, ri)
        if (limited_high and verr > 0.0) or (limited_low and verr < 0.0):
            d_xc = 0.0  # 抗积分饱和
        else:
            d_xc = gm * verr / ccomp_f

        # ---- PWM：周期起点开通并采样保持指令，达到指令值即关断 ----
        frac = np.mod(t / tsw - phase_offset, 1.0)
        new_cycle = frac < frac_prev  # 归一化相位回绕即新周期起点
        frac_prev = frac

        if new_cycle.any():
            switch_on = switch_on | new_cycle
            i_cmd_held = np.where(new_cycle, il_cmd / n_phase, i_cmd_held)
            on_since = np.where(new_cycle, t, on_since)

        # 比较用瞬时电感电流：检测网络是匹配网络而非低通（见模块 docstring）。
        on_time = t - on_since
        reached_command = (i_ph >= i_cmd_held) & (on_time >= min_on_time)
        hit_duty_limit = on_time >= max_on_time
        switch_on = switch_on & ~(reached_command | hit_duty_limit)

        # ---- 功率级：按开关状态给出电感两端电压 ----
        v_switch = np.where(switch_on, vin, 0.0)
        v_inductor = v_switch - vout - i_ph * r_loop

        i_ph = i_ph + v_inductor * dt / l_h
        vc = vc + (i_total - iload) * dt / cout
        xc = xc + d_xc * dt

    time_s = np.asarray(times)
    return TimeDomainRun(
        time_s=time_s,
        vout_v=np.asarray(vout_samples),
        iout_a=np.asarray(iout_samples),
        iphase_a=np.asarray(iphase_samples),
        step_trigger_s=t_step,
        status=status,
        solver_message=message,
    )
