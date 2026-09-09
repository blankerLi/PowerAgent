"""从配置与 SQLite 重建提案器的只读输入。

这是确定性域喂给 LLM 域的那一层。它属于 `controller` 而不是 `agent`：状态由谁构造，
决定了提案器能看到什么，而那正是权限边界所在。提案器自己不能去查数据库，否则"只读
输入"就只是一句约定而非结构性保证。

每轮都完整重建，不做增量
------------------------
每一轮从事实源重新查一遍，而不是在内存里累积。代价是每轮几条 SQL，换来的是"进程
中断后续跑，提案器看到的输入与未中断时一致"这条性质——它同时也是断点续跑与结果可
复现的基础。增量维护一份内存状态会引入"内存与数据库不一致"这一整类问题，而那类
问题不会报错，只会让某一轮的提案基于过期信息。

特征取自评价层，退化到筛选层
----------------------------
一个候选可能有多条 run：筛选层与评价层各一条。prompt 里应当反映**最恶劣工况**，
因此优先取评价层的指标。被筛选层提前拒绝的候选没有评价层数据，此时退回筛选层——
这类候选对模型仍有价值（知道它为什么在初筛就被拦下），留空反而丢掉了信息。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Mapping, Sequence

from poweragent.agent.state import (
    ConstraintSpec,
    DomainSpec,
    FailedRegion,
    SearchState,
    TestedPoint,
)
from poweragent.agent.prompt import compress_waveform
from poweragent.config.schema import ConstraintsConfig, MetricsConfig
from poweragent.config.ticks import expand_all_ticks
from poweragent.controller.stop import BestRecord
from poweragent.eval.aggregate import rank
from poweragent.store.repo import MetricResult, Store

__all__ = ["build_search_state", "legal_domain_from_config", "hard_constraints_from_config"]

# 取全部候选用于排序时的上限。设计空间只有 144 个网格点，而一次搜索的预算是 200 次
# 仿真调用，因此候选数不会接近这个上限；给一个显式值而不是依赖"不传就返回全部"的
# 隐含语义。
_RANK_LIMIT = 10_000


def legal_domain_from_config(
    constraints_cfg: ConstraintsConfig,
) -> dict[str, DomainSpec]:
    """从配置构造合法域，档位在此处展开。

    展开发生在确定性域：提案器看到的是最终的档位取值列表，而不是 `{count, spacing}`
    这样的简写。让模型自己按 count 与 spacing 推算档位，等于把一个确定性计算交给它猜，
    而算错的后果是整批候选偏离档位被拒。
    """
    ticks = expand_all_ticks(constraints_cfg)
    variables = constraints_cfg.design_space.variables

    domain: dict[str, DomainSpec] = {}
    for name in ticks:
        spec = getattr(variables, name)
        domain[name] = DomainSpec(
            unit=spec.unit,
            low=spec.domain[0],
            high=spec.domain[1],
            scale=spec.scale,
            ticks=ticks[name],
        )
    return domain


def _unit_of_observable(observable: str, metrics_cfg: MetricsConfig) -> str:
    """查出一个约束支撑观测量的单位。

    单位不在 `constraints.yaml` 里——那里每条硬约束只有阈值、观测量名、方向与适用层。
    单位属于观测量本身，登记在 `metrics.yaml`：带 `obs.` 前缀的落在
    `constraint_observables`，其余落在 `metrics`。在这里查而不是在约束配置里重复一份，
    避免两处对同一个量给出不同单位。
    """
    if observable.startswith("obs."):
        return getattr(metrics_cfg.constraint_observables, observable[4:]).unit
    return getattr(metrics_cfg.metrics, observable).unit


def hard_constraints_from_config(
    constraints_cfg: ConstraintsConfig, metrics_cfg: MetricsConfig
) -> dict[str, ConstraintSpec]:
    """构造带方向与单位的硬约束描述。

    方向（`sense`）必须带上：只给模型一个 `0.75` 无法判断那是下限还是上限，而"输出
    电压不低于 0.75 V"与"不高于 0.75 V"是完全相反的要求。少这一项，提案质量会系统性
    变差，而原因很难从输出上看出来。
    """
    hard = constraints_cfg.hard_constraints
    out: dict[str, ConstraintSpec] = {}
    for name in type(hard).model_fields:
        entry = getattr(hard, name)
        out[name] = ConstraintSpec(
            value=entry.value,
            sense=entry.sense,
            unit=_unit_of_observable(entry.observable, metrics_cfg),
        )
    return out


def _metrics_for_candidate(
    store: Store, task_id: str, candidate_id: str
) -> list[MetricResult]:
    """取一个候选的指标，优先评价层、退化到筛选层（见模块 docstring）。"""
    query = """
        SELECT m.metric_id, m.value, m.valid, m.invalid_reason, m.run_id
        FROM metric_results m
        JOIN runs r ON r.run_id = m.run_id
        JOIN scenario_set s
          ON s.task_id = r.task_id AND s.scenario_id = r.scenario_id
        WHERE r.task_id = ? AND r.candidate_id = ? AND s.tier = ?
        ORDER BY r.attempt DESC
    """
    for tier in ("evaluation", "screening"):
        rows = store.connection.execute(query, (task_id, candidate_id, tier)).fetchall()
        if rows:
            return [
                MetricResult(
                    run_id=row[4],
                    metric_id=row[0],
                    value=row[1],
                    valid=bool(row[2]),
                    invalid_reason=row[3],
                )
                for row in rows
            ]
    return []


def _verdict_for_candidate(
    store: Store, task_id: str, candidate_id: str
) -> tuple[bool, tuple[str, ...]]:
    """取一个候选的可行性与违反的约束名。

    一个候选在多个场景各有一条判定，全部可行才算可行——硬约束按最恶劣场景判定。
    没有任何判定行时视为不可行：没有证据不等于通过。
    """
    rows = store.connection.execute(
        """
        SELECT cr.feasible, cr.violations
        FROM constraint_results cr
        JOIN runs r ON r.run_id = cr.run_id
        WHERE r.task_id = ? AND cr.candidate_id = ?
        """,
        (task_id, candidate_id),
    ).fetchall()

    if not rows:
        return False, ()

    feasible = all(bool(row[0]) for row in rows)
    names: list[str] = []
    for _, violations_json in rows:
        for item in json.loads(violations_json or "[]"):
            name = item.get("constraint")
            if name and name not in names:
                names.append(name)
    return feasible, tuple(names)


def _tested_candidates(
    store: Store, task_id: str, metrics_cfg: MetricsConfig
) -> tuple[TestedPoint, ...]:
    """重建已评估候选列表。

    目标值取自 `eval.aggregate.rank()`——它按最恶劣场景聚合并施加完备性过滤，是排序的
    唯一权威实现。不在这里重算：重算一遍就出现了两个可能不一致的目标值定义，而报告与
    提案会各引用一个。

    `rank()` 只返回评价集完备的候选，因此被提前拒绝或数据不全的候选目标值留 `None`。
    它们仍然进列表——"这个点试过且不可行"对模型是有用信息，漏掉会让它反复提相邻的点。
    """
    objective_by_id = {
        item.candidate_id: item.worst_case_value
        for item in rank(store, task_id, metrics_cfg, top_n=_RANK_LIMIT)
    }

    rows = store.connection.execute(
        "SELECT candidate_id, parameters_si FROM candidates "
        "WHERE task_id = ? ORDER BY round_index, created_at",
        (task_id,),
    ).fetchall()

    points: list[TestedPoint] = []
    for candidate_id, parameters_json in rows:
        metrics = _metrics_for_candidate(store, task_id, candidate_id)
        if not metrics:
            # 候选已落库但还没有任何完成的 run（例如本轮刚提出、尚未仿真）。
            # 不算"已测"，否则模型会以为这个点的结果已知。
            continue

        feasible, violations = _verdict_for_candidate(store, task_id, candidate_id)
        points.append(
            TestedPoint(
                candidate_id=candidate_id,
                parameters_si=json.loads(parameters_json),
                feasible=feasible,
                objective_value=objective_by_id.get(candidate_id),
                features=compress_waveform(metrics),
                violations=violations,
            )
        )
    return tuple(points)


def _failed_regions(store: Store, task_id: str) -> tuple[FailedRegion, ...]:
    """从 `rejections` 归纳失败区域。

    按拒绝原因的**类别**分组（冒号前的部分），而不是完整原因字符串：
    `low_novelty:1.0000` 与 `low_novelty:0.5000` 是同一类问题，分成两组只会让区域
    碎片化，对模型反而是噪声。

    区域用各维度的包络（最小值到最大值）表示，而不是聚类。包络会把分散的点圈成一个
    过大的框，但对"别再往这片区域提点"这个用途足够；引入聚类需要选算法与参数，而那
    些选择都会影响提案，却没有客观依据可循。
    """
    rows = store.connection.execute(
        "SELECT reason, raw_parameters FROM rejections WHERE task_id = ?",
        (task_id,),
    ).fetchall()

    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for reason, raw_json in rows:
        category = reason.split(":", 1)[0]
        try:
            grouped[category].append(json.loads(raw_json))
        except json.JSONDecodeError:
            continue

    regions: list[FailedRegion] = []
    for category, items in sorted(grouped.items()):
        keys = sorted({key for item in items for key in item})
        bounds: dict[str, tuple[float, float]] = {}
        for key in keys:
            values = [
                float(item[key])
                for item in items
                if key in item and isinstance(item[key], (int, float))
            ]
            if values:
                bounds[key] = (min(values), max(values))
        if bounds:
            regions.append(
                FailedRegion(
                    bounds=bounds, failure_type=category, sample_count=len(items)
                )
            )
    return tuple(regions)


def build_search_state(
    store: Store,
    task_id: str,
    *,
    constraints_cfg: ConstraintsConfig,
    metrics_cfg: MetricsConfig,
    remaining_budget: int,
    current_best: BestRecord | None,
) -> SearchState:
    """从配置与事实源重建提案器的完整只读输入。

    `remaining_budget` 与 `current_best` 由调用方传入而不是在这里查：主循环已经持有
    预算账本与本轮的最佳记录，重新查一遍会得到同一个值，却多了两者可能不一致的可能。
    这与本代码库的既有惯例一致——函数接收它实际需要的量，而不是接收一个能推导出这些
    量的大对象。
    """
    return SearchState(
        legal_domain=legal_domain_from_config(constraints_cfg),
        hard_constraints=hard_constraints_from_config(constraints_cfg, metrics_cfg),
        current_best=current_best,
        tested_candidates=_tested_candidates(store, task_id, metrics_cfg),
        failed_regions=_failed_regions(store, task_id),
        remaining_budget=remaining_budget,
    )
