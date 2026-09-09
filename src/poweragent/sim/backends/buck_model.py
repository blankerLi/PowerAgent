"""多相 Buck 的电路模型：参数装载、温度/输入电压修正与稳态工作点。

平均模型与开关模型共用本模块，两者只在「是否显式建模 PWM」上不同。

为什么是电流模式
----------------
控制架构为电流模式：电流内环控制电感电流，电压外环由 Type-II 补偿器整定。

这不是任意选择。低压大电流功率级的 LC 网络阻尼极小（本规格下 Q ≈ 4），电压模式
下这对双极点在穿越频率处贡献 -180° 相位，而 Type-II 补偿器只有一个零点、最多补回
+90°，无论怎么整定都拿不到 45° 相位裕量。想靠输出电容 ESR 零点补相位，就得把 ESR
加到 mΩ 量级，而 ESR·ΔI 的瞬时压降会直接吃光全部电压容限——稳定性与瞬态性能在
电压模式下无法同时满足。电流内环把电感极点消掉、功率级降为一阶，Type-II 才够用。
工业界的 AI 芯片供电模块同样如此。

状态与方程
----------
平均模型取三个状态量（开关模型在此基础上按相展开电感电流并显式建模 PWM）：

    iL   总电感电流（A），N 相之和
    vc   输出电容电压（V），不含 ESR 上的压降
    xc   补偿电容 Ccomp 上的电压（V），即积分状态

输出电压把电容 ESR 上的压降算进去——这一项是负载阶跃瞬间下冲的组成部分，
忽略它会低估下冲：

    vout = vc + ESR * (iL - iload)

电压外环（Type-II 跨导补偿器：gm 误差放大器 + 串联 Rcomp/Ccomp 接 COMP 脚）。
误差电流 gm*verr 全部流进补偿网络，Ccomp 上积分、Rcomp 上产生瞬时压降：

    verr  = vref - vout
    Ccomp * d(xc)/dt = gm * verr
    vcomp = xc + Rcomp * gm * verr

补偿器输出即电流指令（除以电流检测增益换算为安培），并受限幅约束：

    iL_cmd = clip(vcomp / Ri, 0, i_limit)

电流内环用单个一阶滞后概括，时间常数就是采样延迟：

    Td * d(iL)/dt = iL_cmd - iL,   Td = 0.5 / (N * fsw)

**这一项不能省，也不该被拆成两项。** 峰值电流模式的电流内环逐周期精确跟踪指令，
唯一实质性的动态是采样保持：指令每有效开关周期才更新一次。N 相载波错开 360/N 度、
各相轮流采样，有效采样率是单相开关频率的 N 倍，故 Td = 0.5/(N*fsw)。其等效带宽
1/(2*pi*Td) = N*fsw/pi，与 Ridley 采样保持模型给出的极点位置一致。

它是唯一惩罚「穿越频率逼近开关频率」的物理机制，在 fc 处贡献 atan(2*pi*fc*Td) 的
相位滞后。去掉它，输出电容 ESR 零点会在高频提供大量相位提升，使模型得出「补偿器
增益越大越好」的结论，相位裕量约束形同虚设，寻优退化成「Rcomp 取域上限」。

早先的版本在这个滞后之外还串了一个独立的「电流内环带宽」极点（取 fsw/5），那是
错的：两者表达的是同一个采样效应，串两遍等于把它算了两次，而且 fsw/5 这个量级
比真实极点低了一个数量级，使平均模型系统性偏悲观——负载阶跃下冲与开关模型相差
最多 20%。低保真模型偏悲观并不"安全"：筛选层会据此误杀实际可行的候选，而被误杀
的候选永远不会进入评价层得到纠正。

选一阶滞后而非 Pade 全通近似，是因为后者在时域对阶跃输入会产生非物理的反向
预冲；一阶滞后的相位在 fc < 1/(2*pi*Td) 范围内与真延迟接近，且幅值多一点衰减
的方向是保守的（更早判失稳），不会把不稳定的设计判成稳定。

输出电容：

    C * d(vc)/dt = iL - iload

由此外环开环传递函数为「补偿器 × 1/Ri × 采样滞后 × 1/(sC)」，穿越频率

    fc ≈ gm * Rcomp / (Ri * 2*pi*Cout)

与 Rcomp 成正比；补偿零点 fz = 1/(2*pi*Rcomp*Ccomp)。两个设计变量因此各有明确
作用：Rcomp 定带宽（决定下冲深浅），Ccomp 定零点位置（决定相位裕量）。

抗积分饱和
----------
电流指令限幅是真实的非线性。若限幅期间仍继续积分，误差消失后积分状态早已远超
所需值，环路要花很长时间"吐"回来，表现为剧烈过冲与振荡，即积分饱和（windup）。
本模型用条件积分：限幅已触发、且误差方向仍在把指令往限幅方向推时，冻结积分。

不加这一项的后果实测过：负载阶跃后电感电流过冲到负载电流的数倍，整个搜索空间的
候选全部触发发散判据，模型完全不可用。

工况的物理作用
--------------
`vin_v` 与 `temp_c` 不是装饰性字段：

- **温度**：主要因素。电流检测采用电感 DCR 检测，铜电阻随温度上升（0.393%/K 是
  物理常数），检测增益 Ri 变大、外环穿越频率下降，高温工况的瞬态更差。同时
  MOSFET 导通电阻也上升（取 0.5%/K 的典型量级），加大导通损耗与压降。
- **输入电压**：通过两条路径起作用，都不改变环路增益。一是决定占空比、进而决定
  开关模型的相电流纹波幅值；二是决定电感电流的最大可达变化率
  `N*(Vin - Vout)/L`，评价工况下约 267 A/µs，相对 200 A/µs 的负载斜率只有 1.3 倍
  余量。所以低输入电压确实恶化瞬态，但机理与温度不同：温度压低环路增益，
  输入电压压缩大信号追赶余量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

import numpy as np
import yaml

__all__ = [
    "BuckSpec",
    "load_buck_spec",
    "OperatingCondition",
    "steady_state",
    "current_command",
    "TimeDomainRun",
    "SimStatus",
    "STEP_TRIGGER_FRACTION",
    "ramp_duration_s",
]

# 阶跃触发时刻占总仿真时长的比例。取 10%：前面留出足以让 `obs.vout_min/max`
# 看到一段正常电压，后面留出 90% 供瞬态恢复与稳态重建。两个求解器共用同一比例，
# 否则平均模型与开关模型的波形在时间轴上对不齐，双模型一致性核对无从进行。
STEP_TRIGGER_FRACTION = 0.10

SimStatus = Literal["ok", "diverged", "solver_error"]


@dataclass(frozen=True, slots=True)
class TimeDomainRun:
    """一次时域仿真的输出，两个求解器共用这一种形状。

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

# 铜的电阻温度系数（1/K）。电感 DCR 是铜绕组，DCR 检测的增益随它变化。
_COPPER_TEMPCO_PER_K = 0.00393
# MOSFET 导通电阻的温度系数（1/K）典型量级：125 °C 时约为 25 °C 的 1.5 倍。
_MOSFET_TEMPCO_PER_K = 0.005
# 温度系数的参考温度（°C），器件参数标称值所在的温度。
_TEMPCO_REFERENCE_C = 25.0


@dataclass(frozen=True, slots=True)
class BuckSpec:
    """`models/buck4ph_*.yaml` 装载后的电路参数。

    字段与 YAML 逐项对应，不做单位换算：YAML 里已经全部是 SI 单位。
    `variant` 只用于自检（防止把平均模型文件喂给开关模型求解器），不参与计算。
    """

    variant: str

    # 功率级
    n_phase: int
    fsw_hz: float
    l_per_phase_h: float
    cout_f: float
    cout_esr_ohm: float
    dcr_per_phase_ohm: float
    rds_on_ohm: float

    # 工作点
    vin_nom_v: float
    vout_nom_v: float
    iout_nom_a: float

    # 控制
    gm_s: float
    ri_nom_ohm: float
    i_limit_a: float

    # 调制器（开关模型使用；平均模型只用占空比钳位做限幅自检）
    duty_min: float
    duty_max: float
    phase_shift_deg: float
    dead_time_s: float

    @property
    def l_equivalent_h(self) -> float:
        """N 相并联的等效电感 L/N。"""
        return self.l_per_phase_h / self.n_phase

    @property
    def esr_zero_hz(self) -> float:
        """输出电容 ESR 零点 1/(2*pi*Cout*ESR)。"""
        return 1.0 / (2.0 * math.pi * self.cout_f * self.cout_esr_ohm)

    def switching_period_s(self) -> float:
        return 1.0 / self.fsw_hz

    @property
    def modulator_delay_s(self) -> float:
        """PWM 零阶保持的等效平均延迟 = 半个**有效**开关周期。

        分母是 `n_phase * fsw` 而不是 `fsw`：N 相载波彼此错开 360/N 度，各相轮流
        采样并更新电流指令，因此环路看到的有效采样率是单相开关频率的 N 倍，
        等效延迟相应缩短为 `0.5 / (N * fsw)`。

        这一点是拿开关模型当真值校准出来的，不是推导出来就算了。用 `0.5/fsw`
        时平均模型与开关模型的负载阶跃下冲相差最多 20%，平均模型系统性偏悲观；
        改为有效开关周期后偏差回到个位数百分比。低保真模型偏悲观并非"安全"——
        筛选层会据此误杀实际可行的候选，而被误杀的候选永远不会进入评价层得到
        纠正，分层执行反而成了漏斗上的破洞。

        不做成配置项：它由相数与开关频率唯一决定，是 PWM 采样机制的后果而非可调
        设计参数。允许它被单独配置只会引入「与 fsw/n_phase 不一致」这种新的
        出错方式。
        """
        return 0.5 / (self.n_phase * self.fsw_hz)

    def sense_gain_ohm(self, temp_c: float) -> float:
        """电流检测增益，含温度修正。

        DCR 检测：检测元件就是电感的铜绕组，因此增益随铜的电阻温度系数变化。
        增益变大意味着同样的电感电流被"看得更大"，外环等效增益下降。
        """
        return self.ri_nom_ohm * (
            1.0 + _COPPER_TEMPCO_PER_K * (temp_c - _TEMPCO_REFERENCE_C)
        )

    def conduction_resistance_ohm(self, temp_c: float) -> float:
        """每相回路的等效串联电阻（DCR + 导通电阻），含温度修正。

        两者分别用各自的温度系数修正后相加——用同一个系数修正两者会低估高温下的
        阻抗增长。该值进入稳态占空比与导通压降，不进入电压外环的动态。
        """
        delta = temp_c - _TEMPCO_REFERENCE_C
        dcr = self.dcr_per_phase_ohm * (1.0 + _COPPER_TEMPCO_PER_K * delta)
        rds = self.rds_on_ohm * (1.0 + _MOSFET_TEMPCO_PER_K * delta)
        return dcr + rds

    def max_current_slew_a_per_s(self, vin_v: float) -> float:
        """全部相同时导通时电感电流的最大可达变化率 N*(Vin - Vout)/L。

        这是大信号限制而非小信号带宽：负载阶跃斜率超过它时电流物理上追不上，
        下冲转由输出电容放电主导。

        本规格下的实际余量不大：标称 12 V 时约 299 A/µs，评价工况 10.8 V 时约
        267 A/µs，而评价场景的负载斜率是 200 A/µs——只快 1.3 倍。因此输入电压
        对瞬态**有**实际影响，只是不像温度那样通过改变环路增益起作用，而是通过
        压缩这个大信号余量起作用。
        """
        return self.n_phase * max(vin_v - self.vout_nom_v, 0.0) / self.l_per_phase_h

    def phase_current_ripple_a(self, vin_v: float) -> float:
        """稳态相电流纹波峰峰值 (Vin - Vout)·D·Tsw/L，D = Vout/Vin。

        只在开关模型里真实出现（平均模型按定义不含纹波），这里给出解析值供测试
        与文档核对。它是相电流峰值高于均流值的主要原因。
        """
        duty = self.vout_nom_v / vin_v
        return (vin_v - self.vout_nom_v) * duty * self.switching_period_s() / self.l_per_phase_h

    def crossover_hz(self, *, rcomp_ohm: float, temp_c: float) -> float:
        """电压外环穿越频率的解析估计 fc ≈ gm*Rcomp/(Ri*2*pi*Cout)。

        用于诊断与档位标定，不参与时域求解——时域解自然包含这个带宽，不需要
        把它作为输入。频域裕量提取（`margin.py`）用完整传递函数而非本估计。
        """
        ri = self.sense_gain_ohm(temp_c)
        return self.gm_s * rcomp_ohm / (ri * 2.0 * math.pi * self.cout_f)


def _require(section: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in section:
        raise KeyError(f"模型文件缺少 {where}.{key}")
    return section[key]


def load_buck_spec(path: str | Path) -> BuckSpec:
    """装载 `models/buck4ph_averaged.yaml` 或 `..._switching.yaml`。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    stage = _require(raw, "power_stage", "<root>")
    point = _require(raw, "operating_point", "<root>")
    control = _require(raw, "control", "<root>")
    modulator = _require(raw, "modulator", "<root>")

    n_phase = int(_require(stage, "n_phase", "power_stage"))

    return BuckSpec(
        variant=str(_require(raw, "model_variant", "<root>")),
        n_phase=n_phase,
        fsw_hz=float(_require(stage, "fsw_hz", "power_stage")),
        l_per_phase_h=float(_require(stage, "l_per_phase_h", "power_stage")),
        cout_f=float(_require(stage, "cout_f", "power_stage")),
        cout_esr_ohm=float(_require(stage, "cout_esr_ohm", "power_stage")),
        dcr_per_phase_ohm=float(_require(stage, "dcr_per_phase_ohm", "power_stage")),
        rds_on_ohm=float(_require(stage, "rds_on_ohm", "power_stage")),
        vin_nom_v=float(_require(point, "vin_nom_v", "operating_point")),
        vout_nom_v=float(_require(point, "vout_nom_v", "operating_point")),
        iout_nom_a=float(_require(point, "iout_nom_a", "operating_point")),
        gm_s=float(_require(control, "gm_s", "control")),
        ri_nom_ohm=float(_require(control, "ri_ohm", "control")),
        i_limit_a=float(_require(control, "i_limit_a", "control")),
        duty_min=float(_require(modulator, "duty_min", "modulator")),
        duty_max=float(_require(modulator, "duty_max", "modulator")),
        phase_shift_deg=float(_require(modulator, "phase_shift_deg", "modulator")),
        dead_time_s=float(_require(modulator, "dead_time_s", "modulator")),
    )


@dataclass(frozen=True, slots=True)
class OperatingCondition:
    """一次仿真的工况：由 `task.yaml` 的场景行给出。"""

    vin_v: float
    temp_c: float
    load_start_a: float
    load_end_a: float
    slew_a_per_us: float

    def load_current_a(self, t: float, step_trigger_s: float) -> float:
        """负载电流：阶跃前恒为 `load_start_a`，触发后按 `slew_a_per_us` 线性
        变化到 `load_end_a` 后保持。

        用有限斜率而不是理想台阶：真实负载的电流变化率有限，而下冲深度对这个
        斜率相当敏感，理想台阶会给出过于悲观的结果。
        """
        if t < step_trigger_s:
            return self.load_start_a

        delta = self.load_end_a - self.load_start_a
        if delta == 0.0:
            return self.load_start_a

        ramp = self.slew_a_per_us * 1e6 * (t - step_trigger_s)
        if delta > 0.0:
            return min(self.load_start_a + ramp, self.load_end_a)
        return max(self.load_start_a - ramp, self.load_end_a)


def ramp_duration_s(condition: OperatingCondition) -> float:
    """负载电流从起点变到终点所需时间；`slew` 为零或无阶跃时返回 0。"""
    delta = abs(condition.load_end_a - condition.load_start_a)
    if delta == 0.0 or condition.slew_a_per_us <= 0.0:
        return 0.0
    return delta / (condition.slew_a_per_us * 1e6)


def current_command(vcomp: float, spec: BuckSpec, ri_ohm: float) -> tuple[float, bool, bool]:
    """把补偿器输出电压换算为总电流指令，并施加限幅。

    返回 `(iL_cmd, limited_high, limited_low)`。两个限幅标志供调用方实现抗积分
    饱和——判断"是否触限"必须在钳位之前做，钳位后的值已经丢失这个信息，所以由
    本函数一次算出并返回，而不是让每个调用点各自重算。
    """
    raw = vcomp / ri_ohm
    if raw > spec.i_limit_a:
        return spec.i_limit_a, True, False
    if raw < 0.0:
        return 0.0, False, True
    return raw, False, False


def steady_state(
    spec: BuckSpec, condition: OperatingCondition
) -> tuple[float, float, float, float]:
    """返回阶跃前的稳态初值 `(iL, vc, xc, iL_cmd_delayed)`。

    解析可得，不需要先跑一段仿真去"稳定下来"：

    - 补偿器含积分项 ⟹ 稳态误差为零 ⟹ `vout = vout_nom`
    - 稳态下电容电流为零 ⟹ `iL = iload` ⟹ ESR 上无压降 ⟹ `vc = vout_nom`
    - 电流内环稳态无差 ⟹ `iL_cmd = iL` ⟹ `vcomp = iL * Ri`
    - `verr = 0` ⟹ Rcomp 上无压降 ⟹ `xc = vcomp`

    返回的第四个分量与 `iL` 相等，供需要一个独立电流状态初值的求解器使用
    （开关模型的相电流均分即由它得出）。

    最后一条也说明稳态解与两个设计变量无关：搜索改变的是阶跃响应的形状，不是
    阶跃前的静态工作点。

    直接给解析初值有两个好处：仿真时间全部用在关心的阶跃响应上，不浪费在建立
    稳态；以及每次仿真的初始条件严格相同，结果可复现。
    """
    il_ss = condition.load_start_a
    vc_ss = spec.vout_nom_v  # 稳态电容电流为零，ESR 无压降
    xc_ss = il_ss * spec.sense_gain_ohm(condition.temp_c)

    return il_ss, vc_ss, xc_ss, il_ss
