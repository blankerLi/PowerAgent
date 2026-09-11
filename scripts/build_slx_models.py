"""从 models/buck4ph_*.yaml 生成 MATLAB 后端的 .slx 模型。

用法（需要 MATLAB + Simulink 许可证）：

    python scripts/build_slx_models.py                # 生成全部已实现的变体
    python scripts/build_slx_models.py --variant averaged

参数源是 `models/buck4ph_*.yaml`（`model_package.<variant>.entry`），与 Python 后端
读的是同一份文件——两个后端的电路参数因此不可能漂移。本脚本只做「读参数 → 组装
spec → 交给 MATLAB 侧 build_slx_model.m 搭图」，不含任何电路知识：方程与连线全部
在 `matlab/build_slx_model.m` 里，可审查。

## 为什么这个脚本直接 import matlab.engine

design.md §2.1/§14 的边界是「`sim` 是 PowerAgent 系统中唯一 import MATLAB Engine
的包」，约束对象是 `src/poweragent/` 下的**包代码**——那条边界的用途是保证寻优执行
路径上对 MATLAB 的访问只有一个入口、且只能调 `pa.*` 白名单函数。

本脚本不在那条路径上：它是开发期一次性工具（与 `scripts/margin_cross_check.py` /
`scripts/dual_model_consistency.py` 同类），产出物是 `.slx` 文件，跑完就结束，
`run_task()` 从不调用它。`build_slx_model` 也刻意**不**放进 `matlab/+pa/` 包，因此
不在 `MatlabSession` 的白名单里、也不进 `model_package_hash` 的依赖闭包
（`model.yaml` 的 `matlab_functions: [matlab/+pa]` 只展开那个目录）——它是模型的
**生成器**，不是被仿真的对象。把生成器计入闭包会让「改了生成脚本的一处注释」也使
全部旧结论失效，那是过度敏感而不是严格。

反过来说，本脚本也不能复用 `MatlabSession`：那个类的 `call()` 只接受 `pa.*` 白名单，
而给它开一个例外就会让白名单从「`pa.*` 之外一律拒绝」变成「`pa.*` 加若干例外」。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import yaml  # noqa: E402

from poweragent.config.loader import load_all  # noqa: E402

# 输出采样间隔：与 Python 后端的 `output_dt_s` 默认值一致（averaged.py /
# switching.py 的同名形参默认 0.2 µs）。平均模型用它作为 MaxStep，开关模型用它
# 作为记录抽取间隔——两处都需要这个值，而它在 Python 侧是求解器函数的默认参数、
# 不在任何配置文件里，因此在这里显式重述一次并标注来源。
OUTPUT_DT_S = 0.2e-6

IMPLEMENTED_VARIANTS = ("averaged", "switching")


def build_spec(variant: str) -> dict[str, object]:
    """组装交给 `build_slx_model.m` 的 spec。

    电路参数全部来自 `model_package.<variant>.entry` 指向的 YAML；求解器与信号名
    来自 `configs/model.yaml` 的 `io_contract`；`.slx` 的落点来自同一节的
    `slx_entry`——即"生成到哪里"与"运行期从哪里加载"是同一个字段，不可能不一致。

    公开（无下划线前缀）是因为 `tests/matlab/test_matlab_backend.py` 复用它来验证
    "同一 spec 重复生成得到同一个 `model_package_hash`"。那条测试必须用与真实生成
    完全相同的 spec，自己拼一份就成了两处定义、会漂移。
    """
    bundle = load_all(REPO_ROOT / "configs")
    model_cfg = bundle.model

    section = getattr(model_cfg.model_package, variant)
    if section is None:
        raise SystemExit(f"model.yaml 的 model_package.{variant} 为空，无从生成")
    if not section.slx_entry:
        raise SystemExit(
            f"model.yaml 的 model_package.{variant}.slx_entry 未登记，"
            f"不知道该把 .slx 生成到哪里"
        )

    raw = yaml.safe_load((REPO_ROOT / section.entry).read_text(encoding="utf-8"))
    stage = raw["power_stage"]
    point = raw["operating_point"]
    control = raw["control"]
    modulator = raw["modulator"]

    out_path = REPO_ROOT / section.slx_entry
    solver = model_cfg.io_contract.solver
    signals = model_cfg.io_contract.output_signals

    return {
        "model_name": Path(section.slx_entry).stem,
        "model_variant": variant,
        "out_path": str(out_path),
        # 功率级
        "n_phase": int(stage["n_phase"]),
        "fsw_hz": float(stage["fsw_hz"]),
        "l_per_phase_h": float(stage["l_per_phase_h"]),
        "cout_f": float(stage["cout_f"]),
        "cout_esr_ohm": float(stage["cout_esr_ohm"]),
        "dcr_per_phase_ohm": float(stage["dcr_per_phase_ohm"]),
        "rds_on_ohm": float(stage["rds_on_ohm"]),
        # 工作点
        "vin_nom_v": float(point["vin_nom_v"]),
        "vout_nom_v": float(point["vout_nom_v"]),
        "iout_nom_a": float(point["iout_nom_a"]),
        # 控制
        "gm_s": float(control["gm_s"]),
        "ri_ohm": float(control["ri_ohm"]),
        "i_limit_a": float(control["i_limit_a"]),
        # 调制器
        "duty_min": float(modulator["duty_min"]),
        "duty_max": float(modulator["duty_max"]),
        "phase_shift_deg": float(modulator["phase_shift_deg"]),
        "dead_time_s": float(modulator["dead_time_s"]),
        # 设计变量的出厂默认值：取 model.yaml 的 baseline（一个落在域内的工程整定）。
        # 运行期这两个块的值由 apply_params.m 覆盖，因此这里的取值只影响"刚生成出来
        # 的模型直接打开跑一次"会看到什么，不影响任何被判定的结果。取 baseline 而非
        # 任意数字，是为了让那次手动打开的仿真结果有意义。
        "rcomp_default": float(model_cfg.baseline.parameters_si.rcomp),
        "ccomp_default": float(model_cfg.baseline.parameters_si.ccomp),
        "solver": {
            "max_step": float(solver.max_step),
            "rel_tol": float(solver.rel_tol),
            "stop_time": float(solver.stop_time),
        },
        "output_dt_s": OUTPUT_DT_S,
        "signal_names": {
            "vout": signals.vout.logsout_name,
            "iout": signals.iout.logsout_name,
            "iphase": signals.iphase.logsout_name,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=IMPLEMENTED_VARIANTS,
        action="append",
        help="只生成指定变体；可重复。默认生成全部已实现的变体。",
    )
    args = parser.parse_args()
    variants = args.variant or list(IMPLEMENTED_VARIANTS)

    specs = [build_spec(v) for v in variants]

    import matlab.engine  # noqa: PLC0415 -- 见模块 docstring 的边界说明

    print("starting matlab engine...")
    eng = matlab.engine.start_matlab()
    try:
        eng.addpath(str(REPO_ROOT / "matlab"), nargout=0)
        for spec in specs:
            print(f"building {spec['model_variant']} -> {spec['out_path']}")
            eng.build_slx_model(json.dumps(spec), nargout=0)
            out = Path(str(spec["out_path"]))
            if not out.is_file():
                print(f"  FAILED: {out} 未生成", file=sys.stderr)
                return 1
            print(f"  ok, {out.stat().st_size} bytes")
    finally:
        eng.quit()

    print("done. 依赖闭包已变，需要重跑 scripts/dual_model_consistency.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
