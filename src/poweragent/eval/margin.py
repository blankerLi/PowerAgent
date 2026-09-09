"""poweragent/eval/margin.py

稳定性裕量的「计算」阶段（`design.md` §6.5.2 / §8.5；`tasks.md` 任务 11.3；
需求追溯 R6.12, R6.13, R8.5, R8.6）。

## 本模块的范围：只有「计算」，没有「采集」

`design.md` §8.5 把裕量链路拆成两阶段：**阶段A 采集**（在已注入参数/场景的
模型上跑 `linearize`/`frestimate`，计预算、进 `simulation_key`，由
`matlab/+pa/run_linear_analysis.m`——任务 11.1——与 `sim/simulate.py` 的
`require_margin` 内联调用——任务 7.2——完成）与**阶段B 计算**（从阶段A 已落盘
的频响引用算出 `phase_margin`/`gain_margin` 两个数值，不计预算、进
`evaluation_key`，即本模块）。`extract_margin()` 的函数体内**不启动仿真、不
访问 MATLAB Engine、不 import `sim.*`**——它只做两件事：读一个已存在的 MAT
文件、算两个数。`cross_check_margin()` 则完全不碰文件，只是两个 `MarginPoint`
的纯数值比较。

## `MARGIN_FAILURE_ESCALATION_THRESHOLD` 为什么暂放在本模块，而不是
`controller/stop.py`

`design.md` §8.4 与本任务描述都明确把这个常量登记为「`controller/stop.py` 的
模块常量」——`stop_and_ask_human()` 熔断的判定与触发，语义上属于 `controller`
层的编排职责（`run_task()` 维护「连续几次裕量提取失败」这个任务级计数器，
命中阈值时调用 `stop_and_ask_human(cause='metric_pipeline_error')`）。但
`controller/stop.py` 本身尚未落地（`tasks.md` 任务 14.3，属于 M4 之前不开发
的 `controller` 编排层，本次批次未派发）；本任务（11.3）的范围明确限定为
`eval/margin.py` 一个文件。

把这个常量提前建到一个只含它一行内容的 `controller/stop.py`（抢在任务 14.3
之前落地这个文件名），有具体的风险：任务 14.3 的真实范围是 `should_stop()`
及其完整的熔断判定逻辑，那个任务的实现者需要自己决定这个文件的模块结构、
`import` 边界（`controller/stop.py` 是否该 import `eval.margin`，还是反过来）、
以及是否还有其他熔断阈值要与这个常量放在一起。提前创建这个文件、哪怕只放
一个常量，等于替任务 14.3 做了一个它本该自己做的结构决策——如果 14.3 落地
时发现更合适的组织方式（例如把阈值做成一个小的 `dataclass` 而不是裸常量），
还要先处理「这个常量已经被 11.3 建到别处了」这一层历史包袱。

因此本模块把 `MARGIN_FAILURE_ESCALATION_THRESHOLD = 3` **作为
`eval/margin.py` 自己的模块常量**定义并导出（`extract_margin()` 自身不使用
这个常量——见下方「不在本模块内实现的分层控制流」一节，它纯粹是给下游一个
现成的、值已经钉死为 3 的常量可以直接 import）。等 `controller/stop.py`
（任务 14.3）落地时，该模块应当从这里 `import` 这个常量（`from
poweragent.eval.margin import MARGIN_FAILURE_ESCALATION_THRESHOLD`）或者把
它移过去并在这里留一个向后兼容的重导出——两种方式都不会导致这个值本身出现
第二份定义。**这个值本身不是本模块的计算逻辑需要用到的东西，只是暂存位置。**

## 不在本模块内实现的分层控制流（信息性文档，供 `controller/run_task.py`——
任务 14.4/14.5——的实现者参考）

`design.md` §8.4（`run_task()` 伪代码）与 `requirements.md` R6.12/R6.13 描述
的、裕量提取失败之后的分层控制流与熔断计数器，**都不是 `extract_margin()`
自身能够实现的**——`extract_margin()` 的函数签名（`freq_response_ref,
metrics_cfg, *, run_id`）里没有任何参数告诉它「这次调用对应的场景属于
`screening` 还是 `evaluation` 层」，也没有任何参数携带「本任务迄今连续失败了
几次」这个跨调用的状态。这两样东西都是**调用方**（`run_task()`）在编排循环
里持有的上下文，`extract_margin()` 每次调用都是无状态的纯函数（同一份频响
引用喂两次，产出两次相同的结果），不应该也不能够去猜测或反向推断这些上下文。

记录在此，供 `controller/run_task.py` 落地时对照 `design.md` §8.4 伪代码
接线：

1. **`extract_margin()` 返回 `(pm, gm)` 后，调用方检查 `pm.valid`**（`gm.valid`
   与 `pm.valid` 恒同步——见下方 `extract_margin()` 的实现说明，两条
   `MetricResult` 在失败路径上总是同时置为 `valid=False`，调用方检查其中
   任一个即可，`design.md` §8.4 伪代码只检查 `pm.valid`）。
2. **`pm.valid=False` 时**：调用方以 `store.close_run_failed(run_id,
   'candidate_rejected', 'metric_invalid:phase_margin')` 关闭该 `runs` 行
   （不是本模块的职责——本模块不持有 `Store` 引用、不写数据库，与
   `eval/metrics.py` 的既有边界一致）。随后按该场景的 `tier` 分岔：
   - `tier == 'screening'`：**提前拒绝**——结束该候选的执行，不再执行该候选
     的后续场景行（`return false`，见 §8.4 伪代码）。该层成本敏感、场景行数
     远小于 Evaluation，完整证据的价值不足以抵消成本。
   - `tier == 'evaluation'`：**记录后继续跑完该层其余场景行**（`CONTINUE`，
     不 `return`）。worst-case 聚合的 SQL（`store.repo.WORST_CASE_SQL`）已经
     因为这一行 `constraint_results`/`metric_results` 缺失或不可行而在
     `HAVING COUNT(DISTINCT scenario_id) = (SELECT COUNT(*) FROM eval_set)`
     处自然排除该候选，提前终止省下的预算换不来任何额外结论；且裕量提取
     失败往往是提取实现问题（例如本模块下方标注的 FRD 交叉点算法在特定
     频响形状下失败）而非候选本身的问题，需要跑完完整证据才能定位是哪些
     工况下提取失败。
3. **熔断计数器**（`run_task()` 任务级状态，不是本模块状态）：维护一个
   「同一任务内连续裕量提取失败次数」的计数器，覆盖该任务下**全部候选、
   全部场景**（不是按候选分别计数）——每次 `extract_margin()` 调用返回
   `pm.valid=False` 时计数器加一；返回 `pm.valid=True` 时计数器复位为 0；
   计数器达到 `MARGIN_FAILURE_ESCALATION_THRESHOLD`（本模块导出的常量，
   值为 3）时，调用 `stop_and_ask_human(cause='metric_pipeline_error')`
   停止整个任务（这个界区分的是「个别候选在个别工况下提取不出裕量」与
   「提取链路本身坏了」——前者是候选级、偶发的失败，后者需要人工介入排查
   提取实现，两者用同一个连续计数器区分，不需要按任务调整阈值）。

`extract_margin()` 本身**不**维护、不接触、不返回任何与「连续第几次失败」
相关的状态——它是纯函数，每次独立判定这一次调用是否成功。

## MAT 文件字段名假设：哪些已用合成数据验证，哪些未经真实 MATLAB 核对

见下方两个私有函数（`_extract_from_averaged_linearization` /
`_extract_from_switching_frd`）各自的 docstring；此处汇总：

- `linear_analysis_on_averaged`：期望 `margin_data` 变量是一个结构体，含
  `PhaseMargin`（deg）与 `GainMargin`（**线性比值，非 dB**——MATLAB
  `allmargin()` 的文档化行为）两个字段。这两个字段名直接取自 MATLAB
  `allmargin()` 的标准输出字段（`GMFrequency`/`GainMargin`/`PMFrequency`/
  `PhaseMargin`/`DelayMargin`/`DMFrequency`/`Stable`），且与
  `matlab/+pa/run_linear_analysis.m` 实际执行的 `margin_data =
  allmargin(linsys)` 一致——**字段名的选取有 MATLAB 官方文档依据**，但
  `scipy.io.loadmat(struct_as_record=False, squeeze_me=True)` 把这个 MATLAB
  结构体在 Python 侧还原成 `mat_struct` 对象、其属性访问方式是否与本模块
  假设的完全一致，**未经真实 MATLAB 环境验证**（本环境 Simulink 许可证当前
  不可用，见任务上下文）。已用合成 `scipy.io.savemat` 数据验证的是「假设该
  还原方式成立的前提下，本模块的读取与 dB 换算逻辑是正确的」。
- `freq_response_estimator_on_switching`：期望 `frd_data` 变量含
  `Frequency`（`run_linear_analysis.m` 以 `rad/s` 传入 `frestimate`，因此
  假定落盘的 `frd` 对象的 `Frequency` 属性单位为 `rad/s`——不影响本模块
  内的交叉点算法，因为交叉点插值不需要频率单位换算，只用于确定"哪两个采样
  点夹住了穿越点"）与 `ResponseData`（复数频响，MATLAB `frd` 对象对 SISO
  系统的标准属性名，形状通常为 `1x1xN`，本模块假定 `squeeze_me=True` 后已
  压缩为长度 N 的一维复数数组）两个字段。**这两个字段名、以及"压缩后是一维
  复数数组"这个形状假设，均未经真实 MATLAB `frd` 对象的 `save`/`loadmat`
  往返验证**——MATLAB 的 `frd` 是一个类对象（不是普通 struct），`save()` 后
  `scipy.io.loadmat` 读取类对象的行为本就是本模块顶部大量类似占位说明
  （`eval/metrics.py` 对 `Simulink.SimulationData.Dataset` 的同类已知缺口）
  提到的高风险区域。已用合成数据验证的是「假设这两个字段能以预期的
  Python 数组/复数数组形式读到的前提下，本模块的穿越点插值与裕量换算算法
  本身在已知解析解的二阶系统上给出正确答案」——算法正确性已验证，MATLAB
  侧真实存盘格式的匹配未验证，两者是独立的风险点，此处分别标注。

## `GainMargin` 线性比值 → dB 换算（易错的正确性细节）

MATLAB `allmargin()`（以及 `margin()`）的 `GainMargin` 字段是**线性增益比值
（gain ratio）**，不是 dB——这是 MATLAB Control System Toolbox 的标准文档化
行为（例如 `GainMargin = 2.0` 表示环路增益还可以再增大 2 倍才会到达
0 dB/穿越点，对应 `20 * log10(2.0) ≈ 6.02 dB`）。本模块因此在
`_extract_from_averaged_linearization()` 内显式做
`gain_margin_db = 20.0 * math.log10(gain_margin_linear)` 换算——**遗漏这一步
（直接把线性比值当 dB 数值使用）是这条链路里最容易犯的错误**，因此在此单独
成节强调。`freq_response_estimator_on_switching` 分支不需要这个换算，因为
该分支的交叉点算法直接在 dB 域（`20*log10(|response|)`）内工作，产出的
`gain_margin_db` 本来就已经是 dB。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from poweragent.config.schema import CrossCheckTolerance, MetricsConfig
from poweragent.store.repo import MetricResult

__all__ = [
    "MARGIN_FAILURE_ESCALATION_THRESHOLD",
    "MarginPoint",
    "CrossCheckVerdict",
    "extract_margin",
    "cross_check_margin",
]


# ===========================================================================
# 熔断阈值（design.md §8.4；requirements.md R6.13）——暂存位置说明见模块顶部
# docstring「`MARGIN_FAILURE_ESCALATION_THRESHOLD` 为什么暂放在本模块」一节。
# 不落配置项：它区分的是「个别候选在个别工况下提取不出裕量」与「提取链路
# 坏了」，这个界不需要按任务调整。
# ===========================================================================

MARGIN_FAILURE_ESCALATION_THRESHOLD = 3


# ===========================================================================
# MarginPoint：design.md §6.5.2 的 cross_check_margin() 引用了这个类型但未在
# 别处给出字段定义——本模块自行设计并在此登记（与 eval/metrics.py 的
# `Waveform` 是同一处境：签名给定、字段形状未给定）。
# ===========================================================================


@dataclass(frozen=True, slots=True)
class MarginPoint:
    """由**同一次**裕量提取（同一候选、同一场景、同一方法）产出的一对裕量
    数值，供 `cross_check_margin()` 比较「用 `primary_method` 提取的这一对」
    与「用 `cross_check_method` 提取的另一对」之间的偏差。"""

    phase_margin_deg: float
    gain_margin_db: float


@dataclass(frozen=True, slots=True)
class CrossCheckVerdict:
    """`cross_check_margin()` 的返回类型（design.md §6.5.2，逐字段照抄）。"""

    ok: bool
    phase_delta_deg: float
    gain_delta_db: float


# ===========================================================================
# extract_margin()：阶段B 计算（design.md §6.5.2 / §8.5）
# ===========================================================================


def extract_margin(
    freq_response_ref: str, metrics_cfg: MetricsConfig, *, run_id: str
) -> tuple[MetricResult, MetricResult]:
    """从已采集的频响数据（`freq_response_ref` 指向的 MAT 文件，由
    `matlab/+pa/run_linear_analysis.m` / `sim/simulate.py` 的
    `require_margin` 内联采集落盘）算 `(phase_margin, gain_margin)`。

    **纯计算，不启动仿真、不计预算**——本函数体内不 import `sim.*`、不访问
    `MatlabSession`、不产生任何 `engine_starts`，`budget_units` 增量恒为 0
    （这两点由「本函数不持有任何仿真会话引用、不调用任何仿真接口」这一事实
    保证，不需要额外的运行期断言）。

    按 `metrics_cfg.margin_extraction.primary_method` 分支到对应的读取/计算
    实现（见下方两个私有函数）。**每次调用恰好返回两条 `MetricResult`**
    （`metric_id` 分别为 `"phase_margin"` / `"gain_margin"`），两条在同一次
    调用内**总是同步成功或同步失败**——裕量的相位裕量与增益裕量来自同一次
    频响采集/同一个交叉点算法运行，没有"相位裕量算出来了但增益裕量算不出来"
    这种部分失败的中间态。

    任何加载/解析/提取失败（文件不存在、MAT 内容不含预期变量、字段形状与
    假设不符、FRD 交叉点算法在给定频率范围内找不到穿越点等）都在本函数内
    捕获，**不向外传播异常**——按 `requirements.md` R6.6/R8.6 与本任务描述
    的「采集或提取失败 ⟹ 两条均 valid=False / value=None /
    invalid_reason='extraction_failed'，不填默认值」，返回失败对而不是让
    调用方处理异常。
    """

    try:
        method = metrics_cfg.margin_extraction.primary_method
        if method == "linear_analysis_on_averaged":
            phase_margin_deg, gain_margin_db = _extract_from_averaged_linearization(
                freq_response_ref
            )
        elif method == "freq_response_estimator_on_switching":
            phase_margin_deg, gain_margin_db = _extract_from_switching_frd(
                freq_response_ref
            )
        else:  # pragma: no cover - config.schema 已把该字段强制为二值枚举
            raise ValueError(f"未知的 primary_method: {method!r}")
    except Exception:
        return (
            MetricResult(
                run_id=run_id, metric_id="phase_margin", value=None, valid=False,
                invalid_reason="extraction_failed",
            ),
            MetricResult(
                run_id=run_id, metric_id="gain_margin", value=None, valid=False,
                invalid_reason="extraction_failed",
            ),
        )

    return (
        MetricResult(
            run_id=run_id, metric_id="phase_margin", value=phase_margin_deg, valid=True
        ),
        MetricResult(
            run_id=run_id, metric_id="gain_margin", value=gain_margin_db, valid=True
        ),
    )


def _mat_struct_field(struct: object, *names: str) -> object:
    """从 `scipy.io.loadmat(struct_as_record=False, squeeze_me=True)` 还原出
    的 `mat_struct`（或其他属性对象）上按候选字段名列表依次取值，返回第一个
    存在的属性；全部不存在则抛 `AttributeError`。存在多个候选名是因为 MATLAB
    结构体字段名的大小写/形式在跨版本或不同 `save` 选项下可能有出入（本模块
    未经真实 MATLAB 验证，见模块顶部说明），以最小成本兼容常见写法。"""

    for name in names:
        if hasattr(struct, name):
            return getattr(struct, name)
    raise AttributeError(
        f"MAT struct 缺少字段（尝试过 {names!r}）: {struct!r}"
    )


def _pick_worst_margin_value(values: object, *, prefer_min_abs: bool) -> float:
    """`allmargin()` 的 `PhaseMargin`/`GainMargin` 字段在系统存在多个增益/
    相位穿越点时是**向量**（每个穿越点各一个值），而不是标量；经
    `squeeze_me=True` 加载后，单元素向量会被压缩为标量（`float`/0 维数组），
    多元素向量仍是 1 维数组。本函数统一两种情形：标量原样返回；向量按
    `prefer_min_abs=True`（两种指标都适用——离 0（相位）/离 1（线性增益比值,
    对应 0 dB）越近，代表该条穿越点的稳定性裕量越小、越是"worst case"）取
    绝对值最小的一个元素，镜像 MATLAB `margin()` 命令（区别于 `allmargin()`）
    自动挑选 worst-case 穿越点的既定行为——`allmargin()` 本身不做这一步筛选,
    是本函数替它做。"""

    arr = np.atleast_1d(np.asarray(values, dtype=float))
    if arr.size == 1:
        return float(arr.reshape(-1)[0])
    if prefer_min_abs:
        idx = int(np.argmin(np.abs(arr)))
        return float(arr[idx])
    return float(arr[0])


def _extract_from_averaged_linearization(freq_response_ref: str) -> tuple[float, float]:
    """`primary_method='linear_analysis_on_averaged'` 分支：读取
    `matlab/+pa/run_linear_analysis.m` 以 `save(freq_response_path,
    'margin_data', 'linsys')` 落盘的 MAT 文件，取其中 `margin_data`
    （`allmargin(linsys)` 的输出结构体）的 `PhaseMargin`（deg）与
    `GainMargin`（**线性比值**，见模块顶部专门成节的换算说明）两个字段。

    `scipy.io.loadmat(struct_as_record=False, squeeze_me=True)`——与
    `eval/metrics.py` 的 `_load_waveform()`（任务 10.1 已落地的先例）同一组
    参数，理由相同：`struct_as_record=False` 让 MATLAB 结构体以属性访问
    （`.PhaseMargin`）而非字段索引的方式还原，`squeeze_me=True` 把
    单元素数组压成标量，避免到处写 `float(x[0][0])` 之类的样板代码。

    返回 `(phase_margin_deg, gain_margin_db)`；任何字段缺失或形状异常均向上
    抛出异常，由 `extract_margin()` 统一捕获转译为提取失败。
    """

    import scipy.io as sio

    raw = sio.loadmat(freq_response_ref, struct_as_record=False, squeeze_me=True)
    margin_data = raw["margin_data"]

    phase_margin_deg = _pick_worst_margin_value(
        _mat_struct_field(margin_data, "PhaseMargin"), prefer_min_abs=True
    )
    gain_margin_linear = _pick_worst_margin_value(
        _mat_struct_field(margin_data, "GainMargin"), prefer_min_abs=True
    )

    if not math.isfinite(phase_margin_deg) or not math.isfinite(gain_margin_linear):
        raise ValueError("PhaseMargin/GainMargin 含非有限值")
    if gain_margin_linear <= 0.0:
        # allmargin() 用 Inf 表示"无穿越点/裕量无限大"；非正值不是合法的线性
        # 增益比值（log10 域外），视为提取失败而非静默钳制。
        raise ValueError(f"GainMargin 非正，无法换算为 dB: {gain_margin_linear!r}")

    gain_margin_db = 20.0 * math.log10(gain_margin_linear)
    return phase_margin_deg, gain_margin_db


def _extract_from_switching_frd(freq_response_ref: str) -> tuple[float, float]:
    """`primary_method='freq_response_estimator_on_switching'` 分支：读取
    `matlab/+pa/run_linear_analysis.m` 以 `save(freq_response_path,
    'frd_data')` 落盘的 MAT 文件，取其中 `frd_data`（`frestimate(...)` 的
    输出，MATLAB `frd` 对象）的 `Frequency`（假定单位 rad/s，见模块顶部说明,
    交叉点插值不需要换算这个单位）与 `ResponseData`（复数频响）两个字段，
    经增益/相位穿越点插值算出 `(phase_margin_deg, gain_margin_db)`。

    **这是本模块唯一实现真正数值算法（而非单纯字段搬运）的分支**——`frd`
    对象不像 `allmargin()` 那样已经算好裕量，只提供原始频响数据，需要本函数
    自己找增益穿越频率（|G(jw)|=1 即 0 dB 处）与相位穿越频率（相位=-180°处）
    并线性插值读出对应的相位/幅值。算法与假设的字段名均未经真实 MATLAB `frd`
    对象验证（模块顶部已详细说明），此处只再次标注这一点。

    返回 `(phase_margin_deg, gain_margin_db)`；找不到任一穿越点（响应在给定
    频率范围内从未跨过 0 dB 或 -180°）时抛出异常，由 `extract_margin()`
    统一捕获转译为提取失败。
    """

    import scipy.io as sio

    raw = sio.loadmat(freq_response_ref, struct_as_record=False, squeeze_me=True)
    frd_data = raw["frd_data"]

    response = np.asarray(
        _mat_struct_field(frd_data, "ResponseData", "responsedata"), dtype=complex
    ).reshape(-1)
    if response.size < 2:
        raise ValueError("ResponseData 采样点不足，无法插值找穿越点")

    magnitude_db = 20.0 * np.log10(np.abs(response))
    phase_deg = np.degrees(np.unwrap(np.angle(response)))

    phase_at_gain_crossover = _interp_crossing(magnitude_db, 0.0, phase_deg)
    if phase_at_gain_crossover is None:
        raise ValueError("响应在给定频率范围内未穿越 0 dB，找不到增益穿越点")
    phase_margin_deg = 180.0 + phase_at_gain_crossover

    magnitude_at_phase_crossover_db = _interp_crossing(phase_deg, -180.0, magnitude_db)
    if magnitude_at_phase_crossover_db is None:
        raise ValueError("响应在给定频率范围内未穿越 -180°，找不到相位穿越点")
    gain_margin_db = -magnitude_at_phase_crossover_db

    if not math.isfinite(phase_margin_deg) or not math.isfinite(gain_margin_db):
        raise ValueError("插值结果含非有限值")

    return phase_margin_deg, gain_margin_db


def _interp_crossing(
    test_values: np.ndarray, threshold: float, paired_values: np.ndarray
) -> float | None:
    """在 `test_values`（按频率升序排列的采样序列）中找**第一个**穿越
    `threshold` 的位置（相邻两点一个 >= threshold、另一个 < threshold，或
    恰好等于 threshold），对该位置在 `test_values` 上做线性插值确定穿越点在
    两采样点之间的相对位置，再用同一个插值比例对 `paired_values`（与
    `test_values` 逐点配对的另一个物理量，例如 `test_values` 是幅值时
    `paired_values` 是相位）取值，返回插值后的 `paired_values`。

    找不到任何穿越（`test_values` 全程都在 `threshold` 的同一侧）时返回
    `None`——由调用方（`_extract_from_switching_frd`）转译为「未找到穿越点」
    的提取失败。取「第一个」穿越点（而不是全部穿越点里再挑 worst-case）是
    因为 `frd` 原始响应通常只在目标环路带宽附近有一次穿越；若响应形状复杂到
    存在多次穿越，取第一个是本函数的显式简化选择，未在合成验证之外的真实
    频响数据上核对过这个选择在多穿越场景下是否仍是"worst case"意义上正确的
    一个。
    """

    diff = test_values - threshold
    n = len(diff)
    for i in range(n - 1):
        d0, d1 = diff[i], diff[i + 1]
        if d0 == 0.0:
            return float(paired_values[i])
        if d0 * d1 < 0.0:
            t = d0 / (d0 - d1)
            return float(paired_values[i] + t * (paired_values[i + 1] - paired_values[i]))
    if diff[-1] == 0.0:
        return float(paired_values[-1])
    return None


# ===========================================================================
# cross_check_margin()：开发期一次性使用（design.md §6.5.2 / §8.5）
# ===========================================================================


def cross_check_margin(
    primary: MarginPoint, secondary: MarginPoint, tol: CrossCheckTolerance
) -> CrossCheckVerdict:
    """比较用 `primary_method` 提取的裕量点与用 `cross_check_method` 提取的
    裕量点之间的偏差，判定是否在容差内（design.md §6.5.2，逐字段照抄）。

    `ok = phase_delta_deg <= tol.phase_deg and gain_delta_db <= tol.gain_db`
    （边界取等号视为通过，与 `CrossCheckTolerance.phase_deg`/`gain_db`
    在 `config/schema.py` 声明为 `Field(gt=0)`——严格正数——的容差语义一致：
    容差本身不可能是 0，因此"小于等于容差"与"小于容差"在这里不构成实质
    差异，选择 `<=` 是更常见、更不易引入意外拒绝的边界处理惯例）。

    **开发期一次性使用，运行期不重复调用**（`design.md` §8.5
    `margin_cross_check_once` 是一个独立的开发期工作流，不是 `run_task()`
    循环的一部分——与本代码库对 Dual-Model Consistency 的既有处理原则
    「运行期不复核」同构，见 `config/dual_model_consistency.py` /
    `controller/preflight.py` 顶部对该原则的既有文档化说明）。**这一点本
    函数自身无法在结构上强制**——`cross_check_margin()` 只是一个纯比较
    函数，它没有任何机制阻止调用方在运行期反复调用它；「不应该在运行期
    循环里调用」是给调用方的文档约束，不是本函数的运行期检查（正如
    `design.md` 本身把 `margin_cross_check_once` 与运行期的
    `run_task()` 循环写成两段完全独立的伪代码，从未在 `run_task()` 的
    主循环里出现过对 `cross_check_margin()` 的调用）。
    """

    phase_delta_deg = abs(primary.phase_margin_deg - secondary.phase_margin_deg)
    gain_delta_db = abs(primary.gain_margin_db - secondary.gain_margin_db)
    ok = phase_delta_deg <= tol.phase_deg and gain_delta_db <= tol.gain_db
    return CrossCheckVerdict(
        ok=ok, phase_delta_deg=phase_delta_deg, gain_delta_db=gain_delta_db
    )
