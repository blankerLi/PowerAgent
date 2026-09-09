"""波形产物落盘：写出 `eval/metrics.py` 能直接读取的 MAT 文件。

格式由消费侧决定，不由生产侧发明
------------------------------------
`eval/metrics.py::_load_waveform()` 用 `scipy.io.loadmat(squeeze_me=True,
struct_as_record=False)` 读取产物，并假定这样的形状：

- 每个信号名是一个顶层变量，值为含 `time` / `data` 两个字段的结构
- `step_trigger_s` / `sim_start_s` / `sim_end_s` 是顶层标量变量
- 以 `__` 开头的键（MAT 自带的 header 等）被跳过

该函数的 docstring 把自己标注为「占位实现」，因为 MATLAB 侧 `collect_signals.m`
落盘的是 `Simulink.SimulationData.Dataset` 对象，`loadmat` 读出来是不透明的
结构化数组，对不上这个形状。

本模块的选择是**让生产侧去适配已有的消费侧契约**：Python 后端直接写出
`_load_waveform()` 假定的形状，那个"占位实现"就成了真实可用的实现，
`eval/metrics.py` 一行都不用改。MATLAB 后端未来只需在 `collect_signals.m` 里
加一步转换，写成同一形状即可——两个后端共享同一个产物格式，而不是各写一个
加载器。

沿用 MAT 而不换成 npz/parquet，也是为了这个共享：MAT 是 MATLAB 侧唯一能零依赖
写出的格式，换格式会让 MATLAB 通路需要额外的第三方库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import numpy as np
from scipy.io import savemat

from poweragent.sim.backends.averaged import TimeDomainRun

__all__ = ["save_waveform_mat", "save_run_waveform"]


def save_waveform_mat(
    path: str | Path,
    *,
    time_s: np.ndarray,
    signals: Mapping[str, np.ndarray],
    step_trigger_s: float | None,
    sim_start_s: float = 0.0,
    sim_end_s: float | None = None,
) -> Path:
    """把共享同一时间轴的若干信号写成 MAT 波形产物。

    `signals` 的键就是信号名（须与 `model.yaml` 的
    `io_contract.output_signals.*.logsout_name` 一致），值为一维时间序列或形状
    `(n_time, n_channel)` 的二维数组（相电流即后者）。

    `step_trigger_s` 为 `None` 时不写该变量，加载后 `Waveform.step_trigger_s`
    也就是 `None`——这会让引用 `step_trigger` 系窗口标签的指标落
    `no_step_detected`。这是有意保留的路径：没有阶跃的场景不该伪造一个触发时刻。
    """
    target = Path(path)
    payload: dict[str, object] = {}

    for name, data in signals.items():
        array = np.asarray(data)
        if array.shape[0] != time_s.shape[0]:
            raise ValueError(
                f"信号 {name!r} 的采样点数 {array.shape[0]} 与时间轴 "
                f"{time_s.shape[0]} 不一致"
            )
        payload[name] = {"time": np.asarray(time_s), "data": array}

    payload["sim_start_s"] = float(sim_start_s)
    payload["sim_end_s"] = float(
        sim_end_s if sim_end_s is not None else time_s[-1] if time_s.size else 0.0
    )
    if step_trigger_s is not None:
        payload["step_trigger_s"] = float(step_trigger_s)

    target.parent.mkdir(parents=True, exist_ok=True)
    savemat(str(target), payload, do_compression=True)
    return target


def save_run_waveform(
    run: TimeDomainRun,
    path: str | Path,
    *,
    vout_name: str,
    iout_name: str,
    iphase_name: str,
) -> Path:
    """把一次时域仿真的结果按 `model.yaml` 声明的信号名写成产物。

    信号名由调用方从 `io_contract.output_signals` 取出后传入，本函数不读配置：
    产物的信号命名权属于 I/O 契约，不属于求解器。
    """
    return save_waveform_mat(
        path,
        time_s=run.time_s,
        signals={
            vout_name: run.vout_v,
            iout_name: run.iout_a,
            iphase_name: run.iphase_a,
        },
        step_trigger_s=run.step_trigger_s,
        sim_start_s=float(run.time_s[0]) if run.n_samples else 0.0,
        sim_end_s=float(run.time_s[-1]) if run.n_samples else None,
    )
