"""产出裕量交叉核对记录 `artifacts/margin_cross_check.json`。

用法：

    python scripts/margin_cross_check.py

脚本对若干代表性设计点分别用两种独立方法算稳定性裕量，比较偏差是否落在
`metrics.yaml` 声明的 `cross_check_tolerance` 内，把结果写成记录文件，并打印其
sha256——把该值填回 `metrics.yaml` 的 `margin_extraction.cross_check_record.sha256`
后，`preflight` 的 `check_margin_extraction_ready()` 才会放行。

这是 design.md 所说的「开发期一次性核对」：裕量提取是全项目技术风险最高的一环，
它的正确性不该只靠"跑出来的数看着合理"，而要有一份可追溯、被门禁绑定的核对记录。

两种方法
--------
- 主方法 `linear_analysis_on_averaged`：把平均模型在稳态工作点线性化成状态空间，
  逐频点解 (sI−A)x=B 求频响。
- 核对方法 `manual_bode_reference`：按手写的开环传递函数式直接求值。

两者是同一个数学对象的不同实现路径，因此核对抓的是实现差错（矩阵搭错、多项式
系数写反、单位漏换算），而不是两种建模假设之间的差异。

记录内容必须是确定性的
----------------------
不写时间戳、不写主机名、数值经 `canonical_json` 规范化。否则每次重跑都会得到不同的
sha256，配置里的哈希就得跟着改一次，而一个每次都要更新的门禁等于没有门禁。
重跑本脚本若得到与配置中不同的哈希，含义就明确了：核对结论真的变了（模型或容差
被改动过），需要人来看。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from poweragent.config.hashing import canonical_json  # noqa: E402
from poweragent.config.loader import load_all  # noqa: E402
from poweragent.sim.backends.buck_model import (  # noqa: E402
    OperatingCondition,
    load_buck_spec,
)
from poweragent.sim.backends.margin import (  # noqa: E402
    margins_from_analytic,
    margins_from_state_space,
)

# 核对点：覆盖工程基线、参考网格最优、以及相位裕量明显不足的一角。刻意包含一个
# 不可行点——核对要证明两种方法在整个域上一致，而不只是在好点上一致。
CROSS_CHECK_POINTS = (
    ("baseline", 12000.0, 2.2e-9),
    ("grid_optimum", 18738.0, 8.111e-10),
    ("low_bandwidth", 1000.0, 2.2e-9),
    ("small_ccomp", 18738.0, 1.0e-10),
)


def _format(value: float) -> float:
    """把数值收敛到固定小数位，避免不同平台的浮点末位差异改变 sha256。

    6 位小数远细于核对容差（3° / 1 dB），不影响判定。
    """
    return round(float(value), 6)


def main() -> int:
    bundle = load_all(REPO_ROOT / "configs")
    spec = load_buck_spec(REPO_ROOT / "models" / "buck4ph_averaged.yaml")

    margin_cfg = bundle.metrics.margin_extraction
    tolerance = margin_cfg.cross_check_tolerance

    # 在需要采集裕量的那个场景上核对（require_margin=True 的评价工况）。
    scenario = next(s for s in bundle.task.scenarios if s.require_margin)
    condition = OperatingCondition(
        vin_v=scenario.vin_v,
        temp_c=scenario.temp_c,
        load_start_a=scenario.load_start_a,
        load_end_a=scenario.load_end_a,
        slew_a_per_us=scenario.slew_a_per_us,
    )

    points = []
    worst_phase_delta = 0.0
    worst_gain_delta = 0.0
    all_within = True

    for label, rcomp, ccomp in CROSS_CHECK_POINTS:
        try:
            primary = margins_from_state_space(
                spec, condition, rcomp_ohm=rcomp, ccomp_f=ccomp
            )
            reference = margins_from_analytic(
                spec, condition, rcomp_ohm=rcomp, ccomp_f=ccomp
            )
        except ValueError as exc:
            # 环路不稳定到裕量无从定义。如实记录，不跳过、不编造数值。
            points.append(
                {
                    "label": label,
                    "rcomp_ohm": _format(rcomp),
                    "ccomp_f": ccomp,
                    "status": "margin_undefined",
                    "reason": str(exc),
                }
            )
            continue

        phase_delta = abs(primary.phase_margin_deg - reference.phase_margin_deg)
        gain_delta = abs(primary.gain_margin_db - reference.gain_margin_db)
        within = phase_delta <= tolerance.phase_deg and gain_delta <= tolerance.gain_db

        worst_phase_delta = max(worst_phase_delta, phase_delta)
        worst_gain_delta = max(worst_gain_delta, gain_delta)
        all_within = all_within and within

        points.append(
            {
                "label": label,
                "rcomp_ohm": _format(rcomp),
                "ccomp_f": ccomp,
                "status": "compared",
                "crossover_hz": _format(primary.crossover_hz),
                "phase_crossover_hz": _format(primary.phase_crossover_hz),
                "primary": {
                    "phase_margin_deg": _format(primary.phase_margin_deg),
                    "gain_margin_db": _format(primary.gain_margin_db),
                },
                "reference": {
                    "phase_margin_deg": _format(reference.phase_margin_deg),
                    "gain_margin_db": _format(reference.gain_margin_db),
                },
                "delta": {
                    "phase_deg": _format(phase_delta),
                    "gain_db": _format(gain_delta),
                },
                "within_tolerance": within,
            }
        )

    record = {
        "ok": all_within,
        "primary_method": margin_cfg.primary_method,
        "cross_check_method": margin_cfg.cross_check_method,
        "scenario_id": scenario.scenario_id,
        "tolerance": {
            "phase_deg": _format(tolerance.phase_deg),
            "gain_db": _format(tolerance.gain_db),
        },
        "worst_delta": {
            "phase_deg": _format(worst_phase_delta),
            "gain_db": _format(worst_gain_delta),
        },
        "points": points,
    }

    payload = canonical_json(record).encode("utf-8")
    target = REPO_ROOT / margin_cfg.cross_check_record.path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    digest = hashlib.sha256(payload).hexdigest()

    print(f"写入 {target.relative_to(REPO_ROOT).as_posix()}")
    print(f"核对场景 {scenario.scenario_id}，容差 {tolerance.phase_deg}° / {tolerance.gain_db} dB")
    for point in points:
        if point["status"] == "margin_undefined":
            print(f"  {point['label']:14} 裕量无从定义：{point['reason']}")
            continue
        print(
            f"  {point['label']:14} PM {point['primary']['phase_margin_deg']:8.3f} vs "
            f"{point['reference']['phase_margin_deg']:8.3f}  "
            f"GM {point['primary']['gain_margin_db']:7.3f} vs "
            f"{point['reference']['gain_margin_db']:7.3f}  "
            f"{'一致' if point['within_tolerance'] else '超出容差'}"
        )
    print(f"最差偏差 {record['worst_delta']['phase_deg']}° / {record['worst_delta']['gain_db']} dB")
    print(f"ok = {record['ok']}")
    print()
    print("把下面这行填进 configs/metrics.yaml 的")
    print("margin_extraction.cross_check_record.sha256：")
    print(f"    {digest}")

    return 0 if all_within else 1


if __name__ == "__main__":
    raise SystemExit(main())
