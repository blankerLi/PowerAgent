"""`PythonSession`：与 `MatlabSession` 同形的 Python 后端会话。

为什么不需要抽象基类
--------------------
`sim/simulate.py` 与后端之间只有一个交互面：`session.call(fn, *args, nargout=)`。
`MatlabSession` 也只有 `__enter__` / `__exit__` / `call` 三个方法。因此本类只要
提供同样的三个方法、返回同样形状的结果，`simulate()` 与 `simulate_batch()` 一行
都不用改，也不需要引入 ABC 或 Protocol 层。

这正是 design.md §1 所说的「两个扩展点是函数边界而非抽象层」：换后端换的是
传给 `simulate()` 的那个对象，不是新增一层继承体系。

白名单同样生效
--------------
`call()` 复用 `sim/engine.py` 的 `ALLOWED_MATLAB_FUNCTIONS` 与
`MatlabCallNotAllowedError`。白名单是安全边界（仿真域不接受任意命令），它属于
`sim` 层的契约而不是 MATLAB 的实现细节，因此两个后端共用同一份，而不是各自
维护一份可能漂移的副本。

预算语义的映射
--------------
`engine_starts` 在 MATLAB 侧是「真实启动 Simulink 的次数」。Python 后端没有引擎
可启动，但预算计量的本意是「昂贵仿真调用的次数」，因此这里按每次仿真调用计 1、
每次裕量分析计 1。这样 `BudgetLedger` 与 `max_engine_starts` 的口径在两个后端下
一致，预算配置不必按后端分裂。

三个状态码在本后端不会出现
--------------------------
- `engine_transient`：MATLAB 引擎崩溃或许可证瞬时失败，Python 后端无此故障模式。
- `timeout`：不做主动中断。固定步长仿真的耗时是可预测的，而中断一个正在运行的
  求解器需要多线程或子进程，会把可复现性与错误处理都复杂化。改为跑完后比对
  `runtime.max_wallclock_per_run_s`，超出即如实报 `timeout`——诚实地说，这是
  「事后发现超时」而非「按时限中断」，对预算记账的效果相同，对墙钟没有保护作用。
- `solver_error`：由求解器自身报告（平均模型的 `solve_ivp` 失败、或状态出现
  非有限值）。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping

from poweragent.sim.backends.averaged import simulate_averaged
from poweragent.sim.backends.buck_model import (
    BuckSpec,
    OperatingCondition,
    load_buck_spec,
)
from poweragent.sim.backends.margin import (
    margins_from_state_space,
    save_freq_response_mat,
)
from poweragent.sim.backends.switching import simulate_switching
from poweragent.sim.backends.waveform_io import save_run_waveform
from poweragent.sim.engine import ALLOWED_MATLAB_FUNCTIONS, MatlabCallNotAllowedError

__all__ = ["PythonSession"]


class PythonSession:
    """Python 仿真后端的会话，接口与 `MatlabSession` 一致。

    用法与 `MatlabSession` 相同，可直接替换：

        with PythonSession(base_dir=".") as session:
            simulate(candidate, scenario, session=session, ...)

    `base_dir` 用于解析 `model_cfg_json` 里的 `model_path`（相对仓库根的路径）。
    会话期间的中间产物写在一个临时目录里，`__exit__` 时删除——归档由
    `ArtifactStore` 在 `simulate()` 中完成，本会话只负责把文件放到一个调用方能
    读到的位置。
    """

    def __init__(self, base_dir: str | Path = ".") -> None:
        self._base_dir = Path(base_dir)
        self._workdir: tempfile.TemporaryDirectory[str] | None = None
        self._spec_cache: dict[str, BuckSpec] = {}

    # -- 生命周期 ---------------------------------------------------------

    def __enter__(self) -> "PythonSession":
        self._workdir = tempfile.TemporaryDirectory(prefix="poweragent_sim_")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._workdir is not None:
            self._workdir.cleanup()
            self._workdir = None

    @property
    def _work_path(self) -> Path:
        if self._workdir is None:
            raise RuntimeError(
                "PythonSession is not active; use 'with PythonSession() as session:' "
                "before calling session.call(...)"
            )
        return Path(self._workdir.name)

    # -- 调用入口 ---------------------------------------------------------

    def call(self, fn: str, /, *args: object, nargout: int = 1) -> object:
        """只接受白名单内的函数名，语义与 `MatlabSession.call()` 对齐。

        白名单校验在触碰任何仿真逻辑之前完成，因此被拒绝的调用不产生任何
        `engine_starts`，会话对后续合法调用保持可用。
        """
        if fn not in ALLOWED_MATLAB_FUNCTIONS:
            raise MatlabCallNotAllowedError(
                f"call rejected: {fn!r} is not in ALLOWED_MATLAB_FUNCTIONS "
                f"(allowed: {sorted(ALLOWED_MATLAB_FUNCTIONS)!r})"
            )

        if fn == "pa.simulate_once":
            return self._simulate_once(*args)  # type: ignore[arg-type]
        if fn == "pa.run_linear_analysis":
            return self._run_linear_analysis(*args)  # type: ignore[arg-type]
        if fn == "pa.inspect_model":
            return self._inspect_model(*args)  # type: ignore[arg-type]

        raise NotImplementedError(
            f"{fn!r} 在白名单内但 Python 后端尚未实现。"
            "批量仿真（pa.simulate_batch）与观测量导出（pa.export_observables）"
            "在本阶段不需要：execution_mode 为 serial，观测量由 eval 层直接从"
            "波形产物计算。"
        )

    # -- 各白名单函数的实现 -----------------------------------------------

    def _simulate_once(
        self, model_cfg_json: str, params_json: str, scenario_json: str
    ) -> dict[str, Any]:
        model_cfg = json.loads(model_cfg_json)
        params = json.loads(params_json)
        scenario = json.loads(scenario_json)

        spec = self._load_spec(model_cfg["model_path"])
        condition = _condition_from(scenario)
        solver = model_cfg["io_contract"]["solver"]
        guard = model_cfg["io_contract"]["divergence_guard"]

        started = time.perf_counter()
        if spec.variant == "switching":
            run = simulate_switching(
                spec,
                condition,
                rcomp_ohm=float(params["rcomp"]),
                ccomp_f=float(params["ccomp"]),
                stop_time_s=float(solver["stop_time"]),
                time_step_s=float(solver["max_step"]),
                guard_vout_abs_max=float(guard["vout_abs_max"]),
                guard_iphase_abs_max=float(guard["iphase_abs_max"]),
            )
        else:
            run = simulate_averaged(
                spec,
                condition,
                rcomp_ohm=float(params["rcomp"]),
                ccomp_f=float(params["ccomp"]),
                stop_time_s=float(solver["stop_time"]),
                rel_tol=float(solver["rel_tol"]),
                guard_vout_abs_max=float(guard["vout_abs_max"]),
                guard_iphase_abs_max=float(guard["iphase_abs_max"]),
            )
        elapsed_s = time.perf_counter() - started

        status = run.status
        wallclock_limit_s = float(model_cfg["runtime"]["max_wallclock_per_run_s"])
        if status == "ok" and elapsed_s > wallclock_limit_s:
            # 事后发现超时（不做主动中断，见模块 docstring）。
            status = "timeout"

        result: dict[str, Any] = {
            "status": status,
            "elapsed_ms": int(round(elapsed_s * 1000.0)),
            "engine_starts": 1,
        }

        if status == "ok":
            signals = model_cfg["io_contract"]["output_signals"]
            run_id = str(scenario.get("run_id") or "run")
            path = save_run_waveform(
                run,
                self._work_path / f"{run_id}_waveform.mat",
                vout_name=signals["vout"]["logsout_name"],
                iout_name=signals["iout"]["logsout_name"],
                iphase_name=signals["iphase"]["logsout_name"],
            )
            result["waveform_path"] = str(path)

        return result

    def _run_linear_analysis(
        self,
        model_cfg_json: str,
        params_json: str,
        scenario_json: str,
        margin_cfg_json: str,
    ) -> dict[str, Any]:
        """频域裕量分析。

        裕量始终在**平均模型**上提取，与场景声明的 `model_variant` 无关：
        `margin_extraction.primary_method = linear_analysis_on_averaged` 就是这个
        意思，而需要采集裕量的评价场景用的是开关模型。因此这里把 `model_path`
        换成平均模型的入口，而不是沿用场景的变体。
        """
        model_cfg = json.loads(model_cfg_json)
        params = json.loads(params_json)
        scenario = json.loads(scenario_json)
        margin_cfg = json.loads(margin_cfg_json)

        method = margin_cfg.get("primary_method")
        if method != "linear_analysis_on_averaged":
            raise NotImplementedError(
                f"Python 后端只实现 primary_method='linear_analysis_on_averaged'，"
                f"收到 {method!r}。另一取值 'freq_response_estimator_on_switching' "
                "需要对开关模型做频响估计（扫频注入），本阶段未实现。"
            )

        averaged_entry = model_cfg["model_package"]["averaged"]["entry"]
        spec = self._load_spec(averaged_entry)
        condition = _condition_from(scenario)

        started = time.perf_counter()
        try:
            result = margins_from_state_space(
                spec,
                condition,
                rcomp_ohm=float(params["rcomp"]),
                ccomp_f=float(params["ccomp"]),
            )
        except ValueError as exc:
            # 环路不稳定到裕量无从定义。不落盘产物，让 eval/margin.py 因缺少
            # freq_response_path 而落 extraction_failed——不返回编造的数值。
            return {
                "status": "margin_undefined",
                "reason": str(exc),
                "freq_response_path": "",
                "method": method,
                "engine_starts": 1,
                "elapsed_ms": int(round((time.perf_counter() - started) * 1000.0)),
            }

        run_id = str(scenario.get("run_id") or "run")
        path = save_freq_response_mat(
            self._work_path / f"{run_id}_freq_response.mat", result
        )

        return {
            "status": "ok",
            "freq_response_path": str(path),
            "method": method,
            "engine_starts": 1,
            "elapsed_ms": int(round((time.perf_counter() - started) * 1000.0)),
        }

    def _inspect_model(self, model_cfg_json: str) -> dict[str, Any]:
        """回报模型的可注入参数与输出信号，供 preflight 核对 I/O 契约。

        Python 后端的"可注入参数"就是两个设计变量；输出信号取自模型文件能产出的
        三个。返回的是**后端实际支持的能力**，而非把配置里声明的内容原样回读——
        后者无法发现契约与实现不符。
        """
        model_cfg = json.loads(model_cfg_json)
        spec = self._load_spec(model_cfg["model_path"])

        return {
            "injectable_params": ["rcomp", "ccomp"],
            "output_signals": ["vout", "iout", "iphase"],
            "n_phase": spec.n_phase,
            "model_variant": spec.variant,
        }

    # -- 内部 -------------------------------------------------------------

    def _load_spec(self, model_path: str) -> BuckSpec:
        """按路径装载并缓存电路参数。

        同一会话内会对同一个模型文件反复调用（每个候选每个场景一次），YAML 解析
        虽然只占单次仿真耗时的千分之几，但缓存也顺带保证了会话期内读到的是同一
        份参数——`simulate()` 已在每次调用前做过依赖闭包的不变性检查，这里不重复
        校验文件是否被改动。
        """
        if model_path not in self._spec_cache:
            self._spec_cache[model_path] = load_buck_spec(self._base_dir / model_path)
        return self._spec_cache[model_path]


def _condition_from(scenario: Mapping[str, Any]) -> OperatingCondition:
    return OperatingCondition(
        vin_v=float(scenario["vin_v"]),
        temp_c=float(scenario["temp_c"]),
        load_start_a=float(scenario["load_start_a"]),
        load_end_a=float(scenario["load_end_a"]),
        slew_a_per_us=float(scenario["slew_a_per_us"]),
    )
