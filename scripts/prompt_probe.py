"""用真实的搜索状态构建 prompt 并调一次 LLM，测量压缩比与候选质量。

用法：

    python scripts/prompt_probe.py

需要 `POWERAGENT_LLM_API_KEY`（可放在 `.env`）。脚本做三件事：

1. 从真实配置构造一个搜索状态（合法域、约束、一个已评估的工程基线点）
2. 构建 prompt，报告"原始波形采样点数 → 压缩后 prompt token 数"这个压缩比
3. 调一次模型，报告格式遵从度、token 消耗与候选的分布

第 2 项是要写进报告的指标之一：上下文按信息压缩而不是按长度截断，压缩比是它的
量化证据。第 3 项用来判断 prompt 是否真的把物理结构传达出去了——候选若均匀撒在
整个域上，说明结构信息没起作用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from poweragent.agent.client import DeepSeekClient, request_proposal  # noqa: E402
from poweragent.agent.prompt import (  # noqa: E402
    SYSTEM_PROMPT,
    build_prompt,
    compress_waveform,
)
from poweragent.agent.state import (  # noqa: E402
    ConstraintSpec,
    DomainSpec,
    SearchState,
    TestedPoint,
)
from poweragent.config.env import load_local_env  # noqa: E402
from poweragent.config.loader import load_all  # noqa: E402
from poweragent.config.ticks import expand_all_ticks  # noqa: E402
from poweragent.controller.stop import BestRecord  # noqa: E402
from poweragent.sim.backends.averaged import simulate_averaged  # noqa: E402
from poweragent.sim.backends.buck_model import (  # noqa: E402
    OperatingCondition,
    load_buck_spec,
)
from poweragent.store.repo import MetricResult  # noqa: E402


def _domain_from_config(bundle) -> dict[str, DomainSpec]:
    ticks = expand_all_ticks(bundle.constraints)
    variables = bundle.constraints.design_space.variables
    out: dict[str, DomainSpec] = {}
    for name in ("rcomp", "ccomp"):
        spec = getattr(variables, name)
        out[name] = DomainSpec(
            unit=spec.unit,
            low=spec.domain[0],
            high=spec.domain[1],
            scale=spec.scale,
            ticks=ticks[name],
        )
    return out


def _constraints_from_config(bundle) -> dict[str, ConstraintSpec]:
    hard = bundle.constraints.hard_constraints
    units = {
        "vout_min": "V",
        "vout_max": "V",
        "peak_current_max": "A",
        "phase_margin_min": "deg",
    }
    return {
        name: ConstraintSpec(
            value=getattr(hard, name).value,
            sense=getattr(hard, name).sense,
            unit=units[name],
        )
        for name in units
    }


def main() -> int:
    load_local_env(REPO_ROOT / ".env")
    bundle = load_all(REPO_ROOT / "configs")

    # 跑一次真实仿真拿到波形与指标，用于测量压缩比。
    spec = load_buck_spec(REPO_ROOT / "models" / "buck4ph_averaged.yaml")
    scenario = next(s for s in bundle.task.scenarios if s.require_margin)
    condition = OperatingCondition(
        vin_v=scenario.vin_v,
        temp_c=scenario.temp_c,
        load_start_a=scenario.load_start_a,
        load_end_a=scenario.load_end_a,
        slew_a_per_us=scenario.slew_a_per_us,
    )
    baseline = bundle.model.baseline.parameters_si
    solver = bundle.model.io_contract.solver
    guard = bundle.model.io_contract.divergence_guard

    run = simulate_averaged(
        spec,
        condition,
        rcomp_ohm=baseline.rcomp,
        ccomp_f=baseline.ccomp,
        stop_time_s=solver.stop_time,
        rel_tol=solver.rel_tol,
        guard_vout_abs_max=guard.vout_abs_max,
        guard_iphase_abs_max=guard.iphase_abs_max,
    )

    target_v = bundle.model.io_contract.vout_target_v
    undershoot = target_v - float(run.vout_v.min())
    outside = np.where(np.abs(run.vout_v - target_v) > 0.01 * target_v)[0]
    settling_us = (
        0.0 if outside.size == 0 else (run.time_s[outside[-1]] - run.step_trigger_s) * 1e6
    )

    metrics = [
        MetricResult(run_id="probe", metric_id="undershoot", value=undershoot, valid=True),
        MetricResult(run_id="probe", metric_id="overshoot", value=0.0, valid=True),
        MetricResult(
            run_id="probe", metric_id="settling_time", value=settling_us, valid=True
        ),
        MetricResult(run_id="probe", metric_id="output_ripple", value=0.0, valid=True),
    ]
    features = compress_waveform(
        metrics,
        time_s=run.time_s,
        vout_v=run.vout_v,
        target_v=target_v,
        step_trigger_s=run.step_trigger_s,
        diverged=run.status == "diverged",
    )

    state = SearchState(
        legal_domain=_domain_from_config(bundle),
        hard_constraints=_constraints_from_config(bundle),
        current_best=BestRecord(candidate_id="baseline", value=settling_us),
        tested_candidates=(
            TestedPoint(
                candidate_id="baseline",
                parameters_si={"rcomp": baseline.rcomp, "ccomp": baseline.ccomp},
                feasible=True,
                objective_value=settling_us,
                features=features,
            ),
        ),
        failed_regions=(),
        remaining_budget=bundle.task.budget.max_engine_starts - 3,
    )

    user_prompt, context_hash = build_prompt(state)

    # 压缩比：原始波形的采样点总数 vs prompt 的字符数。
    raw_samples = (
        run.time_s.size  # 时间轴
        + run.vout_v.size
        + run.iout_a.size
        + run.iphase_a.size  # 相电流是二维
    )
    print("=== 上下文压缩 ===")
    print(f"单次仿真原始采样点：{raw_samples:,} 个数值")
    print(f"  时间轴 {run.time_s.size:,} + Vout {run.vout_v.size:,} + "
          f"Iout {run.iout_a.size:,} + Iphase {run.iphase_a.size:,}（{run.iphase_a.shape}）")
    print(f"压缩后特征：6 个数值")
    print(f"prompt 长度：{len(user_prompt):,} 字符")
    print(f"context_hash：{context_hash}")
    print()
    # 本脚本的输出刻意只用 ASCII 符号（us 而非 µs）：Windows 控制台默认 GBK，
    # 打不出 µ。把标准输出切成 UTF-8 会让 PowerShell 管道按 GBK 解码而整体乱码，
    # 迁就终端比迁就符号省事，而 prompt 内部仍可自由使用 µs。
    print("=== 波形特征 ===")
    print(f"下冲 {features.undershoot_v * 1e3:.2f} mV，恢复 {features.settling_time_us:.1f} us，"
          f"穿越 {features.oscillation_count} 次，发散 {features.diverged}")
    print()

    client = DeepSeekClient()
    print(f"=== 调用 {client.model_id} ===")
    result = request_proposal(
        client,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_repair_rounds=bundle.task.budget.max_llm_repair_rounds,
        max_network_retries=bundle.task.llm.max_network_retries,
    )

    print(f"outcome = {result.outcome}，修复轮 = {result.repair_rounds_used}")
    for attempt in result.attempts:
        print(
            f"  attempt {attempt.index}: prompt {attempt.prompt_tokens} token，"
            f"completion {attempt.completion_tokens} token，"
            f"违规 {len(attempt.violations)} 条"
        )
        for violation in attempt.violations:
            print(f"    - {violation.path}: {violation.problem}")

    if result.payload is None:
        print("未取得合格输出")
        return 1

    payload = result.payload
    print()
    print(f"瓶颈判断：{payload.target_bottleneck}")
    print(f"搜索假设：{payload.search_hypothesis}")
    print(f"预期代价：{payload.expected_tradeoff}")
    print(f"建议停止：{payload.stop_recommendation}")
    print()
    print("候选（对照：真实可行区间 Rcomp 约 5.3k~65.8k，最优约 18.7k~43.3k）：")
    rcomps = []
    for i, params in enumerate(payload.candidate_parameters()):
        rcomps.append(params["rcomp"])
        print(f"  {i}: rcomp={params['rcomp']:>10.4g}  ccomp={params['ccomp']:.4g}")

    print()
    print(f"候选 Rcomp 范围：{min(rcomps):.4g} ~ {max(rcomps):.4g}")
    in_optimum = sum(1 for r in rcomps if 18000.0 <= r <= 44000.0)
    print(f"落在最优区间内：{in_optimum}/{len(rcomps)}")
    print(f"本次总 token：{result.total_tokens}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
