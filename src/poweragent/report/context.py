"""从 SQLite 构造报告的渲染上下文。

报告不做判断，只把库里已有的事实排好顺序。它不重算指标、不推断可行性、不填补缺失
值——这些都已经在 `eval` 与 `controller` 里发生过并落了库。渲染层再算一遍的话，报告
里的数字就可能与数据库里的不一致，而两者不一致时没有任何机制会报错。

为什么"结论段上下文"是一个独立的字典
------------------------------------
R17 AC11 第 3 条要求 `runs` 派生的过程量（首次可行解轮次、可行候选率、重复率）
不出现在结论段的渲染上下文键中。这条约束的可执行形式必须是**结构上的**：

如果整个 `ReportContext` 作为一个命名空间交给模板，那么结论段的模板片段照样能写
`{{ telemetry.feasible_rate }}`——键"不在结论段上下文里"就成了一句空话，因为
Jinja2 的作用域是共享的。`{% include %}` 也不解决问题，它继承父作用域。

因此 `conclusion` 是一个独立映射，结论段由 `render_conclusion()` 用**只含这个映射**
的模板单独渲染成字符串，再作为一个变量交给主模板。过程量根本不在那次渲染的命名空间
里，模板写了也取不到值。这样"没有渲染路径"是构造保证，不是事后检查。

过程量本身不是禁忌，它们照常出现在"过程埋点摘要"段（`telemetry`）——那一段就是为
埋点存在的。被禁的只是让它们给设计结论背书。

配置数值的可追溯性
------------------
R18 AC2/AC3 要求报告里的数字都能以主键定位到 `metric_results` / `constraint_results`
/ `runs` / `tasks`。但 design.md §11.1 要求无可行候选时渲染"各变量 `domain` 两端与
`ticks` 首末档位"，而这些值不在任何表里——`tasks` 存的是 `constraints_hash`，不是
`constraints.yaml` 的内容。

两条要求都成立，办法是区分它们约束的对象：AC3 管的是**测量结果**（那些必须来自
SQL），而 `domain`/`ticks` 是**任务的输入约定**，与哈希同类。所以本模块读当前配置
取这些值，同时把当前配置的哈希与 `tasks` 行记录的哈希比对：一致则该段可信，不一致
则按 AC7 的既定处理标为"不可用"，不估算、不用当前值冒充当时值。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from poweragent.config.hashing import constraints_hash, metrics_hash, result_hash
from poweragent.config.schema import ConstraintsConfig, MetricsConfig, TaskConfig
from poweragent.config.ticks import expand_all_ticks
from poweragent.eval.aggregate import rank, worst_case
from poweragent.store.repo import Store

__all__ = [
    "UNAVAILABLE",
    "ReportContext",
    "ResultHashUnavailableError",
    "build_context",
    "compute_result_hash",
    "format_value",
]

# 缺数据时统一渲染的标记。AC7 要求三类埋点任一未落库即标注"不可用"，且不估算、
# 不填默认值、不插值、不取其他任务的值。用一个常量而不是各处写字面量，是为了让
# "报告里出现了不可用"这件事可以被一条 grep 查全。
UNAVAILABLE = "不可用"

# 过程量字段名。它们是 `runs` 表的派生量，按 R23 AC7 记为日志字段、不作为结论指标；
# `test_report_wording.py` 断言这些键不出现在 `ReportContext.conclusion` 中。
PROCESS_METRIC_KEYS = frozenset(
    {"first_feasible_round", "feasible_rate", "duplicate_rate"}
)


def format_value(value: object) -> str:
    """报告内数值的唯一格式化入口：`format(v, ".4g")`（design.md §5.3.1）。

    占位段的数值子集判定与正文渲染必须共用同一个函数（R18 AC5）。若两侧各自
    格式化，同一个量会写出两种文本（`5.2` 与 `5.200`），子集判定便会把正文里
    确实存在的数值判成"新引入的数字"。

    `None` 走 `UNAVAILABLE` 而不是 `"None"`：报告里出现 `None` 会被读成一个取值，
    而它表达的是"这个量没有"。
    """
    if value is None:
        return UNAVAILABLE
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return format(value, ".4g")
    return str(value)


@dataclass(frozen=True, slots=True)
class ReportContext:
    """报告的全部渲染输入。

    分成多个映射而不是一个扁平字典，是为了让"哪些键能被哪一段看到"成为类型层面
    可检查的事实（见模块 docstring）。
    """

    task_id: str
    conclusion: Mapping[str, Any]
    """结论段专用。过程量不得进入，由 `test_report_wording.py` 断言。"""

    task: Mapping[str, Any]
    config_binding: Mapping[str, Any]
    baseline: Mapping[str, Any]
    scenarios: Sequence[Mapping[str, Any]]
    top_candidates: Sequence[Mapping[str, Any]]
    hard_constraints: Sequence[Mapping[str, Any]]
    comparison: Mapping[str, Any]
    telemetry: Mapping[str, Any]
    """过程埋点摘要段。过程量在这里，这是它们被允许出现的地方。"""

    no_feasible: Mapping[str, Any] | None
    """无可行候选时的五项最小内容（design.md §11.1）；有可行候选时为 `None`。"""


def _fetch_task_row(store: Store, task_id: str) -> Mapping[str, Any]:
    row = store.connection.execute(
        "SELECT simulation_only, task_kind, model_package_hash, metrics_hash, "
        "       constraints_hash, scenario_set_hash, execution_env_hash, "
        "       calibration_hash, budget_max_starts, started_at, ended_at, "
        "       stop_reason, cause "
        "FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"build_context: task_id={task_id!r} 不在 tasks 表中")
    keys = (
        "simulation_only", "task_kind", "model_package_hash", "metrics_hash",
        "constraints_hash", "scenario_set_hash", "execution_env_hash",
        "calibration_hash", "budget_max_starts", "started_at", "ended_at",
        "stop_reason", "cause",
    )
    return dict(zip(keys, row))


def _check_config_binding(
    task_row: Mapping[str, Any],
    *,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
) -> Mapping[str, Any]:
    """当前配置的哈希是否与任务落库时的哈希一致。

    不一致不是错误、也不阻止渲染：任务跑完之后改配置是常事。但那时从配置读出的
    `domain`/`ticks` 与单位就不再是当时那一套，凡依赖它们的段落必须标注"不可用"，
    而不是用现在的值冒充当时的值。
    """
    current_metrics = metrics_hash(metrics_cfg.model_dump(mode="json"))
    current_constraints = constraints_hash(constraints_cfg.model_dump(mode="json"))
    metrics_match = current_metrics == task_row["metrics_hash"]
    constraints_match = current_constraints == task_row["constraints_hash"]
    return {
        "metrics_match": metrics_match,
        "constraints_match": constraints_match,
        "all_match": metrics_match and constraints_match,
        "current_metrics_hash": current_metrics,
        "current_constraints_hash": current_constraints,
    }


def _fetch_scenarios(store: Store, task_id: str) -> list[Mapping[str, Any]]:
    rows = store.connection.execute(
        "SELECT scenario_id, tier, model_variant, require_margin, spec_json, "
        "       spec_version "
        "FROM scenario_set WHERE task_id=? ORDER BY tier, scenario_id",
        (task_id,),
    ).fetchall()
    return [
        {
            "scenario_id": scenario_id,
            "tier": tier,
            "model_variant": model_variant,
            "require_margin": bool(require_margin),
            "spec": json.loads(spec_json),
            "spec_version": spec_version,
        }
        for scenario_id, tier, model_variant, require_margin, spec_json, spec_version
        in rows
    ]


def _fetch_candidate_parameters(store: Store, candidate_id: str) -> Mapping[str, float]:
    row = store.connection.execute(
        "SELECT parameters_si FROM candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    return json.loads(row[0]) if row else {}


def _fetch_metrics_for_candidate(
    store: Store, task_id: str, candidate_id: str
) -> list[Mapping[str, Any]]:
    """该候选在评价层各场景上的逐项指标。

    只取 `tier='evaluation'` 的场景行：报告的 Top 名次与 worst-case 取值都定义在
    评价集上，把筛选层的指标混进同一张表会让"这个候选的恢复时间是多少"出现两个
    互相矛盾的答案。
    """
    rows = store.connection.execute(
        "SELECT r.scenario_id, m.metric_id, m.value, m.valid, m.invalid_reason "
        "FROM runs r "
        "JOIN metric_results m ON m.run_id = r.run_id "
        "JOIN scenario_set s ON s.task_id = r.task_id AND s.scenario_id = r.scenario_id "
        "WHERE r.task_id=? AND r.candidate_id=? AND s.tier='evaluation' "
        "ORDER BY r.scenario_id, m.metric_id",
        (task_id, candidate_id),
    ).fetchall()
    return [
        {
            "scenario_id": scenario_id,
            "metric_id": metric_id,
            "value": value,
            "valid": bool(valid),
            "invalid_reason": invalid_reason,
        }
        for scenario_id, metric_id, value, valid, invalid_reason in rows
    ]


def _fetch_constraint_rows(
    store: Store, task_id: str, candidate_id: str
) -> list[Mapping[str, Any]]:
    rows = store.connection.execute(
        "SELECT cr.scenario_id, cr.feasible, cr.violations "
        "FROM constraint_results cr "
        "JOIN runs r ON r.run_id = cr.run_id "
        "JOIN scenario_set s ON s.task_id = r.task_id AND s.scenario_id = r.scenario_id "
        "WHERE r.task_id=? AND cr.candidate_id=? AND s.tier='evaluation' "
        "ORDER BY cr.scenario_id",
        (task_id, candidate_id),
    ).fetchall()
    return [
        {
            "scenario_id": scenario_id,
            "feasible": bool(feasible),
            "violations": json.loads(violations),
        }
        for scenario_id, feasible, violations in rows
    ]


def _hard_constraint_rows(
    constraints_cfg: ConstraintsConfig,
    metrics_cfg: MetricsConfig,
    *,
    trustworthy: bool,
) -> list[Mapping[str, Any]]:
    """硬约束逐条的阈值与阈值来源。

    `threshold_source` 取自 `metrics.yaml` 对应指标/观测量的同名字段——阈值本身
    在 `constraints.yaml`，而"这个阈值凭什么是这个数"记在指标侧。两处分开是有意的，
    报告把它们并排列出。
    """
    if not trustworthy:
        return []

    hard = constraints_cfg.hard_constraints
    # `by_alias=True` 是必需的：`constraint_observables` 的 YAML 键是 `obs.vout_min`
    # （声明为 pydantic 字段别名），而 `hard_constraints.<name>.observable` 写的正是
    # 这个带前缀的形式。按属性名 dump 会得到 `vout_min`，于是每一条观测量类约束都
    # 查不到单位、被标成"不可用"——一处静默的错位，报告照样渲染得出来。
    metrics_dump = metrics_cfg.model_dump(mode="json", by_alias=True)
    metric_specs = metrics_dump.get("metrics") or {}
    observable_specs = metrics_dump.get("constraint_observables") or {}

    rows: list[Mapping[str, Any]] = []
    for name in type(hard).model_fields:
        entry = getattr(hard, name)
        spec = observable_specs.get(entry.observable) or metric_specs.get(
            entry.observable
        ) or {}
        rows.append(
            {
                "name": name,
                "value": entry.value,
                "observable": entry.observable,
                "sense": entry.sense,
                "applies_to_tier": list(entry.applies_to_tier),
                "unit": spec.get("unit", UNAVAILABLE),
                # `constraint_observables` 的条目没有 `threshold_source` 字段——那是
                # `metrics` 节指标才有的。对观测量类约束渲染"—"而不是"不可用"：
                # 前者说"这一项不适用"，后者说"这一项本该有但取不到"，混用会让读者
                # 去找一份并不存在的阈值依据。
                "threshold_source": spec.get(
                    "threshold_source", "—" if spec else UNAVAILABLE
                ),
            }
        )
    return rows


def _fetch_rejection_counts(store: Store, task_id: str) -> Mapping[str, int]:
    """被拒候选按 `reason` 计数（design.md §11.1 第 4 项）。

    按冒号前的类别归并：`reason` 形如 `off_tick:rcomp`，逐个原样计数会把同一类
    问题拆成多行，而这一项的用途正是分辨"候选大量落在域外"（Agent 的问题）、
    "大量 off_tick"（档位或 prompt 的问题）与"大量约束违反"（约束或合法域的问题）
    这三者——它们的下一步动作完全不同。
    """
    rows = store.connection.execute(
        "SELECT reason FROM rejections WHERE task_id=?", (task_id,)
    ).fetchall()
    return dict(sorted(Counter(row[0].split(":", 1)[0] for row in rows).items()))


def _fetch_run_stats(store: Store, task_id: str) -> Mapping[str, Any]:
    row = store.connection.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), "
        "       SUM(cache_hit) "
        "FROM runs WHERE task_id=?",
        (task_id,),
    ).fetchone()
    total, done, failed, cache_hits = (v or 0 for v in row)
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "cache_hits": cache_hits,
    }


def _fetch_llm_stats(store: Store, task_id: str) -> Mapping[str, Any]:
    rows = store.connection.execute(
        "SELECT outcome, COUNT(*), SUM(tokens) FROM llm_calls WHERE task_id=? "
        "GROUP BY outcome ORDER BY outcome",
        (task_id,),
    ).fetchall()
    if not rows:
        # AC7：埋点未落库即标"不可用"。区分"没有调用过"与"调用了但没记"在这里
        # 做不到，两者都表现为无行——但两者都意味着这一段无据可写，处理相同。
        return {"available": False, "calls": 0, "tokens": UNAVAILABLE, "by_outcome": {}}
    return {
        "available": True,
        "calls": sum(count for _outcome, count, _tokens in rows),
        "tokens": sum(tokens or 0 for _outcome, _count, tokens in rows),
        "by_outcome": {outcome: count for outcome, count, _tokens in rows},
    }


def _process_metrics(store: Store, task_id: str) -> Mapping[str, Any]:
    """三个过程量。只允许出现在 `telemetry`，见模块 docstring。"""
    first_feasible = store.connection.execute(
        "SELECT MIN(c.round_index) FROM candidates c "
        "JOIN constraint_results cr ON cr.candidate_id = c.candidate_id "
        "WHERE c.task_id=? AND cr.feasible=1",
        (task_id,),
    ).fetchone()[0]

    counts = store.connection.execute(
        "SELECT COUNT(DISTINCT c.candidate_id), "
        "       COUNT(DISTINCT CASE WHEN cr.feasible=1 THEN c.candidate_id END) "
        "FROM candidates c "
        "LEFT JOIN constraint_results cr ON cr.candidate_id = c.candidate_id "
        "WHERE c.task_id=?",
        (task_id,),
    ).fetchone()
    total_candidates, feasible_candidates = (v or 0 for v in counts)

    proposed = total_candidates + len(
        store.connection.execute(
            "SELECT 1 FROM rejections WHERE task_id=?", (task_id,)
        ).fetchall()
    )

    return {
        "first_feasible_round": first_feasible if first_feasible is not None else UNAVAILABLE,
        "feasible_rate": (
            feasible_candidates / total_candidates if total_candidates else UNAVAILABLE
        ),
        "duplicate_rate": (
            (proposed - total_candidates) / proposed if proposed else UNAVAILABLE
        ),
    }


def _build_conclusion(
    task_row: Mapping[str, Any],
    ranked_rows: Sequence[Mapping[str, Any]],
    task_cfg: TaskConfig,
) -> Mapping[str, Any]:
    """结论段上下文。

    只放"结论边界"所需的事实：轨道标记、停止原因、是否存在可行候选、第一名候选的
    参数与主目标取值、达标线。不放任何过程量（模块 docstring 说明了为什么这是结构
    性要求）。

    也不放"是否采纳"这类项目级结论——R18 AC6 明确报告不渲染达标线判定与采纳/否决
    结论，那是 Checkpoint 3 的人工决定，报告只提供依据。
    """
    best = ranked_rows[0] if ranked_rows else None
    target = (
        task_cfg.objective_target.target_value
        if task_cfg.objective_target is not None
        else None
    )
    return {
        "simulation_only": bool(task_row["simulation_only"]),
        "task_kind": task_row["task_kind"],
        "stop_reason": task_row["stop_reason"] or UNAVAILABLE,
        "in_progress": task_row["stop_reason"] is None,
        "cause": task_row["cause"],
        "has_feasible_candidate": best is not None,
        "best_candidate_id": best["candidate_id"] if best else None,
        "best_parameters": best["parameters"] if best else {},
        "best_objective": best["worst_case_value"] if best else None,
        "objective_metric_id": best["objective_metric_id"] if best else None,
        "objective_target": target,
    }


def _build_no_feasible(
    store: Store,
    task_id: str,
    task_row: Mapping[str, Any],
    constraints_cfg: ConstraintsConfig,
    hard_rows: Sequence[Mapping[str, Any]],
    scenarios: Sequence[Mapping[str, Any]],
    *,
    trustworthy: bool,
) -> Mapping[str, Any]:
    """无可行候选时的五项最小内容（design.md §11.1 / L23-3）。

    合法域边界列在首位而不是把硬约束阈值列在首位（R18 AC9）：Baseline 门禁曾通过
    意味着这套约束与模型能产出至少一个可行解，那么"整个域里没有一个点可行"更可能
    是域划错了，而不是阈值定错了。首位放什么决定了读报告的人先怀疑什么。
    """
    domain_rows: list[Mapping[str, Any]] = []
    if trustworthy:
        ticks = expand_all_ticks(constraints_cfg)
        for name, spec in constraints_cfg.design_space.variables.model_dump().items():
            variable_ticks = ticks.get(name, ())
            domain_rows.append(
                {
                    "name": name,
                    "unit": spec.get("unit", UNAVAILABLE),
                    "domain_low": spec["domain"][0],
                    "domain_high": spec["domain"][1],
                    "tick_first": variable_ticks[0] if variable_ticks else UNAVAILABLE,
                    "tick_last": variable_ticks[-1] if variable_ticks else UNAVAILABLE,
                    "tick_count": len(variable_ticks),
                }
            )

    return {
        "legal_domain": domain_rows,
        "legal_domain_available": trustworthy,
        "hard_constraints": list(hard_rows),
        "scenario_set_hash": task_row["scenario_set_hash"],
        "scenarios": list(scenarios),
        "rejection_counts": _fetch_rejection_counts(store, task_id),
        "budget_used": store.sum_budget_units(task_id),
        "budget_max": task_row["budget_max_starts"],
    }


def build_context(
    task_id: str,
    store: Store,
    *,
    task_cfg: TaskConfig,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
) -> ReportContext:
    """把一个任务的落库事实组装成渲染上下文。

    签名相对 design.md §6.10 的字面形式 `build_context(task_id, store)` 多三个
    kwonly 配置参数。理由：AC12 要求指标单位取自 `metrics.yaml`、约束单位取自
    `constraints.yaml`，而这两处内容不在数据库里（`tasks` 只存它们的哈希）；
    `eval.aggregate.rank()` 也需要 `metrics_cfg` 才能定序。让本函数自己去
    `load_all()` 是更差的选择——那样渲染结果会随进程的当前工作目录变化，而配置
    从哪来应当由调用方明示。

    可行性与 Evaluation 集完备性的过滤全部由 `rank()` 承担，本函数不重做
    （`WORST_CASE_SQL` 的 `HAVING` 已经做过一遍，再筛一次就有了两个判据）。
    """
    task_row = _fetch_task_row(store, task_id)
    config_binding = _check_config_binding(
        task_row, metrics_cfg=metrics_cfg, constraints_cfg=constraints_cfg
    )
    trustworthy = bool(config_binding["all_match"])

    scenarios = _fetch_scenarios(store, task_id)
    hard_rows = _hard_constraint_rows(
        constraints_cfg, metrics_cfg, trustworthy=trustworthy
    )

    objective_metric_id = metrics_cfg.objective.primary.metric_id
    ranked_rows = [
        {
            "rank": entry.rank,
            "candidate_id": entry.candidate_id,
            "parameters": _fetch_candidate_parameters(store, entry.candidate_id),
            "worst_case_value": entry.worst_case_value,
            "objective_metric_id": objective_metric_id,
            "secondary_values": dict(entry.secondary_values),
            "metrics": _fetch_metrics_for_candidate(store, task_id, entry.candidate_id),
            "constraints": _fetch_constraint_rows(store, task_id, entry.candidate_id),
        }
        for entry in rank(store, task_id, metrics_cfg, top_n=3)
    ]

    # AC4：可行候选少于三个时按实际数量渲染，缺位名次标为不可用。名次的占位在这里
    # 生成而不是留给模板判断，模板只负责把它显示出来。
    top_candidates = list(ranked_rows) + [
        {"rank": missing, "available": False}
        for missing in range(len(ranked_rows) + 1, 4)
    ]
    for row in ranked_rows:
        row["available"] = True  # type: ignore[index]

    baseline_row = store.find_baseline(
        task_row["scenario_set_hash"]  # 见下方注释：这不是 gate_key
    )
    # `baselines.gate_key` 是四个哈希的 H，本函数手上没有那个组合值的计算路径
    # （它由 `controller.preflight` 在门禁时算出）。用 scenario_set_hash 查必然
    # 落空——这不是 bug 而是"这份数据在本任务的库里不可得"，按 AC7 标不可用，
    # 不去猜一个 gate_key，也不把某个恰好存在的基线拿来充数。
    baseline = (
        {
            "available": True,
            "passed": baseline_row.passed,
            "frozen_result_hash": baseline_row.frozen_result_hash,
            "frozen_at": baseline_row.frozen_at,
        }
        if baseline_row is not None
        else {"available": False}
    )

    run_stats = _fetch_run_stats(store, task_id)
    telemetry = {
        "interventions": dict(store.count_interventions_by_checkpoint(task_id)),
        "started_at": task_row["started_at"] or UNAVAILABLE,
        "ended_at": task_row["ended_at"] or UNAVAILABLE,
        "llm": _fetch_llm_stats(store, task_id),
        "runs": run_stats,
        "budget_used": store.sum_budget_units(task_id),
        "budget_max": task_row["budget_max_starts"],
        "budget_granted": store.sum_approved_budget_increase(task_id),
        "rejection_counts": _fetch_rejection_counts(store, task_id),
        **_process_metrics(store, task_id),
    }

    comparison = {
        "baseline": baseline,
        "best": ranked_rows[0] if ranked_rows else None,
        "objective_metric_id": objective_metric_id,
        "objective_direction": metrics_cfg.objective.primary.direction,
        "objective_target": (
            task_cfg.objective_target.target_value
            if task_cfg.objective_target is not None
            else None
        ),
    }

    return ReportContext(
        task_id=task_id,
        conclusion=_build_conclusion(task_row, ranked_rows, task_cfg),
        task={
            **task_row,
            "simulation_only": bool(task_row["simulation_only"]),
            "calibration_hash": task_row["calibration_hash"] or UNAVAILABLE,
        },
        config_binding=config_binding,
        baseline=baseline,
        scenarios=scenarios,
        top_candidates=top_candidates,
        hard_constraints=hard_rows,
        comparison=comparison,
        telemetry=telemetry,
        no_feasible=(
            None
            if ranked_rows
            else _build_no_feasible(
                store,
                task_id,
                task_row,
                constraints_cfg,
                hard_rows,
                scenarios,
                trustworthy=trustworthy,
            )
        ),
    )


# ===========================================================================
# compute_result_hash()：Checkpoint 3 审批与 apply 前比对所绑定的结果哈希
#
# design.md §5.3.1「`result_hash` 的重算范围」/ §7.6 / §8.10；CP-10。
#
# ## 为什么落在本模块
#
# `cli.py` 的 `_compute_current_result_hash()` 此前是一个 `raise
# NotImplementedError` 的桩，其 docstring 写明"这是报告聚合层
# （`report/render.py` / `eval/aggregate.py`）的职责，均为远期波次的任务"。
# 那两处现已落地，桩的阻塞理由不再成立。9 个键里有 7 个的数据源本模块已经为
# 报告渲染各查过一遍：
#
#   parameters_si            _fetch_candidate_parameters()
#   per_scenario             _fetch_metrics_for_candidate()
#   constraints              _fetch_constraint_rows()
#   四个哈希                  _fetch_task_row()
#
# 在 `cli.py` 里重写这四段 SQL 会引入两处必须逐字符保持同步的联表查询，而它们
# 的语义有讲究（只取 `tier='evaluation'`：worst-case 与 Top 名次都定义在评价集
# 上，混入筛选层会让同一个候选的同一个指标出现两个值）。复用比重写安全。
#
# ## 四个哈希取自 `tasks` 行，不由当前配置重算
#
# `tasks` 表的六个哈希是"这次任务用的是哪个版本的什么东西"的权威记录。若改为
# 从当前 `configs/` 重算，同一个 `task_id` 在两个工作目录下会算出不同的
# `result_hash`，而库内状态一个字节都没变——那样这个哈希就不再是"绑定这份结果"
# 而是"绑定这台机器此刻的配置"。
#
# ## 配置漂移在这里是错误，而报告渲染时不是
#
# `_check_config_binding()` 对同一件事的处理是"不一致不阻止渲染，只把依赖当前
# 配置的段落标为不可用"——因为任务跑完之后改配置是常事，而报告的用途是让人读。
# 审批不能这样处理，原因是 `worst_case` 这一项**必须**用当前加载的
# `metrics_cfg` 才算得出来：`objective.primary.metric_id` 只存在于
# `metrics.yaml`，库里只有它的哈希。于是有两种可能：
#
#   一致  ⟹ 当前配置就是当初那份，`worst_case` 与库内四个哈希自洽；
#   不一致 ⟹ 用另一份配置的 `primary.metric_id` 去聚合库内数据，算出的
#            `worst_case` 与 `tasks` 行记录的 `metrics_hash` 描述的不是同一件
#            事，拼进同一个 payload 得到的哈希没有可解释的含义。
#
# 第二种情况下唯一诚实的行为是拒绝并说明，而不是产出一个"能通过但含义模糊"的
# 哈希——`approvals.result_hash` 的全部价值在于 `apply` 时重算比对能挡住误签，
# 一个含义模糊的值会让这道闸门看起来在工作而实际不在。
#
# ## `per_scenario` 必须投影掉 `invalid_reason`
#
# design.md §5.3.1 把 `per_scenario` 的行形状定为恰好
# `{scenario_id, metric_id, value, valid}` 四键，而
# `_fetch_metrics_for_candidate()` 为报告多带了一个 `invalid_reason`。
# `result_hash()` 只忽略 **payload 顶层**的多余键，行内的多余键会照样进
# `canonical_json`。不投影就会算出一个与 design.md 定义不同的哈希——而且这个
# 偏差不会报错，只会让"重算比对"在两个都自称正确的实现之间永远不一致。
# ===========================================================================


class ResultHashUnavailableError(RuntimeError):
    """无法为某个候选重算 `result_hash`，因此不能对它签审批。

    三种触发情形（都不是"稍后重试就会好"的瞬时问题，而是"这次审批本身不该
    发生"）：

    - 当前 `metrics.yaml` / `constraints.yaml` 的哈希与 `tasks` 行记录的不一致
      （配置漂移，见模块内 `compute_result_hash` 上方一节）。
    - `candidate_id` 不在 `candidates` 表中。
    - `candidate_id` 未通过 worst-case 聚合的完备性/可行性过滤：它要么有场景
      没跑完，要么在某个评价场景上不可行。这样的候选没有"最终结果"可绑定，
      审批它是无意义的。
    """


def compute_result_hash(
    task_id: str,
    store: Store,
    candidate_id: str,
    *,
    metrics_cfg: MetricsConfig,
    constraints_cfg: ConstraintsConfig,
) -> str:
    """从库内当前状态为 `candidate_id` 重算 `result_hash`（9 键范围见
    `config.hashing.result_hash()`）。

    参数顺序与 `build_context()` 一致（`task_id` 在前、`store` 次之、配置以
    kwonly 显式传入），本函数同为纯读函数：不写任何表、不落任何文件。

    不可用时抛 `ResultHashUnavailableError`，由 `cli.cmd_report` 转译为
    `click.UsageError`（退出码 1），不写 `approvals` 行。
    """
    task_row = _fetch_task_row(store, task_id)

    current_metrics = metrics_hash(metrics_cfg.model_dump(mode="json"))
    current_constraints = constraints_hash(constraints_cfg.model_dump(mode="json"))
    drifted = [
        name
        for name, current, recorded in (
            ("metrics_hash", current_metrics, task_row["metrics_hash"]),
            ("constraints_hash", current_constraints, task_row["constraints_hash"]),
        )
        if current != recorded
    ]
    if drifted:
        raise ResultHashUnavailableError(
            f"配置已漂移，无法为 task_id={task_id!r} 的候选重算 result_hash："
            f"{', '.join(drifted)} 与 tasks 行记录的取值不一致。"
            "审批必须绑定任务当初那份配置下的结果；"
            "请改回当初的配置，或对当前配置重跑一次任务。"
        )

    parameters_si = _fetch_candidate_parameters(store, candidate_id)
    if not parameters_si:
        raise ResultHashUnavailableError(
            f"candidate_id={candidate_id!r} 不在 candidates 表中"
        )

    worst_by_candidate = worst_case(store, task_id, metrics_cfg)
    if candidate_id not in worst_by_candidate:
        raise ResultHashUnavailableError(
            f"candidate_id={candidate_id!r} 未通过 worst-case 聚合的完备性过滤："
            "它或有评价场景未跑完，或在某个评价场景上不可行，"
            "没有可供审批绑定的最终结果。"
        )

    # design.md §5.3.1 的行形状恰为四键；`invalid_reason` 是报告用的额外列，
    # 留在行内会进 canonical_json（见模块内上方"必须投影掉"一节）。
    per_scenario = [
        {
            "scenario_id": row["scenario_id"],
            "metric_id": row["metric_id"],
            "value": row["value"],
            "valid": row["valid"],
        }
        for row in _fetch_metrics_for_candidate(store, task_id, candidate_id)
    ]

    return result_hash(
        {
            "candidate_id": candidate_id,
            "parameters_si": parameters_si,
            "worst_case": worst_by_candidate[candidate_id],
            "per_scenario": per_scenario,
            "constraints": _fetch_constraint_rows(store, task_id, candidate_id),
            "model_package_hash": task_row["model_package_hash"],
            "constraints_hash": task_row["constraints_hash"],
            "metrics_hash": task_row["metrics_hash"],
            "scenario_set_hash": task_row["scenario_set_hash"],
        }
    )
