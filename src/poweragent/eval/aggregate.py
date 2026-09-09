"""poweragent/eval/aggregate.py

worst-case 聚合、排序键与排序（`design.md` §5.4 / §6.5.3；`tasks.md` 任务 12.2 /
12.3；需求追溯 R9.5, R9.6, R9.7, R9.12, R1.2, R1.3, R1.4, R9.8, R9.9, R9.11；
属性 CP-6）。

## `WORST_CASE_SQL` 的落点

SQL 常量的权威定义落在 `poweragent.store.repo`（与 `Store.query_worst_case()`
同一模块，紧邻其唯一调用点），本模块从该处 `import` 并重新导出，而不是重新
抄写一份文本——`design.md` §5.4 的 SQL 是本文件与 `store/repo.py` 之间唯一需要
逐字符保持同步的内容，两处各写一份会造成文本漂移的风险。`WORST_CASE_SQL` 字符
串本身与 `design.md` §5.4 逐字符相同（含注释与 CTE 命名）。

## `worst_case()` 不做任何 Python 侧补全

`HAVING COUNT(DISTINCT scenario_id) = (SELECT COUNT(*) FROM eval_set)` 已经是
「任一 Evaluation 场景缺失即不可行」的全部实现——SQL 返回的每一行都已经是满足
完备条件的候选，本函数只需要把三元组行重塑为 `dict[str, float]`：

- 不对 `store.query_worst_case()` 的返回结果做任何进一步过滤（`HAVING` 已经
  把不完备的候选排除在结果集之外）。
- 不在缺失场景值处填充占位数值后再参与聚合——这条路径在 SQL 里不存在（缺失
  场景对应的候选根本不会出现在 `primary_vals` / 分组结果中），本函数同样不
  提供等价的 Python 实现。
- 无候选满足完备条件时，`store.query_worst_case()` 返回空列表，`worst_case()`
  据此返回空字典，不抛异常、不返回任何填充值。

## `RankedCandidate`：设计说明（design.md §6.5.3 只给出返回类型名，未定义字段）

最小字段集：

- `candidate_id: str` —— 候选标识，`rank()` 输出的主键。
- `worst_case_value: float` —— 该候选在 Evaluation 集上的主目标 worst-case 聚合值
  （即 `worst_case()` 为该候选返回的值），原始未做方向调整，供调用方直接展示。
- `secondary_values: Mapping[str, float]` —— 该候选参与排序的次目标取值，键为
  `metric_id`，只含 `objective.secondary_lexicographic` 中**已激活**的项（未激活
  项既未参与比较，也不放进这个映射，避免调用方误读成"已比较过但恰好没有区分度"）。
- `rank: int` —— 1-indexed 名次（`rank()` 输出列表中的位置 + 1），不是数据库列，
  纯粹是本次排序结果的展示字段。

## `sort_key()` 的 `tie_tolerance` 分桶（bucketing）技术：设计说明

`design.md` §6.5.3` 把 `sort_key()` 描述为返回"一个 tuple"用于比较，暗示其调用
惯例是 `sorted(candidates, key=lambda c: sort_key(...))` 这种标准键排序，而不是
两两比较的自定义 comparator。但 R9.8/R9.11 要求的"主目标差绝对值 ≤ tie_tolerance
判并列"本质上是一个**成对关系**（pairwise relation）：三个候选的 worst-case 值
10.0、10.05、10.1（`tie_tolerance=0.1`）中，1↔2 差 0.05 并列，2↔3 差 0.05 并列，
但 1↔3 差 0.1（=tie_tolerance，按 AC 定义仍并列，取等判为并列）——这类链式关系
在更极端的输入下会退化为经典的"容差并列不是等价关系"问题（差值判定不满足传递
性），无法用任何单一的 per-candidate 排序键精确还原。

本实现采用标准的**分桶**近似：把主目标值（按 `direction` 调整符号后）除以
`tie_tolerance` 再四舍五入取整，作为 tuple 的第一个元素。四舍五入到同一整数
桶的两个候选，其原始差值必然 ≤ `tie_tolerance`（桶宽即为 `tie_tolerance`），
因此"同桶 ⟹ 判并列"这一方向严格成立、不会误判本不并列的候选为并列；但反过来
"差值 ≤ tie_tolerance 却落入不同桶"的边界情况确实存在（桶边界附近的两个值，
例如桶边界正中的两侧），这是分桶法固有的近似，design.md 只给出 tuple 签名、
未给出比较算法本身，因此这里选择这一标准做法并在此明确记录其边界行为，而不是
试图实现一个不可能用单键排序精确表达的成对传递闭包。

`tie_tolerance == 0`（schema 约束为 `ge=0`，允许取 0）时除以零会报错，因此特判：
`tie_tolerance == 0` 时跳过分桶，直接用带符号的原始值（不做任何舍入）作为第一
个 tuple 元素——此时"并列"只在两个候选的调整后取值逐位相等时发生，符合"零容差"
的字面含义。

## 次目标项：跳过未激活项、按各自 `direction` 取反、原始精度不分桶

`objective.secondary_lexicographic` 的顺序即字典序优先级顺序；`metric_id` 不在
`active_metrics` 内的项整项跳过（不占用 tuple 位置，不是补 0 或补 None）。已激活
项按其自身 `direction`（与主目标的 `direction` 无关，两者可以相反）取反后原样
放入 tuple——`tie_tolerance` 是 design.md 明确限定的**主目标专属**概念（"主目标
差异落在 tie_tolerance 内时按 secondary_lexicographic 比较"），次目标本身不再有
第二层"并列"容差，全精度比较。

`secondary` 映射缺失某个已激活 `metric_id` 时，`sort_key()` 抛出 `KeyError`
（而不是静默当作"最差"排到最后）——`secondary` 是调用方传入的数据，缺键更可能
是数据管道 bug（例如该候选的这个次目标指标从未被有效计算过），静默吞掉会把真实
问题伪装成"合法的排序结果"。

## `rank()` 的实现路径

1. 直接调用 `worst_case()`（任务 12.2）取得主目标聚合值；"不可行候选与未跑完
   Evaluation 集的候选不参与主目标排序"由 `worst_case()` 自身的 SQL `HAVING`
   完备性过滤保证，本函数复用而不重新实现该过滤（`Store.query_worst_case()`
   的 SQL 早已把 `feasible=0` 与主目标 `valid=0` 的候选排除在结果集之外）。
2. 对 `secondary_lexicographic` 中每个**已激活**的 `metric_id`，复用同一条
   `Store.query_worst_case(task_id, metric_id)`（把该方法的 `primary_metric`
   形参换成次目标的 `metric_id`）取得该次目标在 Evaluation 集上的"worst-case
   风格"聚合值——`WORST_CASE_SQL` 的 `HAVING` 完备性逻辑不关心具体聚合哪个
   `metric_id`，同一条 SQL 对任意 `metric_id` 都成立。这让次目标复用与主目标
   完全相同的"Evaluation 全场景有效值取最大值"聚合语义，是一个一致、有文本
   依据的选择（design.md 没有给次目标另外的聚合方式，临时发明一种不一致的
   ad-hoc 聚合反而没有依据）。
3. 按 `sort_key()` 升序排序（Python 默认 tuple 比较即完成三段定序，不需要
   自定义 comparator），取前 `top_n`，1-indexed 编号，组装 `RankedCandidate`。

## "不提供以加权得分抵消硬约束的比较路径"：结构性满足，非运行期检查

`sort_key()` / `rank()` 全程只把各指标的（方向调整后）取值原样放入 tuple 的
不同位置，从未把不同指标的数值相加、相乘或加权求和成一个标量。Python 的 tuple
比较规则（逐位比较，第一个不同的位置即决定大小关系，后续位置只在前面全部相等
时才生效）本身就排除了"用次目标的优势抵消主目标的劣势"这种交易——不存在任何
数值组合方式能让一个 tuple 因为第二个元素更好而在第一个元素更差的情况下排到
前面。这是三段字典序比较方法在构造上就具备的性质，因此本实现不需要、也没有
另外增加一个"检测加权评分"的运行期断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from poweragent.config.schema import MetricsConfig
from poweragent.store.repo import WORST_CASE_SQL, Store

__all__ = ["WORST_CASE_SQL", "worst_case", "RankedCandidate", "sort_key", "rank"]


def worst_case(
    store: Store, task_id: str, metrics_cfg: MetricsConfig
) -> dict[str, float]:
    """执行 §5.4 的 `WORST_CASE_SQL`；未跑完的候选不出现在返回值中。

    `primary_metric` 取自 `metrics_cfg.objective.primary.metric_id`。返回值
    丢弃 SQL 结果里的 `n_ok` 列——它只是 `HAVING` 子句自身的完备性计数，不属于
    本函数的返回契约。
    """
    primary_metric = metrics_cfg.objective.primary.metric_id
    rows = store.query_worst_case(task_id, primary_metric)
    return {candidate_id: worst for candidate_id, worst, _n_ok in rows}


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """`rank()` 的单个输出元素；字段设计见本模块顶部文档。"""

    candidate_id: str
    worst_case_value: float
    secondary_values: Mapping[str, float]
    rank: int


def sort_key(
    candidate_id: str,
    worst: float,
    secondary: Mapping[str, float],
    metrics_cfg: MetricsConfig,
) -> tuple:
    """三段定序键：`(主目标分桶值, *已激活次目标取反值..., candidate_id)`。

    分桶技术与次目标处理的完整推导见本模块顶部文档。`secondary` 缺失某个
    已激活 `metric_id` 时抛出 `KeyError`（不静默填充）。
    """
    objective = metrics_cfg.objective
    primary_direction = objective.primary.direction
    signed_worst = worst if primary_direction == "minimize" else -worst

    tie_tolerance = objective.tie_tolerance
    primary_key: float
    if tie_tolerance == 0:
        primary_key = signed_worst
    else:
        primary_key = round(signed_worst / tie_tolerance)

    secondary_keys: list[float] = []
    for term in objective.secondary_lexicographic:
        if term.metric_id not in metrics_cfg.active_metrics:
            continue
        if term.metric_id not in secondary:
            raise KeyError(
                f"sort_key: candidate_id={candidate_id!r} 缺少已激活次目标 "
                f"metric_id={term.metric_id!r} 在 secondary 映射中的取值"
            )
        value = secondary[term.metric_id]
        secondary_keys.append(value if term.direction == "minimize" else -value)

    return (primary_key, *secondary_keys, candidate_id)


def rank(
    store: Store,
    task_id: str,
    metrics_cfg: MetricsConfig,
    *,
    top_n: int = 3,
) -> list[RankedCandidate]:
    """按三段定序对满足 Evaluation 集完备条件的候选排序，返回前 `top_n` 名。

    完备性/可行性过滤完全由 `worst_case()`（及其底层 `WORST_CASE_SQL`）承担，
    本函数不重新实现该过滤。次目标取值复用 `Store.query_worst_case()`，对每个
    已激活的次目标 `metric_id` 各调用一次，理由见本模块顶部文档。
    """
    worst_by_candidate = worst_case(store, task_id, metrics_cfg)

    active_metrics = metrics_cfg.active_metrics
    secondary_metric_ids = [
        term.metric_id
        for term in metrics_cfg.objective.secondary_lexicographic
        if term.metric_id in active_metrics
    ]

    secondary_worst_by_metric: dict[str, dict[str, float]] = {
        metric_id: {
            candidate_id: worst
            for candidate_id, worst, _n_ok in store.query_worst_case(task_id, metric_id)
        }
        for metric_id in secondary_metric_ids
    }

    entries: list[tuple[str, float, dict[str, float]]] = []
    for candidate_id, worst in worst_by_candidate.items():
        secondary_values = {
            metric_id: secondary_worst_by_metric[metric_id][candidate_id]
            for metric_id in secondary_metric_ids
            if candidate_id in secondary_worst_by_metric[metric_id]
        }
        entries.append((candidate_id, worst, secondary_values))

    entries.sort(key=lambda e: sort_key(e[0], e[1], e[2], metrics_cfg))

    return [
        RankedCandidate(
            candidate_id=candidate_id,
            worst_case_value=worst,
            secondary_values=secondary_values,
            rank=index + 1,
        )
        for index, (candidate_id, worst, secondary_values) in enumerate(
            entries[:top_n]
        )
    ]
