"""产出双模型一致性记录 `artifacts/dual_model_consistency.json`。

用法：

    python scripts/dual_model_consistency.py

在 `model.yaml` 的 `dual_model_consistency.checkpoints` 声明的场景上，用工程基线
参数分别跑平均模型与开关模型，比较稳态输出与负载阶跃下冲的相对偏差是否落在该节
声明的 `steady_tol` / `transient_tol` 内，把结果写成记录文件。

`preflight` 的 `check_dual_model_consistency()` 读这份记录，并要求三件事同时成立：
记录存在、`ok` 为真、记录里绑定的 `model_package_hash` 与当前依赖闭包重算值一致。
第三条意味着**改动任何模型文件都会使这份记录失效**——这正是目的：两个保真度是否
一致的结论只对当时那份模型成立。

为什么必须核对
--------------
分层执行的全部价值建立在一个假设上：筛选层（平均模型）筛掉的候选，在评价层
（开关模型）也确实不好。这个假设不成立时，省下的仿真次数是虚假的，还会误杀
实际可行的候选——而被误杀的候选不会进入评价层得到纠正。

这份核对就是给那个假设立的证据。它也真的抓到过错误：早先平均模型的采样延迟漏了
相数（用 `0.5/fsw` 而非 `0.5/(N·fsw)`），下冲偏差一度达 20%，平均模型系统性偏悲观。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from poweragent.config.hashing import canonical_json  # noqa: E402
from poweragent.config.loader import load_all  # noqa: E402
from poweragent.sim.backends.averaged import simulate_averaged  # noqa: E402
from poweragent.sim.backends.buck_model import (  # noqa: E402
    OperatingCondition,
    load_buck_spec,
)
from poweragent.sim.backends.switching import simulate_switching  # noqa: E402
from poweragent.sim.hashing import (  # noqa: E402
    model_package_hash,
    resolve_dependency_closure,
)

# 稳态窗口取仿真末段，与 eval/metrics.py 的稳态窗口惯例同口径。
STEADY_TAIL_FRACTION = 0.10


def _tail_mean(run) -> float:
    span = run.time_s[-1] - run.time_s[0]
    mask = run.time_s >= run.time_s[-1] - STEADY_TAIL_FRACTION * span
    return float(run.vout_v[mask].mean())


def _round(value: float, digits: int = 9) -> float:
    return round(float(value), digits)


def main() -> int:
    bundle = load_all(REPO_ROOT / "configs")
    model_cfg = bundle.model

    if not model_cfg.averaged_model_required:
        print("averaged_model_required=false，preflight 会豁免该检查，无需核对记录")
        return 0

    model_dump = model_cfg.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=REPO_ROOT)
    current_hash = model_package_hash(closure, model_dump)

    avg_spec = load_buck_spec(REPO_ROOT / model_dump["model_package"]["averaged"]["entry"])
    sw_spec = load_buck_spec(REPO_ROOT / model_dump["model_package"]["switching"]["entry"])

    solver = model_cfg.io_contract.solver
    guard = model_cfg.io_contract.divergence_guard
    vout_target = model_cfg.io_contract.vout_target_v
    baseline = model_cfg.baseline.parameters_si
    rcomp, ccomp = baseline.rcomp, baseline.ccomp

    scenarios_by_id = {s.scenario_id: s for s in bundle.task.scenarios}

    checkpoints = []
    all_within = True

    for checkpoint in model_cfg.dual_model_consistency.checkpoints:
        row = scenarios_by_id[checkpoint.scenario_id]
        condition = OperatingCondition(
            vin_v=row.vin_v,
            temp_c=row.temp_c,
            load_start_a=row.load_start_a,
            load_end_a=row.load_end_a,
            slew_a_per_us=row.slew_a_per_us,
        )

        averaged = simulate_averaged(
            avg_spec, condition, rcomp_ohm=rcomp, ccomp_f=ccomp,
            stop_time_s=solver.stop_time, rel_tol=solver.rel_tol,
            guard_vout_abs_max=guard.vout_abs_max,
            guard_iphase_abs_max=guard.iphase_abs_max,
        )
        switching = simulate_switching(
            sw_spec, condition, rcomp_ohm=rcomp, ccomp_f=ccomp,
            stop_time_s=solver.stop_time, time_step_s=solver.max_step,
            guard_vout_abs_max=guard.vout_abs_max,
            guard_iphase_abs_max=guard.iphase_abs_max,
        )

        if averaged.status != "ok" or switching.status != "ok":
            all_within = False
            checkpoints.append(
                {
                    "scenario_id": checkpoint.scenario_id,
                    "status": "simulation_failed",
                    "averaged_status": averaged.status,
                    "switching_status": switching.status,
                }
            )
            continue

        # 稳态：开关模型含纹波，取窗口均值与平均模型比较。
        avg_steady, sw_steady = _tail_mean(averaged), _tail_mean(switching)
        steady_dev = abs(sw_steady - avg_steady) / abs(avg_steady)
        steady_ok = steady_dev <= checkpoint.steady_tol

        # 瞬态：负载阶跃下冲深度。
        avg_undershoot = vout_target - float(np.min(averaged.vout_v))
        sw_undershoot = vout_target - float(np.min(switching.vout_v))
        transient_dev = abs(sw_undershoot - avg_undershoot) / abs(avg_undershoot)
        transient_ok = transient_dev <= checkpoint.transient_tol

        all_within = all_within and steady_ok and transient_ok
        checkpoints.append(
            {
                "scenario_id": checkpoint.scenario_id,
                "status": "compared",
                "parameters_si": {"rcomp": _round(rcomp), "ccomp": ccomp},
                "steady": {
                    "averaged_v": _round(avg_steady),
                    "switching_v": _round(sw_steady),
                    "relative_deviation": _round(steady_dev),
                    "tolerance": _round(checkpoint.steady_tol),
                    "within_tolerance": steady_ok,
                },
                "transient": {
                    "averaged_undershoot_v": _round(avg_undershoot),
                    "switching_undershoot_v": _round(sw_undershoot),
                    "relative_deviation": _round(transient_dev),
                    "tolerance": _round(checkpoint.transient_tol),
                    "within_tolerance": transient_ok,
                },
            }
        )

    record = {
        "ok": all_within,
        # preflight 绑定这个哈希：改动任何模型文件都会使本记录失效。
        "model_package_hash": current_hash,
        "checkpoints": checkpoints,
    }

    target = REPO_ROOT / "artifacts" / "dual_model_consistency.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json(record).encode("utf-8"))

    print(f"写入 {target.relative_to(REPO_ROOT).as_posix()}")
    print(f"model_package_hash = {current_hash}")
    for point in checkpoints:
        if point["status"] != "compared":
            print(f"  {point['scenario_id']}: 仿真失败 "
                  f"averaged={point['averaged_status']} switching={point['switching_status']}")
            continue
        steady, transient = point["steady"], point["transient"]
        print(
            f"  {point['scenario_id']}: "
            f"稳态 {steady['averaged_v']:.6f} vs {steady['switching_v']:.6f} V "
            f"({steady['relative_deviation'] * 100:.4f}% / 容差 {steady['tolerance'] * 100:.2f}%) "
            f"{'ok' if steady['within_tolerance'] else 'FAIL'}"
        )
        print(
            f"  {'':{len(point['scenario_id'])}}  "
            f"下冲 {transient['averaged_undershoot_v'] * 1e3:.2f} vs "
            f"{transient['switching_undershoot_v'] * 1e3:.2f} mV "
            f"({transient['relative_deviation'] * 100:.2f}% / 容差 "
            f"{transient['tolerance'] * 100:.2f}%) "
            f"{'ok' if transient['within_tolerance'] else 'FAIL'}"
        )
    print(f"ok = {record['ok']}")

    return 0 if all_within else 1


if __name__ == "__main__":
    raise SystemExit(main())
