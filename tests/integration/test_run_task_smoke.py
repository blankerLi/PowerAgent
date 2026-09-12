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


def test_checkpoint1_lists_every_hard_constraint(bundle) -> None:
    """Checkpoint 1 给人确认的硬约束必须与 schema 声明的完全一致，一条不少。

    这里原本手写了一份名字清单，只有四项——`gain_margin_min` 在它作为第五条硬
    约束被加进 `constraints.yaml` 时没有同步加进去。后果是人在确认"我要搜的是
    这个任务"时看到的恰好是**修复前**的约束集，而那条缺失的约束正是参考扫描
    暴露"约束集缺了一半稳定性判据"之后补上的那条。

    判定链路不受影响（`eval/constraints.py` 直接从 `ConstraintsConfig` 读），
    所以这个缺陷不会让任何结论出错——它只让人工确认环节看到的信息不完整，
    而那恰恰是最难靠其他测试发现的一类问题。

    断言的是"与 schema 字段集合相等"而不是"包含这五个名字"：后者在新增第六条
    约束时仍会漏，前者不会。
    """
    from poweragent.controller.run_task import _checkpoint1_summary

    summary = _checkpoint1_summary(bundle.task, bundle.metrics, bundle.constraints)

    listed = set(summary["hard_constraints"])
    declared = set(type(bundle.constraints.hard_constraints).model_fields)

    assert listed == declared, (
        f"Checkpoint 1 与 schema 不一致；缺失={sorted(declared - listed)} "
        f"多余={sorted(listed - declared)}"
    )
    # 每条都要带齐四个字段，否则人看到的是残缺的约束描述。
    for name, entry in summary["hard_constraints"].items():
        assert set(entry) == {"value", "observable", "sense", "applies_to_tier"}, (
            f"{name} 的字段不完整: {sorted(entry)}"
        )


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


def test_real_simulations_record_their_elapsed_time(outcome) -> None:
    """真实跑过仿真的 `runs` 行必须落下 `elapsed_ms`，不能是 NULL。

    这一列此前在所有运行里恒为 `NULL`：`close_run_ok()` 只写了 `waveform_ref` /
    `observable_ref` / `ended_at` 三列，尽管它接收的是整个 `SimulationResult`
    （其中 `elapsed_ms` 一直有值）。实测三次运行 24/24、45/45、34/34 行全空。

    它不是装饰性的过程量，缺了它有三处直接后果：`preflight` 的
    `check_budget_feasibility()` 拿不到实测的 `probe_single_run_s`（`model.yaml`
    里 `max_wallclock_per_run_s: 180` 那句"无探针数据，按保守估计取 3 分钟"就是
    这个缺口的产物）；README 主张的分层执行成本梯度无法从库里复算；
    `find_cached_simulation()` 读回时的 `or 0` 兜底会把 `NULL` 变成 0，让缺失
    看起来像"这次仿真不花时间"。

    缓存命中的行不在断言范围内——那些行由 `run_task()` 显式传 `elapsed_ms=0`，
    语义是"本次没有真的跑仿真"，是正确的 0 而不是缺失。
    """
    result, store, _propose = outcome

    rows = store.connection.execute(
        "SELECT run_id, elapsed_ms, cache_hit FROM runs "
        "WHERE task_id=? AND status='done'",
        (result.task_id,),
    ).fetchall()
    assert rows, "没有任何 done 状态的 runs 行"

    real_runs = [r for r in rows if not r[2]]
    assert real_runs, "没有非缓存命中的运行，本条断言失去意义"

    missing = [r[0] for r in real_runs if r[1] is None]
    assert not missing, f"这些真实运行的 elapsed_ms 为 NULL: {missing}"

    # 至少有一次仿真耗时为正——全 0 说明写入了但值不对（例如错传了常量 0）。
    assert any(r[1] > 0 for r in real_runs), (
        f"全部真实运行的 elapsed_ms 都是 0: {[(r[0], r[1]) for r in real_runs]}"
    )


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


# --------------------------------------------------------------------------
# Checkpoint 3：最终推荐审批与 result_hash 绑定
# --------------------------------------------------------------------------
#
# 同样挂在这个文件：`result_hash` 的 9 键里有 7 个要从一个真跑过仿真的库里聚合
# 出来（`worst_case` 需要评价层跑完、`per_scenario` 需要 `metric_results` 有行），
# 而 `outcome` fixture 已经提供了这样一个库。
#
# 这一组的核心不是"能算出一个哈希"，而是**这个哈希对什么敏感、对什么不敏感**。
# `approvals.result_hash` 的全部价值在于 `apply` 时重算比对能挡住误签（CP-10），
# 因此两个方向都必须测：
#
#   该敏感的敏感   —— 候选的评价结果变了，哈希必须变（否则闸门形同虚设）
#   该不敏感的不敏感 —— 过程量（`elapsed_ms`/`cache_hit`/`budget_units`）变了，
#                      哈希不得变（否则重跑一次同一候选就把审批作废了，
#                      design.md §5.3.1 明确把它们排除在重算范围外）
#
# 只测前者会漏掉一个很实际的失效：把整行 `runs` 都塞进哈希也能让前者通过，但那样
# 每次缓存命中都会改变哈希。


def _clone_store(store: Store, tmp_path: Path, name: str = "clone.db") -> Store:
    """把库整份拷到 `tmp_path` 上，供破坏性断言使用。

    用 sqlite 的 backup API 而不是复制文件：库开在 WAL 模式下
    （`ddl.sql` 的 `PRAGMA journal_mode=WAL`），直接 copy 主文件会漏掉还留在
    `-wal` 里尚未 checkpoint 的页，拷出来的副本可能比源库旧。

    `outcome` 是 module 作用域的 fixture，本组里改数据的断言若直接改源库，
    执行顺序就会变成隐式依赖——先跑的把库改了，后跑的看到的就不是同一个库。
    """
    import sqlite3

    destination = tmp_path / name
    connection = sqlite3.connect(destination)
    try:
        store.connection.backup(connection)
    finally:
        connection.close()
    return Store(destination)


def _evaluation_run_ids(store: Store, task_id: str, candidate_id: str) -> list[str]:
    return [
        row[0]
        for row in store.connection.execute(
            "SELECT r.run_id FROM runs r "
            "JOIN scenario_set s ON s.task_id = r.task_id "
            "                   AND s.scenario_id = r.scenario_id "
            "WHERE r.task_id=? AND r.candidate_id=? AND s.tier='evaluation'",
            (task_id, candidate_id),
        ).fetchall()
    ]


def test_result_hash_is_stable_across_recomputation(outcome, bundle) -> None:
    """同一库状态重算两次得到同一个哈希，且不截断。

    这是"`apply` 前重算比对"的前提：若重算本身不稳定，比对永远失败，那道闸门就
    只能被绕过。
    """
    from poweragent.report.context import compute_result_hash

    result, store, _propose = outcome
    kwargs = {"metrics_cfg": bundle.metrics, "constraints_cfg": bundle.constraints}

    first = compute_result_hash(
        result.task_id, store, result.best_candidate_id, **kwargs
    )
    second = compute_result_hash(
        result.task_id, store, result.best_candidate_id, **kwargs
    )

    assert first == second, "同一库状态重算两次应得同一哈希"
    assert len(first) == 64, "sha256 十六进制应为 64 字符（登记表中只有 candidate_id 截断）"
    assert first == first.lower(), "应为小写十六进制"


def test_result_hash_changes_when_the_primary_metric_changes(
    outcome, bundle, tmp_path
) -> None:
    """主目标指标的取值变了，哈希必须变。

    改的是 `settling_time`，它既是 `objective.primary.metric_id`（进 `worst_case`
    一项）也是 `per_scenario` 里的一行，两条路径都会带动哈希变化。
    """
    from poweragent.report.context import compute_result_hash

    result, store, _propose = outcome
    clone = _clone_store(store, tmp_path, "primary.db")
    kwargs = {"metrics_cfg": bundle.metrics, "constraints_cfg": bundle.constraints}

    before = compute_result_hash(
        result.task_id, clone, result.best_candidate_id, **kwargs
    )

    run_ids = _evaluation_run_ids(clone, result.task_id, result.best_candidate_id)
    assert run_ids, "最佳候选应有评价层的 runs 行"
    clone.connection.executemany(
        "UPDATE metric_results SET value = value + 1.0 "
        "WHERE run_id=? AND metric_id='settling_time'",
        [(run_id,) for run_id in run_ids],
    )
    clone.connection.commit()

    after = compute_result_hash(
        result.task_id, clone, result.best_candidate_id, **kwargs
    )
    assert after != before, "主目标取值变化后 result_hash 必须变"


def test_result_hash_changes_when_a_non_primary_metric_changes(
    outcome, bundle, tmp_path
) -> None:
    """非主目标指标变了，哈希也必须变。

    这条比上一条更能定位问题：`output_ripple` 不参与 `worst_case`，只出现在
    `per_scenario` 里。若只有上一条通过而这条失败，说明 `per_scenario` 根本没
    进哈希——而它恰恰是"这个候选在各场景上的完整证据"这一层。
    """
    from poweragent.report.context import compute_result_hash

    result, store, _propose = outcome
    clone = _clone_store(store, tmp_path, "secondary.db")
    kwargs = {"metrics_cfg": bundle.metrics, "constraints_cfg": bundle.constraints}

    before = compute_result_hash(
        result.task_id, clone, result.best_candidate_id, **kwargs
    )

    run_ids = _evaluation_run_ids(clone, result.task_id, result.best_candidate_id)
    clone.connection.executemany(
        "UPDATE metric_results SET value = value * 1.5 "
        "WHERE run_id=? AND metric_id='output_ripple'",
        [(run_id,) for run_id in run_ids],
    )
    clone.connection.commit()

    after = compute_result_hash(
        result.task_id, clone, result.best_candidate_id, **kwargs
    )
    assert after != before, "per_scenario 里任一指标取值变化后 result_hash 必须变"


def test_result_hash_ignores_process_metrics(outcome, bundle, tmp_path) -> None:
    """过程量（`elapsed_ms` / `cache_hit` / `budget_units`）变化不得改变哈希。

    design.md §5.3.1 把这三列明确排除在重算范围外，理由写在同一句里："重跑同一
    候选不应作废审批"。缓存命中会让 `cache_hit=1`、`budget_units=0`、
    `elapsed_ms` 归零，若它们进了哈希，一次重跑就能让先前的审批失效——那时这道
    闸门拦下的不是误签，而是正常的复用。
    """
    from poweragent.report.context import compute_result_hash

    result, store, _propose = outcome
    clone = _clone_store(store, tmp_path, "process.db")
    kwargs = {"metrics_cfg": bundle.metrics, "constraints_cfg": bundle.constraints}

    before = compute_result_hash(
        result.task_id, clone, result.best_candidate_id, **kwargs
    )

    clone.connection.execute(
        "UPDATE runs SET elapsed_ms = COALESCE(elapsed_ms, 0) + 9999, "
        "                cache_hit = 1, "
        "                budget_units = budget_units + 7 "
        "WHERE task_id=? AND candidate_id=?",
        (result.task_id, result.best_candidate_id),
    )
    clone.connection.commit()

    after = compute_result_hash(
        result.task_id, clone, result.best_candidate_id, **kwargs
    )
    assert after == before, (
        "过程量不在 result_hash 的重算范围内，它们变化时哈希必须保持不变"
    )


def test_approve_rejects_an_unknown_candidate(outcome, bundle) -> None:
    """审批一个不在 `candidates` 表里的 id 必须被拒。"""
    from poweragent.report.context import (
        ResultHashUnavailableError,
        compute_result_hash,
    )

    result, store, _propose = outcome

    with pytest.raises(ResultHashUnavailableError, match="不在 candidates 表中"):
        compute_result_hash(
            result.task_id,
            store,
            "cand_does_not_exist",
            metrics_cfg=bundle.metrics,
            constraints_cfg=bundle.constraints,
        )


def test_approve_rejects_an_infeasible_candidate(outcome, bundle, tmp_path) -> None:
    """在某个评价场景上不可行的候选没有可绑定的结果，审批必须被拒。

    这是 worst-case 聚合的 `HAVING` 完备性过滤在审批路径上的延伸：那条 SQL 已经
    把不可行与未跑完的候选排除在结果集之外，因此"算不出 worst_case"与"不该被
    审批"在这里是同一件事，不需要另写一套可行性判断。
    """
    from poweragent.report.context import (
        ResultHashUnavailableError,
        compute_result_hash,
    )

    result, store, _propose = outcome
    clone = _clone_store(store, tmp_path, "infeasible.db")

    clone.connection.execute(
        "UPDATE constraint_results SET feasible = 0 WHERE candidate_id=?",
        (result.best_candidate_id,),
    )
    clone.connection.commit()

    with pytest.raises(ResultHashUnavailableError, match="完备性过滤"):
        compute_result_hash(
            result.task_id,
            clone,
            result.best_candidate_id,
            metrics_cfg=bundle.metrics,
            constraints_cfg=bundle.constraints,
        )


def test_approve_rejects_drifted_config(outcome, bundle) -> None:
    """当前配置的哈希与 `tasks` 行记录的不一致时，审批必须被拒。

    与报告渲染的处理刻意不同：`_check_config_binding()` 遇到漂移只把依赖当前配置
    的段落标为"不可用"、继续渲染，因为任务跑完之后改配置是常事而报告是给人读的。
    审批不能这样——`worst_case` 必须用当前 `metrics_cfg` 的 `primary.metric_id`
    才算得出来（库里只有它的哈希），配置不是当初那份时，算出的 `worst_case` 与
    `tasks` 行记录的 `metrics_hash` 描述的就不是同一件事。
    """
    from poweragent.report.context import (
        ResultHashUnavailableError,
        compute_result_hash,
    )

    result, store, _propose = outcome

    # 只动 tie_tolerance：它不改变任何指标的算法，却足以让 metrics_hash 变化。
    # 用这种"看起来无害"的改动，正是要说明判据是哈希相等而不是某种语义等价判断。
    drifted = bundle.metrics.model_copy(
        update={
            "objective": bundle.metrics.objective.model_copy(
                update={"tie_tolerance": bundle.metrics.objective.tie_tolerance + 0.1}
            )
        }
    )

    with pytest.raises(ResultHashUnavailableError, match="配置已漂移"):
        compute_result_hash(
            result.task_id,
            store,
            result.best_candidate_id,
            metrics_cfg=drifted,
            constraints_cfg=bundle.constraints,
        )


def test_cmd_report_approve_binds_the_recomputed_hash(
    outcome, bundle, config_dir: Path, tmp_path, monkeypatch
) -> None:
    """`poweragent report --approve` 写入的审批行绑定的正是当前重算值。

    走完整的 CLI 入口而不是直接调 `compute_result_hash()`：这条路径上还有双签
    校验、`approvals` 落库与两个模块级默认路径的解析，它们一起构成 Checkpoint 3。

    `monkeypatch.setattr` 替换两个模块级常量而不是 `chdir`：`_DEFAULT_CONFIG_DIR`
    是相对路径 `configs/`，`chdir` 到 tmp_path 之后它就指不到仓库里的配置了。
    """
    from poweragent import cli
    from poweragent.report.context import compute_result_hash

    result, store, _propose = outcome
    clone = _clone_store(store, tmp_path, "approve.db")
    clone_path = tmp_path / "approve.db"
    clone.connection.close()

    monkeypatch.setattr(cli, "DEFAULT_DB_PATH", clone_path)
    monkeypatch.setattr(cli, "_DEFAULT_CONFIG_DIR", config_dir)

    exit_code = cli.cmd_report(
        result.task_id,
        approve=result.best_candidate_id,
        approver="alice",
        second_approver="bob",
        note="smoke test approval",
    )
    assert exit_code == 0

    reopened = Store(clone_path)
    row = reopened.connection.execute(
        "SELECT decision, approver, second_approver, candidate_id, result_hash "
        "FROM approvals WHERE kind='final_recommendation'",
    ).fetchone()
    assert row is not None, "应写入一条 final_recommendation 审批行"
    decision, approver, second_approver, candidate_id, stored_hash = row

    assert decision == "approve"
    assert (approver, second_approver) == ("alice", "bob")
    assert candidate_id == result.best_candidate_id
    assert stored_hash == compute_result_hash(
        result.task_id,
        reopened,
        result.best_candidate_id,
        metrics_cfg=bundle.metrics,
        constraints_cfg=bundle.constraints,
    ), "落库的 result_hash 必须等于同一库状态下的重算值"


def test_cmd_report_approve_requires_two_distinct_approvers(
    outcome, config_dir: Path, tmp_path, monkeypatch
) -> None:
    """双签必须是两个不同的人，同一个人签两次要被拒且不留审批行。

    这条断言的落点在 `record_approval()`（`store/repo.py` 的结构性断言），
    CLI 只负责把它转译成 `UsageError`。一并断言"没有写入任何行"：审批记录的
    用途是不可否认，一条被拒的审批留在表里会让"谁批准了什么"变得可争辩。
    """
    import click

    from poweragent import cli

    result, store, _propose = outcome
    clone = _clone_store(store, tmp_path, "samesigner.db")
    clone_path = tmp_path / "samesigner.db"
    clone.connection.close()

    monkeypatch.setattr(cli, "DEFAULT_DB_PATH", clone_path)
    monkeypatch.setattr(cli, "_DEFAULT_CONFIG_DIR", config_dir)

    with pytest.raises(click.UsageError, match="second_approver != approver"):
        cli.cmd_report(
            result.task_id,
            approve=result.best_candidate_id,
            approver="alice",
            second_approver="alice",
        )

    reopened = Store(clone_path)
    count = reopened.connection.execute(
        "SELECT COUNT(*) FROM approvals WHERE kind='final_recommendation'"
    ).fetchone()[0]
    assert count == 0, "被拒的审批不得留下 approvals 行"


# --------------------------------------------------------------------------
# worst-case 聚合跨评价场景
# --------------------------------------------------------------------------


def test_worst_case_aggregates_over_every_evaluation_scenario(outcome, bundle) -> None:
    """主目标取遍所有评价场景的最差值，而不是某一个场景的值。

    评价集只有一个场景时这条断言是空的——`max()` 作用在单元素集合上恒等于那个
    元素，聚合与不聚合无从区分。`configs/task.yaml` 现有两个评价场景
    （`eval_vin_min_step_max` / `eval_vin_max_unload`），因此这里能真正验证
    `WORST_CASE_SQL` 的 `MAX(...)` 是跨场景取的。

    同时验证 `HAVING` 完备性过滤的前提：进入排名的候选必须在**每个**评价场景上
    都有一行有效的主目标指标。
    """
    from poweragent.eval.aggregate import rank

    result, store, _propose = outcome

    evaluation_ids = {
        s.scenario_id for s in bundle.task.scenarios if s.tier == "evaluation"
    }
    assert len(evaluation_ids) >= 2, (
        "本断言要求至少两个评价场景，否则跨场景聚合无从验证"
    )

    primary = bundle.metrics.objective.primary.metric_id
    ranked = rank(store, result.task_id, bundle.metrics, top_n=10)
    assert ranked, "应有可行候选进入排名"

    for entry in ranked:
        per_scenario = dict(
            store.connection.execute(
                "SELECT r.scenario_id, m.value FROM runs r "
                "JOIN metric_results m ON m.run_id = r.run_id "
                "JOIN scenario_set s ON s.task_id = r.task_id "
                "                   AND s.scenario_id = r.scenario_id "
                "WHERE r.task_id=? AND r.candidate_id=? AND s.tier='evaluation' "
                "  AND m.metric_id=? AND m.valid=1",
                (result.task_id, entry.candidate_id, primary),
            ).fetchall()
        )

        assert set(per_scenario) == evaluation_ids, (
            f"候选 {entry.candidate_id} 只在 {set(per_scenario)} 上有有效主目标，"
            f"却仍进入了排名——HAVING 完备性过滤失效"
        )

        expected = max(per_scenario.values())
        assert entry.worst_case_value == pytest.approx(expected), (
            f"候选 {entry.candidate_id} 的 worst_case={entry.worst_case_value} "
            f"与逐场景取值 {per_scenario} 的最大值 {expected} 不一致"
        )


def test_both_evaluation_scenarios_collect_margin(outcome, bundle) -> None:
    """两个评价场景都落下了 PM/GM 两行指标。

    `require_margin=true` 若在某个评价场景上漏掉，该场景会因支撑指标缺失被判
    违反（R9.3），进而使每个候选都永远不可行。`test_config_contract.py` 从配置
    侧断言了这一点，这里从落库结果侧确认它真的生效了。
    """
    result, store, _propose = outcome

    rows = store.connection.execute(
        "SELECT r.scenario_id, m.metric_id, COUNT(*) FROM runs r "
        "JOIN metric_results m ON m.run_id = r.run_id "
        "JOIN scenario_set s ON s.task_id = r.task_id "
        "                   AND s.scenario_id = r.scenario_id "
        "WHERE r.task_id=? AND s.tier='evaluation' "
        "  AND m.metric_id IN ('phase_margin','gain_margin') "
        "GROUP BY r.scenario_id, m.metric_id",
        (result.task_id,),
    ).fetchall()

    collected: dict[str, set[str]] = {}
    for scenario_id, metric_id, _count in rows:
        collected.setdefault(scenario_id, set()).add(metric_id)

    evaluation_ids = {
        s.scenario_id for s in bundle.task.scenarios if s.tier == "evaluation"
    }
    for scenario_id in evaluation_ids:
        assert collected.get(scenario_id) == {"phase_margin", "gain_margin"}, (
            f"评价场景 {scenario_id} 未落下完整的 PM/GM，实际为 "
            f"{collected.get(scenario_id)}"
        )
