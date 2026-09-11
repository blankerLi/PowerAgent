"""MATLAB/Simulink 后端的条件测试层（`pytest -m matlab`，默认与 CI 中跳过）。

这一层测的不是电路，而是**跨语言边界的契约**：Python 侧对 `matlab/+pa/*.m` 返回值
形状的假设、MATLAB 侧落盘产物能否被 `eval` 层直接消费、以及两个后端在同一输入上
是否给出同一个电路的结果。

## 为什么它必须是条件层而不是默认层

需要 MATLAB + Simulink + Simulink Control Design 许可证与 `models/*.slx`，整组实测
约 72 s（engine 启动 20 s + 开关模型首次仿真 29 s，含 Simulink 首次编译 + 两次模型
生成；昂贵的仿真由 module 级 fixture 跑一次后多个测试共用）。`pyproject.toml` 的
`addopts`
因此把 `matlab` 与 `live_llm` 一并排除在默认路径外，跑它要显式选择：

    pytest -m matlab

**同时**保留下面 fixture 里的逐项环境检查并在不满足时 skip。两层都要：`-m matlab`
表达"我想跑这一层"，fixture 表达"这台机器能不能跑"，而且 skip 原因要具体——
"MATLAB 测试被跳过了"本身不是信息，"因为 slx_entry 没登记"才是。

## 这一层与 Python 后端集成测试的分工

`tests/integration/test_simulate_with_python_backend.py` 已经覆盖了 `simulate()`
的模型不变性检查、产物归档、预算计入等**后端无关**的逻辑。本文件不重复那些，
只测「换成 MATLAB 后端之后还成立吗」以及「两个后端是否一致」。

## 双后端一致性容差的构成（不是"希望做到多好"，是实测值加余量）

实测偏差（评价场景 eval_vin_min_step_max，工程基线候选）：

    settling_time        51.2  vs 51.2  us   0.00%
    phase_peak_current   44.6719 vs 44.6702 A  0.00%
    undershoot           0.0392342 vs 0.039032 V  0.52%
    obs.vout_min         0.760766 vs 0.760968 V  0.03%
    output_ripple        0.00119866 vs 0.0012931 V  7.88%
    overshoot            0.00101956 vs 0.00108183 V  6.11%

分两档不是为了让测试通过，而是因为这两组量的**误差机制不同**：

- 瞬态与峰值类（settling_time / undershoot / phase_peak_current / obs.*）由功率级
  与环路的大信号行为决定，两个后端解的是同一组方程、用同一个固定步长（20 ns
  显式欧拉 vs Simulink ode1），偏差只来自浮点累加顺序，实测 ≤0.52%。容差取 2%。
- 纹波尺度类（output_ripple / overshoot）是 mV 级量，叠在峰峰 13.7 mV 的开关纹波
  之上。输出记录按 200 ns 抽取（纹波基频 4×fsw = 2 MHz，周期 500 ns），每个纹波
  周期只有 2.5 个采样点，读到的峰值取决于采样落在三角波的哪个相位；两个后端的
  抽取起点差一步（20 ns）就足以改变读数。稳态纹波峰峰值本身两侧只差 0.12%，
  说明电路一致、差异全在采样相位。容差取 15%。

`overshoot` 归入纹波档而不是瞬态档，是因为它在本工况下的绝对值（约 1 mV）远小于
纹波峰峰值——它测的实际上是"恢复过程中某个纹波峰恰好比 vout_target 高多少"，
对采样相位的敏感度与 `output_ripple` 同级，与 `undershoot`（39 mV，远大于纹波）
不同。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from poweragent.config.loader import load_all
from poweragent.eval.margin import extract_margin
from poweragent.eval.metrics import compute_metrics
from poweragent.sim.backends.session import PythonSession
from poweragent.sim.hashing import (
    fast_fingerprint,
    model_package_hash,
    resolve_dependency_closure,
)
from poweragent.sim.simulate import inspect_model, simulate
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, ScenarioSpec

pytestmark = pytest.mark.matlab

BASELINE_PARAMS = {"rcomp": 12000.0, "ccomp": 2.2e-9}
GRID_BEST_PARAMS = {"rcomp": 28480.3586844, "ccomp": 100e-12}
TASK_ID = "matlab_backend_task"

# 两档双后端一致性容差，构成见模块 docstring。
TOL_TRANSIENT_REL = 0.02
TOL_RIPPLE_SCALE_REL = 0.15
RIPPLE_SCALE_METRICS = frozenset({"output_ripple", "overshoot"})

# 裕量的双实现一致性容差。两侧是完全独立的实现路径——MATLAB 的
# linearize+allmargin（数值线性化）与 Python 的 open_loop_state_space（手写状态
# 空间 + 解析纯延迟）——实测偏差 ≤0.0005 deg / ≤0.0005 dB。容差取
# metrics.yaml 里 phase_margin / gain_margin 的 compare_tolerance（1.0 deg /
# 0.5 dB），即"同一候选重复评估时判为一致"的既有口径：两个实现算同一个数学对象，
# 没有理由比重复评估宽松。
PYTHON_REFERENCE_MARGINS = {
    # (rcomp, ccomp) -> (phase_margin_deg, gain_margin_db)
    # 由 sim/backends/margin.py 的 margins_from_state_space() 在
    # models/buck4ph_averaged.yaml、vin=10.8 V、T=85 degC 下算出。
    "baseline": (83.463, 16.698),
    "grid_best": (73.022, 9.186),
}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundle(config_dir: Path):
    return load_all(config_dir)


@pytest.fixture(scope="module")
def frozen_hashes(bundle, repo_root: Path):
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    return fast_fingerprint(closure), model_package_hash(closure, model_dump)


@pytest.fixture(scope="module")
def matlab_session(bundle, repo_root: Path):
    """一个 module 级复用的 `MatlabSession`。

    环境不满足时 skip 而不是 fail，逐项检查并给出具体原因——"MATLAB 测试被跳过了"
    本身不是信息，"因为 slx_entry 没登记"才是。

    engine 启动实测 16~21 s，因此整个文件共用一个会话（`MatlabSession` 本就要求
    进程内单例、串行使用）。
    """
    pytest.importorskip("matlab.engine", reason="matlabengine 未安装")

    from poweragent.sim.engine import MatlabSession

    for variant in ("switching", "averaged"):
        section = getattr(bundle.model.model_package, variant)
        if section is None or not section.slx_entry:
            pytest.skip(f"model.yaml 的 model_package.{variant}.slx_entry 未登记")
        if not (repo_root / section.slx_entry).is_file():
            pytest.skip(
                f"{section.slx_entry} 不存在；先跑 python scripts/build_slx_models.py"
            )

    session = MatlabSession(base_dir=repo_root)
    try:
        session.__enter__()
    except Exception as exc:  # noqa: BLE001 -- 任何启动失败都应转为 skip
        pytest.skip(f"MATLAB Engine 无法启动: {exc}")

    # Simulink 许可证在 load_system 时才签出，engine 启动成功不代表它可用。
    # 在这里显式签出一次，签不出就 skip——否则失败会以「找不到系统或文件」之类的
    # 形式出现在第一个真正用到模型的测试里，与真实原因相距很远。
    try:
        for feature in ("Simulink", "Simulink_Control_Design"):
            ok = session._engine.eval(  # noqa: SLF001 -- 测试有意检查环境前置条件
                f"license('checkout','{feature}')", nargout=1
            )
            if not ok:
                session.__exit__(None, None, None)
                pytest.skip(f"{feature} 许可证无法签出")
    except Exception as exc:  # noqa: BLE001
        session.__exit__(None, None, None)
        pytest.skip(f"许可证探测失败: {exc}")

    try:
        yield session
    finally:
        session.__exit__(None, None, None)


@pytest.fixture(scope="module")
def artifacts_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("matlab_artifacts")


def _scenario(bundle, scenario_id: str, **overrides) -> ScenarioSpec:
    row = next(s for s in bundle.task.scenarios if s.scenario_id == scenario_id)
    fields = {
        "scenario_id": row.scenario_id,
        "tier": row.tier,
        "model_variant": row.model_variant,
        "require_margin": row.require_margin,
        "vin_v": row.vin_v,
        "temp_c": row.temp_c,
        "load_start_a": row.load_start_a,
        "load_end_a": row.load_end_a,
        "slew_a_per_us": row.slew_a_per_us,
        "spec_version": "matlab_backend",
    }
    fields.update(overrides)
    return ScenarioSpec(**fields)


def _simulate(
    bundle, repo_root, frozen, artifacts_root, session, scenario, run_id, params
):
    return simulate(
        Candidate(candidate_id=f"cand_{run_id}", parameters_si=params),
        scenario,
        model_cfg=bundle.model,
        metrics_cfg=bundle.metrics,
        session=session,
        run_id=run_id,
        artifacts=ArtifactStore(base_dir=artifacts_root),
        task_id=TASK_ID,
        frozen_fingerprint=frozen[0],
        frozen_model_package_hash=frozen[1],
        base_dir=repo_root,
    )


def _metrics(bundle, waveform_ref: str, scenario, run_id: str) -> dict[str, float]:
    return {
        m.metric_id: m.value
        for m in compute_metrics(
            waveform_ref,
            scenario,
            bundle.metrics,
            run_id=run_id,
            guard=bundle.model.io_contract.divergence_guard,
            vout_target_v=bundle.model.io_contract.vout_target_v,
        )
        if m.valid
    }


@pytest.fixture(scope="module")
def screening_run(bundle, repo_root, frozen_hashes, artifacts_root, matlab_session):
    """筛选场景（平均模型）跑一次，多个测试共用。"""
    scenario = _scenario(bundle, "scr_nom")
    result = _simulate(
        bundle,
        repo_root,
        frozen_hashes,
        artifacts_root,
        matlab_session,
        scenario,
        "run_scr_matlab",
        BASELINE_PARAMS,
    )
    return scenario, result


@pytest.fixture(scope="module")
def evaluation_run(bundle, repo_root, frozen_hashes, artifacts_root, matlab_session):
    """评价场景（开关模型时域 + 平均模型裕量）跑一次，多个测试共用。

    实测首次约 29 s（1 ms @ 20 ns 固定步长，4 相 PWM；其中绝大部分是 Simulink 首次
    编译，同一会话内后续调用降到 0.5~1 s），因此不在每个测试里重跑。
    """
    scenario = _scenario(bundle, "eval_vin_min_step_max")
    result = _simulate(
        bundle,
        repo_root,
        frozen_hashes,
        artifacts_root,
        matlab_session,
        scenario,
        "run_eval_matlab",
        BASELINE_PARAMS,
    )
    return scenario, result


# --------------------------------------------------------------------------
# 1. I/O 契约
# --------------------------------------------------------------------------


def test_inspect_model_matches_io_contract(bundle, matlab_session) -> None:
    """两个变体的 Block Path 与信号名都在模型里真实存在。

    这条同时守护三处容易漂移的约定：`injectable_params.*.block_path` 的根段替换
    （`resolve_block_path.m`）、`param` 名与实际块参数名一致（Constant 的 `Value`）、
    以及信号记录设在输出端口上而非信号线上（`inspect_model.m` 的反查方式）。
    任一处不符都会让 `inspect_model` 抛 `pa:ContractError`。
    """
    for variant in ("averaged", "switching"):
        info = inspect_model(bundle.model, variant, session=matlab_session)

        assert info.model_path.endswith(".slx"), info.model_path
        assert sorted(info.checked_params) == ["ccomp", "rcomp"]
        assert sorted(info.checked_signals) == ["iout", "iphase", "vout"]


def test_non_whitelisted_call_is_rejected_before_touching_engine(matlab_session) -> None:
    """白名单外的函数名在触碰 Engine 之前被拒，且会话对后续调用保持可用。"""
    from poweragent.sim.engine import MatlabCallNotAllowedError

    with pytest.raises(MatlabCallNotAllowedError):
        matlab_session.call("system", "echo hi")
    with pytest.raises(MatlabCallNotAllowedError):
        matlab_session.call("pa.simulate_once ", "{}", "{}", "{}")  # 尾随空格
    with pytest.raises(MatlabCallNotAllowedError):
        matlab_session.call("PA.simulate_once", "{}", "{}", "{}")  # 大小写不同

    # 会话对后续调用保持可用：白名单校验发生在触碰 Engine 之前，被拒的调用不会
    # 让会话进入不可用状态。第二、三次 raises 本身已经说明这一点（会话若已损坏，
    # 抛出的会是 MatlabExecutionError 之类而不是 MatlabCallNotAllowedError），
    # 这里再确认 Engine 句柄仍在。
    assert matlab_session._engine is not None  # noqa: SLF001 -- 有意检查会话状态


def test_matlab_package_is_on_path(matlab_session) -> None:
    """`__enter__` 里的 addpath 生效——没有它全部 `pa.*` 都无法解析。

    用 `which` 而不是 `exist`：`exist('pa.inspect_model')` 对包限定名恒返回 0，
    即使函数完全可用（R2024a 实测），拿它做可达性检查会得到假阴性。
    """
    where = matlab_session._engine.eval(  # noqa: SLF001 -- 有意检查会话内部状态
        "which('pa.simulate_once')", nargout=1
    )
    assert where, "pa.simulate_once 不在 MATLAB 搜索路径上"
    assert "simulate_once.m" in str(where)


# --------------------------------------------------------------------------
# 2. 波形产物往返：MATLAB 落盘 -> eval 层消费
# --------------------------------------------------------------------------


def test_screening_waveform_is_consumable_by_eval(bundle, screening_run) -> None:
    """筛选场景的波形产物能被 `compute_metrics()` 直接读出全部时域指标。

    这条打通 `collect_signals.m` 与 `eval/metrics.py::_load_waveform()` 之间的格式
    契约。它曾经是断的：`collect_signals.m` 落盘的是变量名 `logsout` 的
    `Simulink.SimulationData.Dataset` 对象且用 `-v7.3`（HDF5），而
    `scipy.io.loadmat` 既不支持 v7.3、也无法把类对象还原成
    `{signal: (time, data)}`。任一处退回去，这条测试就会失败。
    """
    scenario, result = screening_run

    assert result.status == "ok", result
    assert result.waveform_ref is not None
    assert Path(result.waveform_ref).is_file()
    assert result.observable_ref is None, "筛选场景不采裕量"
    assert result.engine_starts == 1

    metrics = _metrics(bundle, result.waveform_ref, scenario, "run_scr_matlab")

    # 时域五项 + 两项约束观测量都必须算得出来（裕量两项不在时域层产出）。
    for metric_id in (
        "settling_time",
        "output_ripple",
        "overshoot",
        "undershoot",
        "phase_peak_current",
        "obs.vout_min",
        "obs.vout_max",
    ):
        assert metric_id in metrics, f"{metric_id} 未产出或 valid=False"


def test_switching_waveform_has_real_ripple_and_phase_currents(
    bundle, evaluation_run
) -> None:
    """开关模型产出真实的输出纹波与逐相不同的相电流——平均模型里这两者都不存在。

    这条确认 PWM 与相位交错真的建进了模型，而不是搭出了一个"看起来像开关模型
    但实际在跑平均行为"的东西。判据取自 Python 开关后端的实测量级
    （稳态纹波峰峰约 13.7 mV、单相三角波峰峰约 11.9 A），用宽区间而非精确值：
    这里要否证的是"纹波为零/各相完全相同"，不是复算纹波幅值（那是双后端一致性
    测试的事）。
    """
    import numpy as np
    import scipy.io as sio

    _, result = evaluation_run
    assert result.status == "ok", result

    raw = sio.loadmat(result.waveform_ref, squeeze_me=True, struct_as_record=False)
    t = np.asarray(raw["Vout"].time)
    v = np.asarray(raw["Vout"].data)
    iphase = np.asarray(raw["Iphase"].data)
    step_trigger_s = float(raw["step_trigger_s"])

    assert iphase.ndim == 2 and iphase.shape[1] == 4, iphase.shape

    # 阶跃前的一段稳态（避开初始瞬态与阶跃本身）
    mask = (t > 0.3 * step_trigger_s) & (t < 0.9 * step_trigger_s)
    ripple_pp_v = float(v[mask].max() - v[mask].min())
    assert 5e-3 < ripple_pp_v < 40e-3, f"稳态纹波峰峰 {ripple_pp_v * 1e3:.2f} mV 不在量级内"

    phase_pp = iphase[mask].max(axis=0) - iphase[mask].min(axis=0)
    assert (phase_pp > 5.0).all(), f"某相无三角波纹波: {phase_pp}"
    assert (phase_pp < 20.0).all(), (
        f"某相纹波异常大 {phase_pp}——相位交错可能漏拍"
        f"（载波相位判据踩在浮点边界上时会漏一个开通周期，纹波翻倍）"
    )

    # 四相载波错开 360/N 度，任一时刻各相电流不应完全相同。
    spread = float(np.abs(iphase[mask] - iphase[mask].mean(axis=1, keepdims=True)).max())
    assert spread > 1.0, f"各相电流几乎相同（{spread:.3f} A），相位交错未生效"


# --------------------------------------------------------------------------
# 3. 裕量链路
# --------------------------------------------------------------------------


def test_margin_extraction_matches_python_reference(
    bundle, repo_root, frozen_hashes, artifacts_root, matlab_session
) -> None:
    """`linearize + allmargin` 与 Python 手写状态空间给出同一组裕量。

    两侧是完全独立的实现路径，因此这条不只是回归测试，它同时否证了三类容易发生
    且不会自己暴露的错误：

    - `UseExactDelayModel` 未开启（Transport Delay 被按 Pade 0 阶丢弃）：实测会让
      PM 从 73.02 变成 80.37、GM 直接为 0。
    - `GainMargin` 的 dB 换算漏掉（`allmargin` 返回的是线性比值）：会让 GM 变成
      20·log10 的两次嵌套，数值仍"看起来正常"。
    - `allmargin` 的 `GainMargin` 首元素是 DC 处的伪穿越（恒为 0），按绝对值最小
      挑会选中它并让每个候选都判 `extraction_failed`。

    裕量采集在**平均模型**上做，与触发它的场景声明的 `model_variant` 无关
    （`primary_method = linear_analysis_on_averaged`）；这里用一个 `require_margin`
    的场景把这条路径走通。
    """
    pm_tol = float(bundle.metrics.metrics.phase_margin.compare_tolerance.absolute)
    gm_tol = float(bundle.metrics.metrics.gain_margin.compare_tolerance.absolute)

    scenario = _scenario(bundle, "eval_vin_min_step_max", model_variant="averaged")

    for label, params in (
        ("baseline", BASELINE_PARAMS),
        ("grid_best", GRID_BEST_PARAMS),
    ):
        result = _simulate(
            bundle,
            repo_root,
            frozen_hashes,
            artifacts_root,
            matlab_session,
            scenario,
            f"run_margin_{label}",
            params,
        )
        assert result.status == "ok", result
        assert result.observable_ref, (
            f"{label}: 未产出频响产物。run_linear_analysis 的 reason 字段会说明原因"
        )
        # require_margin=true 的场景必须计入额外的引擎启动（R8.4）。
        expected = 1 + bundle.metrics.margin_extraction.extra_engine_starts_per_candidate
        assert result.engine_starts >= expected

        pm, gm = extract_margin(
            result.observable_ref, bundle.metrics, run_id=f"run_margin_{label}"
        )
        assert pm.valid and gm.valid, (pm, gm)

        ref_pm, ref_gm = PYTHON_REFERENCE_MARGINS[label]
        assert abs(pm.value - ref_pm) < pm_tol, f"{label}: PM {pm.value} vs {ref_pm}"
        assert abs(gm.value - ref_gm) < gm_tol, f"{label}: GM {gm.value} vs {ref_gm}"


# --------------------------------------------------------------------------
# 4. .slx 的可复现性（门禁的区分能力）
# --------------------------------------------------------------------------


def test_regenerating_the_model_yields_the_same_package_hash(
    repo_root: Path, matlab_session, tmp_path: Path
) -> None:
    """用同一份 spec 重复生成 `.slx`，`model_package_hash()` 必须一致。

    这条守护的是门禁的**区分能力**，不是性能或整洁。`.slx` 的原始字节对重复生成
    不确定——实测同一 spec 连续生成 4 次得到 4 个不同的 sha256，文件大小也在
    98700~98704 之间抖动，差异全在文件元数据、文档 UUID、块 UUID 与编辑器状态里。
    若 `model_package_hash()` 直接哈希原始字节，它测的就是"这个二进制文件最近有没有
    被重新写过"而不是"模型是否真的变了"：一个参数都没改、只是重跑了一次生成脚本，
    门禁也会判"模型变了"。而门禁一旦频繁误报，人就会习惯性地重跑记录或再审批一次，
    那时真正的模型变更会被同样地放过去。

    规范化逻辑本身由 `tests/unit/test_slx_hashing.py` 用构造的 OPC 包守护（含对
    模型定义改动仍敏感的反向断言），那一层不需要 MATLAB。本条是唯一能验证"真实
    Simulink 重复保存"这个前提的地方。

    同时断言 `fast_fingerprint()` **确实**变了：两级不变性检查的设计就是"低成本指纹
    不一致时升级为全量重算"，重新生成模型正应落在"指纹变、全量不变"这条假警报路径
    上（`sim/simulate.py` 的 `_check_model_not_mutated()`）。若指纹也不变，说明
    `(size, mtime_ns)` 没能反映文件被重写，那是另一个问题。
    """
    import json
    import sys

    if str(repo_root / "scripts") not in sys.path:
        sys.path.insert(0, str(repo_root / "scripts"))
    from build_slx_models import build_spec

    spec = build_spec("averaged")
    model_config = {"model_package": {"averaged": {"slx_entry": "x.slx"}}}

    generated: list[Path] = []
    for index in range(2):
        target = tmp_path / f"regen{index}.slx"
        spec_i = {**spec, "out_path": str(target)}
        matlab_session._engine.build_slx_model(  # noqa: SLF001 -- 生成器不在 pa.* 白名单内
            json.dumps(spec_i), nargout=0
        )
        assert target.is_file(), f"{target} 未生成"
        generated.append(target)

    raw_digests = {hashlib.sha256(p.read_bytes()).hexdigest() for p in generated}
    package_hashes = {model_package_hash([p], model_config) for p in generated}
    fingerprints = {fast_fingerprint([p]) for p in generated}

    assert len(package_hashes) == 1, (
        f"同一 spec 重复生成得到不同的 model_package_hash: {package_hashes}"
    )
    assert len(raw_digests) == 2, (
        "原始字节竟然一致了——说明 Simulink 这一版不再写入随时间变化的标识。"
        "规范化本身仍应保留（它不依赖这个前提），但本条测试的前提描述需要更新。"
    )
    assert len(fingerprints) == 2, (
        "fast_fingerprint 未能反映文件被重写，两级不变性检查的升级路径失效"
    )


# --------------------------------------------------------------------------
# 5. 双后端一致性
# --------------------------------------------------------------------------


def test_two_backends_describe_the_same_circuit(
    bundle, repo_root, frozen_hashes, artifacts_root, evaluation_run
) -> None:
    """同一候选同一评价场景，两个后端的指标落在同一档容差内。

    这是把 Python 后端保留下来的主要理由：它是 MATLAB 结果的独立交叉验证。
    容差分两档，构成见模块 docstring——瞬态与峰值类 2%，纹波尺度类 15%。

    这条测试实际抓到过一个 bug：开关模型的载波相位判据踩在浮点边界上，导致相 2
    每隔若干周期漏一次开通，`output_ripple` 因此比 Python 后端大 4.5 倍
    （348%），而 `settling_time` / `phase_peak_current` 只差约 2%——只看瞬态量
    是发现不了的。
    """
    scenario, matlab_result = evaluation_run
    assert matlab_result.status == "ok", matlab_result

    with PythonSession(base_dir=repo_root) as python_session:
        python_result = _simulate(
            bundle,
            repo_root,
            frozen_hashes,
            artifacts_root,
            python_session,
            scenario,
            "run_eval_python",
            BASELINE_PARAMS,
        )
    assert python_result.status == "ok", python_result

    matlab_metrics = _metrics(
        bundle, matlab_result.waveform_ref, scenario, "run_eval_matlab"
    )
    python_metrics = _metrics(
        bundle, python_result.waveform_ref, scenario, "run_eval_python"
    )

    shared = sorted(set(matlab_metrics) & set(python_metrics))
    assert len(shared) >= 5, f"可比较的指标太少: {shared}"

    deviations: list[str] = []
    for metric_id in shared:
        a = python_metrics[metric_id]
        b = matlab_metrics[metric_id]
        rel = abs(b - a) / max(abs(a), 1e-12)
        tol = (
            TOL_RIPPLE_SCALE_REL
            if metric_id in RIPPLE_SCALE_METRICS
            else TOL_TRANSIENT_REL
        )
        if rel > tol:
            deviations.append(
                f"{metric_id}: python={a:.6g} matlab={b:.6g} rel={rel:.2%} > {tol:.0%}"
            )

    assert not deviations, "双后端偏差超出容差:\n  " + "\n  ".join(deviations)
