"""四个配置文件的契约测试。

这些断言守护的是「配置本身是否可用」，而不是某个函数的行为。放在测试里而不是
一次性脚本里，是因为配置会随物理规格调整而变动，每次变动都应重新过一遍：
占位符是否填全、跨文件引用是否仍然对得上、模型依赖闭包里声明的路径是否还存在。

其中 `test_device_limits_match_model_files` 覆盖的是 `config.loader` **管不到**的
一致性：`constraints.yaml` 的 `device_limits` 与 `models/buck4ph_*.yaml` 的
`power_stage` 描述的是同一批器件，但它们是两份独立文件，loader 无法交叉校验。
这类"人工维护的一致性"最容易在改参数时漏改一处，因此值得一条测试。
"""

from pathlib import Path

import pytest
import yaml

from poweragent.config.loader import load_all
from poweragent.config.ticks import expand_all_ticks
from poweragent.sim.hashing import model_package_hash, resolve_dependency_closure

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# 加载与校验
# --------------------------------------------------------------------------


def test_load_all_accepts_filled_configs(config_dir: Path) -> None:
    """四个配置文件通过占位符预检、强类型校验与跨文件一致性校验。

    `load_all()` 只要发现一条问题就抛 `ConfigLoadError` 且不返回任何模型，
    因此本测试能走到断言即意味着全部校验通过。
    """
    bundle = load_all(config_dir)

    assert bundle.task.task_id == "comp_tuning_v1"
    assert bundle.task.simulation_only is True, "本阶段为仿真轨，结论不足以支撑打板"
    assert [s.scenario_id for s in bundle.task.scenarios] == [
        "scr_nom",
        "eval_vin_min_step_max",
    ]


def test_hard_constraints_are_exactly_five(config_dir: Path) -> None:
    """硬约束恰五条，不接受第六条（requirements.md R9 AC1 的数量约束）。

    第五条 `gain_margin_min` 由 AC1 的变更记录引入：`phase_margin_min` 只是
    45°/6 dB 配对判据的一半，缺另一半时最优化会走到增益裕量 1.9 dB 的角点。
    """
    bundle = load_all(config_dir)

    assert set(type(bundle.constraints.hard_constraints).model_fields) == {
        "vout_min",
        "vout_max",
        "peak_current_max",
        "phase_margin_min",
        "gain_margin_min",
    }


def test_phase_margin_constraint_applies_only_to_evaluation_tier(config_dir: Path) -> None:
    """相位裕量只在 evaluation 层判定，且只有 evaluation 场景采集裕量。

    这两件事必须一致：若某层要判 `phase_margin` 却没有任何 `require_margin=true`
    的场景，该约束永远拿不到观测值。
    """
    bundle = load_all(config_dir)

    assert bundle.constraints.hard_constraints.phase_margin_min.applies_to_tier == [
        "evaluation"
    ]

    margin_tiers = {s.tier for s in bundle.task.scenarios if s.require_margin}
    assert margin_tiers == {"evaluation"}


def test_divergence_guard_is_looser_than_hard_constraints(config_dir: Path) -> None:
    """发散判据安全界须宽于评价阈值。

    两者刻意不合并：`divergence_guard` 是"仿真已经跑飞了"的判据，
    `hard_constraints` 是"这个设计不可接受"的判据。前者必须更宽，
    否则一个仅仅不合格的候选会被误判为数值发散。
    """
    bundle = load_all(config_dir)

    guard = bundle.model.io_contract.divergence_guard
    hard = bundle.constraints.hard_constraints

    assert guard.vout_abs_max >= hard.vout_max.value
    assert guard.iphase_abs_max >= hard.peak_current_max.value


def test_baseline_parameters_lie_inside_design_domain(config_dir: Path) -> None:
    """工程基线须落在设计空间闭区间内（不要求命中档位）。

    基线在域外会让 M1 出口门禁 `check_baseline_gate()` 去评估一个搜索器
    永远不会提出的点，前后对比因此失去意义。
    """
    bundle = load_all(config_dir)

    baseline = bundle.model.baseline.parameters_si
    variables = bundle.constraints.design_space.variables

    for name in ("rcomp", "ccomp"):
        low, high = getattr(variables, name).domain
        value = getattr(baseline, name)
        assert low <= value <= high, f"baseline.{name}={value} 落在 [{low}, {high}] 之外"


# --------------------------------------------------------------------------
# 档位展开
# --------------------------------------------------------------------------


def test_ticks_expand_to_twelve_geometric_steps(config_dir: Path) -> None:
    """两个设计变量各展开 12 个等比档位，合成 144 点参考网格。"""
    bundle = load_all(config_dir)
    ticks = expand_all_ticks(bundle.constraints)

    assert set(ticks) == {"rcomp", "ccomp"}
    for name, values in ticks.items():
        assert len(values) == 12, f"{name} 档位数应为 12"

        numeric = [float(v) for v in values]
        low, high = getattr(bundle.constraints.design_space.variables, name).domain
        assert numeric[0] == pytest.approx(low)
        assert numeric[-1] == pytest.approx(high)

        ratios = [b / a for a, b in zip(numeric, numeric[1:])]
        assert all(r == pytest.approx(ratios[0], rel=1e-9) for r in ratios), (
            f"{name} 档位非等比"
        )


def test_tick_match_tolerance_is_strictly_below_ratio_bound(config_dir: Path) -> None:
    """档位命中容差须严格小于 0.01 × (档位比 − 1)。

    否则相邻两档的容差区间会开始重叠，"命中哪一档"变得不确定，
    而档位是候选去重与新颖度计算的基础。
    """
    bundle = load_all(config_dir)
    ticks = expand_all_ticks(bundle.constraints)

    tol = bundle.constraints.design_space.tick_match_rel_tol
    for name, values in ticks.items():
        numeric = [float(v) for v in values]
        ratio = numeric[1] / numeric[0]
        assert tol < 0.01 * (ratio - 1.0), f"{name}: tick_match_rel_tol 过大"


# --------------------------------------------------------------------------
# 模型依赖闭包
# --------------------------------------------------------------------------


def test_dependency_closure_paths_all_exist(repo_root: Path) -> None:
    """`model_package` 中声明的每一条路径都真实存在。

    闭包里出现一个不存在的文件，`model_package_hash()` 会在读取时抛
    `FileNotFoundError`，且这个错误会发生在 preflight 阶段——比在仿真
    跑了一半时才发现要好，但更好的是在这里就发现。
    """
    raw_model = yaml.safe_load((repo_root / "configs/model.yaml").read_text("utf-8"))
    closure = resolve_dependency_closure(raw_model, base_dir=repo_root)

    missing = [p for p in closure if not p.is_file()]
    assert not missing, f"闭包中缺失 {len(missing)} 条路径: {[str(p) for p in missing]}"


def test_averaged_variant_is_included_in_closure(repo_root: Path) -> None:
    """`averaged_model_required=true` 时平均模型必须进入合并闭包。

    这是取 true 的实际理由：只有进了闭包，改动平均模型才会改变
    `model_package_hash`，旧结论才会按设计失效。
    """
    raw_model = yaml.safe_load((repo_root / "configs/model.yaml").read_text("utf-8"))
    assert raw_model["averaged_model_required"] is True

    closure = resolve_dependency_closure(raw_model, base_dir=repo_root)
    names = {p.name for p in closure}

    assert "buck4ph_switching.yaml" in names
    assert "buck4ph_averaged.yaml" in names


def test_model_package_hash_is_deterministic(repo_root: Path) -> None:
    """同一份闭包与配置两次求值得到相同摘要。"""
    raw_model = yaml.safe_load((repo_root / "configs/model.yaml").read_text("utf-8"))
    closure = resolve_dependency_closure(raw_model, base_dir=repo_root)

    first = model_package_hash(closure, raw_model)
    second = model_package_hash(closure, raw_model)

    assert first == second
    assert len(first) == 64, "摘要不得截断（design.md §5.3.1）"


# --------------------------------------------------------------------------
# loader 覆盖不到的跨文件一致性
# --------------------------------------------------------------------------


def test_device_limits_match_model_files(repo_root: Path, config_dir: Path) -> None:
    """`constraints.yaml` 的 `device_limits` 与两个模型文件的 `power_stage` 一致。

    三处描述的是同一批器件。`device_limits` 是关键器件参数的唯一权威来源
    （R22.1/R22.2），模型文件里的取值必须与它逐项相同——不一致意味着
    "约束判定用的器件"和"实际被仿真的器件"不是同一个，结论无效。
    """
    bundle = load_all(config_dir)
    limits = bundle.constraints.design_space.device_limits

    expected = {
        "cout_f": limits.cout_c.value,
        "cout_esr_ohm": limits.cout_esr.value,
        "l_per_phase_h": limits.l_per_phase.value,
    }

    for model_file in ("buck4ph_averaged.yaml", "buck4ph_switching.yaml"):
        stage = yaml.safe_load(
            (repo_root / "models" / model_file).read_text("utf-8")
        )["power_stage"]
        for key, want in expected.items():
            assert stage[key] == pytest.approx(want), (
                f"{model_file}.power_stage.{key}={stage[key]} 与 device_limits 的 {want} 不一致"
            )


def test_model_files_agree_on_shared_physics(repo_root: Path) -> None:
    """平均模型与开关模型描述同一个电路：共有的功率级与工作点必须逐项相同。

    两者是同一电路的两种保真度。若功率级不同，screening 层筛除的候选
    在 evaluation 层未必真的差，分层执行的成本节省就是虚假的。
    """
    averaged = yaml.safe_load(
        (repo_root / "models/buck4ph_averaged.yaml").read_text("utf-8")
    )
    switching = yaml.safe_load(
        (repo_root / "models/buck4ph_switching.yaml").read_text("utf-8")
    )

    assert averaged["topology"] == switching["topology"]
    assert averaged["power_stage"] == switching["power_stage"]
    assert averaged["operating_point"] == switching["operating_point"]
    assert averaged["control"] == switching["control"]


def test_output_signal_names_match_metric_signals(config_dir: Path) -> None:
    """`metrics.yaml` 里引用的信号名都在 `model.yaml` 的输出信号契约中。

    指标引用一个模型不产出的信号，会在评价阶段落 `signal_missing`——
    那是运行时才暴露的配置错误，本可以在加载期就发现。
    """
    bundle = load_all(config_dir)

    declared = {
        bundle.model.io_contract.output_signals.vout.logsout_name,
        bundle.model.io_contract.output_signals.iout.logsout_name,
        bundle.model.io_contract.output_signals.iphase.logsout_name,
    }

    referenced = {
        spec.signal
        for spec in (
            bundle.metrics.metrics.settling_time,
            bundle.metrics.metrics.output_ripple,
            bundle.metrics.metrics.overshoot,
            bundle.metrics.metrics.undershoot,
            bundle.metrics.metrics.phase_peak_current,
        )
    }
    # `constraint_observables` 的两项在 YAML 里键名为 `obs.vout_min` / `obs.vout_max`，
    # schema 中以 alias 映射到不含点号的字段名。
    observables = bundle.metrics.constraint_observables
    referenced |= {observables.vout_min.signal, observables.vout_max.signal}

    assert referenced <= declared, f"未声明的信号: {referenced - declared}"
