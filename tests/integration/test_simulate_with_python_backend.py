"""用 Python 后端驱动真实的 `sim.simulate()`，验证接线而非各部件本身。

前面的测试各自验证了求解器、指标提取、裕量提取。这里测的是把它们连起来的那条路：
`simulate()` 的模型不变性检查、参数注入、产物归档、裕量的额外调用与预算计入，
以及 `eval` 层能否消费归档后的产物。

`simulate()` 一行都没为 Python 后端改动——它与后端之间只有 `session.call()` 一个
交互面，而 `PythonSession` 与 `MatlabSession` 同形。若哪天这条测试因为签名不匹配
而失败，说明那个边界被破坏了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from poweragent.config.loader import load_all
from poweragent.eval.constraints import judge
from poweragent.eval.margin import extract_margin
from poweragent.eval.metrics import compute_metrics
from poweragent.sim.backends.session import PythonSession
from poweragent.sim.hashing import (
    fast_fingerprint,
    model_package_hash,
    resolve_dependency_closure,
)
from poweragent.sim.simulate import simulate
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, ScenarioSpec

pytestmark = pytest.mark.integration

BASELINE_PARAMS = {"rcomp": 12000.0, "ccomp": 2.2e-9}
TASK_ID = "integration_task"


@pytest.fixture(scope="module")
def bundle(config_dir: Path):
    return load_all(config_dir)


@pytest.fixture(scope="module")
def frozen_hashes(bundle, repo_root: Path):
    """任务开始时冻结的闭包指纹与模型包哈希，由 `simulate()` 用于不变性检查。"""
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    return fast_fingerprint(closure), model_package_hash(closure, model_dump)


def _scenario(bundle, *, require_margin: bool) -> ScenarioSpec:
    """按 task.yaml 的场景行构造 `ScenarioSpec`。

    取 `require_margin` 匹配的那一行，因此筛选场景用平均模型、评价场景用开关模型
    并额外采集裕量——两条路径都要走到。
    """
    row = next(s for s in bundle.task.scenarios if s.require_margin is require_margin)
    return ScenarioSpec(
        scenario_id=row.scenario_id,
        tier=row.tier,
        model_variant=row.model_variant,
        require_margin=row.require_margin,
        vin_v=row.vin_v,
        temp_c=row.temp_c,
        load_start_a=row.load_start_a,
        load_end_a=row.load_end_a,
        slew_a_per_us=row.slew_a_per_us,
        spec_version="integration",
    )


def _run_simulate(bundle, repo_root, frozen, artifacts_root: Path, scenario, run_id: str):
    fingerprint, package_hash = frozen
    artifacts = ArtifactStore(base_dir=artifacts_root)

    with PythonSession(base_dir=repo_root) as session:
        return simulate(
            Candidate(candidate_id="cand_baseline", parameters_si=BASELINE_PARAMS),
            scenario,
            model_cfg=bundle.model,
            metrics_cfg=bundle.metrics,
            session=session,
            run_id=run_id,
            artifacts=artifacts,
            task_id=TASK_ID,
            frozen_fingerprint=fingerprint,
            frozen_model_package_hash=package_hash,
            base_dir=repo_root,
        )


# --------------------------------------------------------------------------
# 筛选场景：平均模型，不采裕量
# --------------------------------------------------------------------------


def test_screening_scenario_produces_archived_waveform(
    bundle, repo_root: Path, frozen_hashes, tmp_path: Path
) -> None:
    """筛选场景跑通并归档波形，预算只计一次仿真调用。

    `engine_starts` 在 Python 后端下的口径是「昂贵仿真调用次数」而非真实的 Simulink
    启动次数，这样 `max_engine_starts` 的预算配置不必按后端分裂。
    """
    scenario = _scenario(bundle, require_margin=False)
    result = _run_simulate(
        bundle, repo_root, frozen_hashes, tmp_path, scenario, "run_screening"
    )

    assert result.status == "ok", result
    assert result.waveform_ref is not None
    assert Path(result.waveform_ref).is_file()
    assert result.observable_ref is None, "筛选场景不应采集裕量"
    assert result.engine_starts == 1
    assert result.elapsed_ms >= 0


def test_metrics_can_be_computed_from_the_archived_waveform(
    bundle, repo_root: Path, frozen_hashes, tmp_path: Path
) -> None:
    """归档后的波形能被 `eval/metrics.py` 直接消费，五项指标全部有效。

    这打通了后端产物格式与指标层加载器的契约。归档会把文件移到 `ArtifactStore`
    的最终路径，因此这里验证的是**归档后**的产物仍可读——而非后端刚写出的临时文件。
    """
    scenario = _scenario(bundle, require_margin=False)
    result = _run_simulate(
        bundle, repo_root, frozen_hashes, tmp_path, scenario, "run_metrics"
    )

    metrics = {
        m.metric_id: m
        for m in compute_metrics(
            result.waveform_ref,
            scenario,
            bundle.metrics,
            run_id="run_metrics",
            guard=bundle.model.io_contract.divergence_guard,
            vout_target_v=bundle.model.io_contract.vout_target_v,
        )
    }

    for metric_id in (
        "output_ripple", "overshoot", "undershoot", "settling_time", "phase_peak_current"
    ):
        assert metrics[metric_id].valid, (
            f"{metric_id} 落 invalid: {metrics[metric_id].invalid_reason}"
        )

    # 工程基线在筛选工况下的量级应与直接跑求解器时一致（约 35 mV 下冲、40 µs 级恢复）。
    assert 0.02 < metrics["undershoot"].value < 0.06
    assert 10.0 < metrics["settling_time"].value < 120.0
    assert 30.0 < metrics["phase_peak_current"].value < 55.0

    # 平均模型无开关纹波，稳态窗口的峰峰值应接近零。
    assert metrics["output_ripple"].value < 1e-4


# --------------------------------------------------------------------------
# 评价场景：开关模型 + 裕量
# --------------------------------------------------------------------------


def test_evaluation_scenario_collects_margin_and_counts_extra_call(
    bundle, repo_root: Path, frozen_hashes, tmp_path: Path
) -> None:
    """评价场景额外发起一次裕量分析，产物归档且预算计入两次调用。

    `metrics.yaml` 声明 `extra_engine_starts_per_candidate = 1`，`simulate()` 会在
    累加后校验该后置条件；这里确认两侧口径一致。
    """
    scenario = _scenario(bundle, require_margin=True)
    result = _run_simulate(
        bundle, repo_root, frozen_hashes, tmp_path, scenario, "run_evaluation"
    )

    assert result.status == "ok", result
    assert result.waveform_ref is not None and Path(result.waveform_ref).is_file()
    assert result.observable_ref is not None, "评价场景应采集裕量"
    assert Path(result.observable_ref).is_file()
    assert result.engine_starts == 2, "一次仿真 + 一次裕量分析"


def test_margin_and_constraints_from_the_archived_artefacts(
    bundle, repo_root: Path, frozen_hashes, tmp_path: Path
) -> None:
    """从归档产物一路走到硬约束判定：指标 → 裕量 → 可行性。

    这是判分链的完整通路。工程基线在最恶劣工况下应判为可行——若它不可行，M1 出口的
    基线门禁会拿一个不可行的参考点去做前后对比，改进量也就无从谈起。
    """
    scenario = _scenario(bundle, require_margin=True)
    result = _run_simulate(
        bundle, repo_root, frozen_hashes, tmp_path, scenario, "run_judge"
    )

    metrics = list(
        compute_metrics(
            result.waveform_ref,
            scenario,
            bundle.metrics,
            run_id="run_judge",
            guard=bundle.model.io_contract.divergence_guard,
            vout_target_v=bundle.model.io_contract.vout_target_v,
        )
    )
    phase_margin, gain_margin = extract_margin(
        result.observable_ref, bundle.metrics, run_id="run_judge"
    )
    assert phase_margin.valid and gain_margin.valid
    assert phase_margin.value >= bundle.constraints.hard_constraints.phase_margin_min.value

    verdict = judge(
        metrics + [phase_margin, gain_margin],
        scenario,
        bundle.constraints,
        candidate_id="cand_baseline",
        run_id="run_judge",
    )

    assert verdict.feasible, f"工程基线应可行，违反项: {verdict.violations}"


# --------------------------------------------------------------------------
# 会话契约
# --------------------------------------------------------------------------


def test_session_rejects_calls_outside_the_whitelist(repo_root: Path) -> None:
    """白名单在 Python 后端同样生效，且拒绝发生在触碰仿真逻辑之前。

    白名单是仿真域的安全边界（不接受任意命令），属于 `sim` 层契约而非 MATLAB 的
    实现细节，因此两个后端共用同一份定义，不各留一份可能漂移的副本。
    """
    from poweragent.sim.engine import MatlabCallNotAllowedError

    with PythonSession(base_dir=repo_root) as session:
        with pytest.raises(MatlabCallNotAllowedError):
            session.call("os.system", "echo hi")
        with pytest.raises(MatlabCallNotAllowedError):
            session.call("pa.simulate_once; disp('x')", "{}", "{}", "{}")

        # 会话在被拒绝的调用之后仍然可用。
        info = session.call("pa.inspect_model", '{"model_path": "models/buck4ph_averaged.yaml"}')
        assert info["injectable_params"] == ["rcomp", "ccomp"]


def test_inspect_model_reports_backend_capability(bundle, repo_root: Path) -> None:
    """`pa.inspect_model` 回报后端实际支持的注入参数与输出信号。

    它回报的是**能力**而不是把配置原样读回——后者无法发现契约与实现不符。这里
    顺带确认配置声明的注入参数集合与后端支持的集合一致。
    """
    import json

    model_dump = bundle.model.model_dump(mode="json")
    payload = json.dumps({**model_dump, "model_path": model_dump["model_package"]["averaged"]["entry"]})

    with PythonSession(base_dir=repo_root) as session:
        info = session.call("pa.inspect_model", payload)

    declared = set(model_dump["io_contract"]["injectable_params"])
    assert set(info["injectable_params"]) == declared
    assert info["model_variant"] == "averaged"
    assert info["n_phase"] == 4


def test_session_cleans_up_its_working_directory(repo_root: Path) -> None:
    """会话退出后临时工作目录被删除。

    归档由 `ArtifactStore` 负责，会话只需把文件放在调用方能读到的地方；退出后
    不留残留，避免长时间运行的搜索在临时目录里堆积上百份波形。
    """
    with PythonSession(base_dir=repo_root) as session:
        workdir = session._work_path  # noqa: SLF001 -- 测试有意检查内部状态
        assert workdir.is_dir()

    assert not workdir.exists()
