"""用真实后端把 `run_task()` 从头到尾跑一遍。

这是 `controller` 的第一次实际验证。在此之前它有约 3800 行实现却从未运行过：
preflight 的七条断言、场景集冻结、预算账本、分层执行与提前拒绝、失败重试、
轮末聚合与停止判定，全部只在单元层面被间接触及。整条主循环能不能跑通，在这条
测试跑之前是未知的。

写成测试而不是一次性脚本，是为了它能反复跑、能进 CI：主循环的接线一旦被改动破坏，
这里会立刻失败，而不是等到某次手工冒烟才发现。

不用 LLM
--------
`propose_fn` 注入一个固定候选序列。`agent.propose` 尚未实现，而主循环的接线正确性
与候选从哪来无关——把两件事分开验证，能让这条测试在 agent 落地之前就守住主循环，
之后也不会因为 LLM 的不确定性而随机失败。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from poweragent.config.hashing import canonical_json, constraints_hash, metrics_hash
from poweragent.config.loader import load_all
from poweragent.controller.run_task import ProposeResult, run_task
from poweragent.controller.scenario import compute_scenario_set_hash
from poweragent.sim.backends.session import PythonSession
from poweragent.sim.hashing import model_package_hash, resolve_dependency_closure
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, Store

pytestmark = pytest.mark.integration

BASELINE = {"rcomp": 12000.0, "ccomp": 2.2e-9}
GRID_BEST = {"rcomp": 18738.0, "ccomp": 8.111e-10}


def _freeze_configs(store: Store, bundle, repo_root: Path) -> None:
    """写入 preflight 要求的两个冻结点。

    这是 M0 出口的动作：`run_task()` 只**校验**冻结记录，从不创建它们——冻结是
    一次人工审批行为，让程序在跑不通时自动补一条冻结记录，等于让被校验方自己签发
    通行证。

    - `model_package`：依赖闭包全部文件内容 + model.yaml 规范化内容的 sha256
    - `safety`：`H(constraints_hash, metrics_hash, scenario_set_hash)`，公式由
      调用方计算，`store.freeze()` 不解读 hash 内容
    """
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    store.freeze(
        "model_package",
        hash=model_package_hash(closure, model_dump),
        detail="smoke test freeze",
    )

    safety_hash = hashlib.sha256(
        canonical_json(
            {
                "constraints_hash": constraints_hash(
                    bundle.constraints.model_dump(mode="json")
                ),
                "metrics_hash": metrics_hash(bundle.metrics.model_dump(mode="json")),
                "scenario_set_hash": compute_scenario_set_hash(bundle.task),
            }
        ).encode("utf-8")
    ).hexdigest()
    store.freeze("safety", hash=safety_hash, detail="smoke test freeze")


def _scripted_propose_fn(rounds: list[list[dict[str, float]]]):
    """按给定脚本逐轮返回候选；脚本用尽后返回空候选并建议停止。

    最后一轮返回空 + `stop_recommendation=True`，让主循环走"agent 建议停止"的
    分支收尾，而不是靠耗尽 `no_improvement_rounds` 空转数轮——后者会让这条测试
    多跑几轮无意义的仿真。
    """
    calls: list[int] = []

    def propose(state, *, round_context) -> ProposeResult:
        index = len(calls)
        calls.append(index)
        if index >= len(rounds):
            return ProposeResult(candidates=(), stop_recommendation=True)
        return ProposeResult(
            candidates=tuple(
                Candidate(
                    candidate_id=f"cand_r{index}_{i}", parameters_si=params
                )
                for i, params in enumerate(rounds[index])
            ),
            stop_recommendation=False,
        )

    propose.calls = calls  # type: ignore[attr-defined]
    return propose


@pytest.fixture(scope="module")
def bundle(config_dir: Path):
    return load_all(config_dir)


@pytest.fixture(scope="module")
def outcome(bundle, repo_root: Path, tmp_path_factory):
    """跑一次完整的寻优任务，返回 `TaskOutcome` 与 `Store` 供各条断言查询。

    module 作用域：主循环要跑真实仿真（两个候选 × 三次调用），跑一次让多条断言
    共用，比每条测试各跑一遍省几倍时间。
    """
    workspace = tmp_path_factory.mktemp("smoke")
    db_path = workspace / "runs.db"
    store = Store(db_path)
    _freeze_configs(store, bundle, repo_root)

    propose = _scripted_propose_fn([[BASELINE, GRID_BEST]])

    with PythonSession(base_dir=repo_root) as session:
        result = run_task(
            bundle.task,
            bundle.model,
            bundle.metrics,
            bundle.constraints,
            yes=True,  # 跳过 Checkpoint 1 的交互确认
            propose_fn=propose,
            session=session,
            store=store,
            artifacts=ArtifactStore(base_dir=workspace / "artifacts"),
            base_dir=repo_root,
        )

    return result, store, propose


# --------------------------------------------------------------------------
# 主循环能跑通
# --------------------------------------------------------------------------


def test_run_task_completes_with_a_stop_reason(outcome) -> None:
    """主循环正常收尾并给出停止原因。

    `run_task()` 在 FINISH 处断言 `stop_reason` 非空（R16.6）——任务不允许在没有
    明确停止原因的情况下结束。
    """
    result, _store, _propose = outcome

    assert result.task_id == "comp_tuning_v1"
    assert result.stop_reason, "停止原因不得为空"
    assert result.stop_reason in {
        "target_reached", "no_improvement", "budget_exhausted", "stop_and_ask_human"
    }, f"未预期的停止原因: {result.stop_reason}"
    assert result.wallclock_s > 0.0


def test_preflight_passed_all_assertions(outcome) -> None:
    """能走到主循环就意味着 preflight 七条断言全过。

    其中两条此前一直是拒绝状态：`check_margin_extraction_ready`（缺裕量交叉核对
    记录）与 `check_dual_model_consistency`（缺双模型一致性记录）。它们现在由
    真实产出的证据文件放行，而不是被绕过。
    """
    result, _store, _propose = outcome

    # preflight 失败会抛 PreflightError，根本走不到 outcome fixture 返回。
    assert result.stop_reason is not None


def test_task_row_and_scenario_set_are_persisted(outcome, bundle) -> None:
    """`tasks` 行与 `scenario_set` 冻结行都已落库。"""
    result, store, _propose = outcome

    assert store.task_exists(result.task_id)

    rows = store.connection.execute(
        "SELECT scenario_id FROM scenario_set WHERE task_id=? ORDER BY scenario_id",
        (result.task_id,),
    ).fetchall()
    persisted = {row[0] for row in rows}
    assert persisted == {s.scenario_id for s in bundle.task.scenarios}


# --------------------------------------------------------------------------
# 分层执行与预算
# --------------------------------------------------------------------------


def test_both_tiers_ran_and_budget_was_charged(outcome) -> None:
    """两个候选都走完筛选层与评价层，预算按调用次数计入。

    每个候选：筛选层 1 次仿真 + 评价层 1 次仿真 + 1 次裕量分析 = 3 个预算单位。
    两个候选共 6，且不得超过 `max_engine_starts`。
    """
    result, store, _propose = outcome

    used = store.sum_budget_units(result.task_id)
    assert used == result.engine_starts_used
    assert 0 < used <= 200, f"预算用量 {used} 超出配置上限"

    # `runs` 的粒度是 (candidate_id, scenario_id, attempt)，层次信息在场景集里，
    # 因此按 scenario_id 归并即可区分两层。
    rows = store.connection.execute(
        "SELECT scenario_id, COUNT(*) FROM runs WHERE task_id=? GROUP BY scenario_id",
        (result.task_id,),
    ).fetchall()
    by_scenario = dict(rows)

    assert by_scenario.get("scr_nom", 0) >= 2, f"筛选场景运行数不足: {by_scenario}"
    assert by_scenario.get("eval_vin_min_step_max", 0) >= 2, (
        f"评价场景运行数不足: {by_scenario}"
    )


def test_ledger_matches_the_single_source_of_truth(outcome) -> None:
    """预算账本与 `runs.budget_units` 之和一致。

    `run_task()` 在每轮循环顶部与 FINISH 处都断言这条不变量。它是"SQLite 是唯一
    事实源"的具体体现：账本不持有独立的计数，随时可从数据库重建。
    """
    result, store, _propose = outcome

    assert result.engine_starts_used == store.sum_budget_units(result.task_id)


# --------------------------------------------------------------------------
# 评价结果落库
# --------------------------------------------------------------------------


def test_metrics_and_constraint_verdicts_are_persisted(outcome) -> None:
    """每次成功运行都留下指标行与约束判定行。"""
    result, store, _propose = outcome

    metric_count = store.connection.execute(
        "SELECT COUNT(*) FROM metric_results WHERE run_id IN "
        "(SELECT run_id FROM runs WHERE task_id=?)",
        (result.task_id,),
    ).fetchone()[0]
    assert metric_count > 0, "没有任何指标行落库"

    # 裕量指标只在评价场景采集，应当也落了库。
    margin_rows = store.connection.execute(
        "SELECT COUNT(*) FROM metric_results WHERE metric_id='phase_margin' "
        "AND run_id IN (SELECT run_id FROM runs WHERE task_id=?)",
        (result.task_id,),
    ).fetchone()[0]
    assert margin_rows > 0, "评价层应产出相位裕量指标行"

    verdict_count = store.connection.execute(
        "SELECT COUNT(*) FROM constraint_results WHERE run_id IN "
        "(SELECT run_id FROM runs WHERE task_id=?)",
        (result.task_id,),
    ).fetchone()[0]
    assert verdict_count > 0, "没有任何约束判定行落库"


def test_a_feasible_best_candidate_was_selected(outcome) -> None:
    """任务结束时选出了可行的最佳候选。

    两个注入的候选（工程基线与参考网格最优）在评价工况下都满足全部硬约束，
    因此这里必须选出一个——若为 `None`，说明 worst-case 聚合的完备性过滤把两者
    都排除了，那是接线问题而非物理问题。
    """
    result, _store, _propose = outcome

    assert result.best_candidate_id is not None, "应选出可行的最佳候选"
    assert result.best_candidate_id.startswith("cand_r0_")


def test_state_can_be_rebuilt_from_the_database(outcome, bundle) -> None:
    """搜索状态可完全从 SQLite 重建，不依赖内存中的残留。

    这是断点续跑的前提：`rebuild_state()` 只读数据库，不需要上次进程留下任何东西。
    """
    from poweragent.controller.recovery import rebuild_state

    result, store, _propose = outcome
    rebuilt = rebuild_state(
        store,
        result.task_id,
        metrics_cfg=bundle.metrics,
        budget=bundle.task.budget,
    )

    # 剩余预算由 runs 表求和推出，而不是从上次进程的内存里继承。
    max_starts = bundle.task.budget.max_engine_starts
    assert rebuilt.remaining_budget == max_starts - result.engine_starts_used

    # 最佳候选同样应能重建出来，且与任务结束时选出的一致。
    assert rebuilt.current_best is not None
    assert rebuilt.current_best.candidate_id == result.best_candidate_id


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------
#
# 断言挂在这个文件而不是新建一份：`outcome` fixture 已经跑完一次完整任务，报告渲染
# 需要的正是那样一个库。另起一个文件就得再跑一遍仿真，而被测的是渲染而非仿真。


def test_report_renders_from_the_finished_task(outcome, config_dir: Path, tmp_path, monkeypatch) -> None:
    """`render_report()` 端到端产出 Markdown 与图，且不动 `llm_calls`。

    `monkeypatch.chdir(tmp_path)`：图目录是 `artifacts/<task_id>/plots/`，相对当前
    工作目录。不切目录的话测试会往仓库里写产物——测试留下的文件与真实渲染产物混在
    一起，之后没人分得清哪个是哪个。

    渲染前后 `llm_calls` 行数相同（R18 AC1）：报告是纯渲染，不推理。这条性质在
    `report` 模块里靠"没有那条代码路径"保证（它不导入 `agent`），这里从外部再确认
    一次——一旦有人为了"补一段自动结论"而在渲染层调模型，这条断言会失败。
    """
    from poweragent.report.render import render_report

    result, store, _propose = outcome

    calls_before = store.connection.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE task_id=?", (result.task_id,)
    ).fetchone()[0]

    # 先解析成绝对路径再切工作目录：`config_dir` fixture 给的是相对路径，chdir 之后
    # 它就指向 tmp_path 下一个不存在的 configs/。
    absolute_config_dir = config_dir.resolve()
    monkeypatch.chdir(tmp_path)
    report_path = render_report(
        result.task_id, store=store, config_dir=absolute_config_dir
    )

    assert report_path.is_file(), "报告文件未产出"
    text = report_path.read_text(encoding="utf-8")

    # 默认输出路径由 AC1 规定。
    assert report_path == Path("artifacts") / result.task_id / "reports" / (
        f"report_{result.task_id}.md"
    )

    calls_after = store.connection.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE task_id=?", (result.task_id,)
    ).fetchone()[0]
    assert calls_after == calls_before, "渲染报告不得产生 LLM 调用"

    # 十段的标题都在（顺序由 R18 AC4 规定，这里只验证不缺段）。
    for heading in (
        "结论边界声明",
        "任务与配置哈希",
        "Baseline 前值基线",
        "Top 1/2/3",
        "硬约束逐条结果",
        "寻优前后对比",
        "关键波形图",
        "过程埋点摘要",
        "推荐理由",
        "限制说明",
    ):
        assert heading in text, f"报告缺少段落: {heading}"

    # 库里的事实要出现在报告里：任务 id、停止原因、被选中的最佳候选。
    assert result.task_id in text
    assert result.stop_reason in text
    assert result.best_candidate_id is not None
    assert result.best_candidate_id in text


def test_report_waveform_plot_matches_the_best_candidate(
    outcome, config_dir: Path, tmp_path, monkeypatch
) -> None:
    """波形图画的是报告里那个最佳候选的运行，不是碰巧最早跑完的那个。

    这条断言存在的原因是它曾经不成立：早先的实现按 `ORDER BY started_at LIMIT 1`
    取"最早的可行评价层运行"，于是图与第四节列的是两个不同的候选。两边都渲染得
    出来、都不报错，只是说的不是同一个设计——正是这种缺陷需要一条断言钉住。
    """
    from poweragent.report.render import render_report

    result, store, _propose = outcome

    absolute_config_dir = config_dir.resolve()
    monkeypatch.chdir(tmp_path)
    report_path = render_report(
        result.task_id, store=store, config_dir=absolute_config_dir
    )
    text = report_path.read_text(encoding="utf-8")

    plots = sorted((Path("artifacts") / result.task_id / "plots").glob("waveform_*.png"))
    assert plots, "未产出波形图"
    assert len(plots) == 1, f"应只画一张波形图，实际 {len(plots)} 张"

    # 图文件名里的 run_id 必须属于最佳候选，且是评价层的那次运行。
    run_id = plots[0].stem.removeprefix("waveform_")
    row = store.connection.execute(
        "SELECT r.candidate_id, s.tier FROM runs r "
        "JOIN scenario_set s ON s.task_id=r.task_id AND s.scenario_id=r.scenario_id "
        "WHERE r.run_id=?",
        (run_id,),
    ).fetchone()
    assert row is not None, f"图对应的 run_id={run_id!r} 不在库中"
    candidate_id, tier = row
    assert candidate_id == result.best_candidate_id, (
        f"波形图画的是 {candidate_id}，而报告的最佳候选是 {result.best_candidate_id}"
    )
    assert tier == "evaluation", "波形应取自评价层，而非筛选层的近似模型"

    # 报告里的引用指向那张图，且用相对路径（连同 artifacts/ 复制后仍显示得出来）。
    assert f"../plots/{plots[0].name}" in text
