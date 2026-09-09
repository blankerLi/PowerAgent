"""确定性候选校验器 —— 全系统唯一。

这是 LLM 域与仿真域之间的闸门。模型提出的每个候选都必须过这里，没有旁路：
`design.md` 的 CP-2 要求"不存在绕过校验的仿真路径"，连参考网格扫描的 144 个点也走
同一个函数。

三件事它不做
------------
**不调 LLM。** 校验必须是确定性的，否则"这个候选为什么被拒"没有稳定答案。

**不静默修正语义。** 不命中档位的取值被**拒绝**，而不是吸附到最近档位。这一条是刻意
的：静默吸附会让模型永远学不会精确输出档位值，而且被吸附后的点与模型意图的点是两个
不同的设计，落库时却记成模型提了后者——搜索轨迹就此失真。实测模型在 prompt 里给出
展开档位后能精确命中，所以严格拒绝是可行的要求，不是苛求。

**不决定自己的严格度。** `mode` 由 `controller` 按确定性规则给出（无改善轮数与是否
已有最佳候选决定），提案器无权选择。让被校验方挑选校验标准会直接击穿权限边界。

为什么没有 normalize_si
-----------------------
`design.md` §6.7 列了一个 `normalize_si()`，职责是"统一 SI、拒绝无单位数值"。在本实现
里它无事可做：`agent/schema.py` 的 pydantic 契约已经把候选约束为正的有限浮点数，
键集合恰为设计变量，没有任何携带单位的形态能通过。保留一个只剩键检查的函数会让人
以为还有单位转换发生。键集合检查因此内联在 `validate()` 里。

三种 mode 只在第三类检查上不同
------------------------------
前四项（键集合、数值合法性、合法域、档位）三种 mode 一律执行。差异只在重复与新颖度：

- `explore`：拒绝重复点，拒绝与已测点档位距离小于 `novelty_min_ticks` 的点
- `local_refine`：拒绝重复点，允许近邻并把距离写进 `notes`
- `grid`：跳过重复与新颖度检查

`grid` 的存在理由很具体：参考网格的相邻点档位距离恰为 1.0，若 `novelty_min_ticks` 被
冻结为大于 1.0，144 个点会被整批以 `low_novelty` 拒绝。有了 `grid` 模式，那条阈值就
可以按探索需要自由取值，不必为了迁就网格扫描而被限制在 1.0 以内。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence

from poweragent.agent.state import DomainSpec, TestedPoint
from poweragent.config.hashing import candidate_id as compute_candidate_id
from poweragent.store.repo import Candidate

__all__ = [
    "ValidationMode",
    "ValidationOutcome",
    "snap_to_tick",
    "tick_distance",
    "validate",
    "decide_mode",
]

ValidationMode = Literal["explore", "local_refine", "grid"]

# 拒绝原因中距离数值的格式化规则（`design.md` §5.3.1）。与 `notes` 里的近邻标注共用
# 同一规则，保证两次调用对同一距离产出逐字符相同的字符串——拒绝原因会落
# `rejections` 表，若格式不稳定，同一原因会出现多种写法而无法聚合统计。
def _format_distance(distance: float) -> str:
    return format(distance, ".4f")


# 新颖度比较的容差。档位字面量按 `.12g` 格式化后再转回浮点，因此"恰好相差一整档"
# 算出来常是 0.999999999999 而不是 1.0。`novelty_min_ticks = 1.0` 的语义是"至少差一
# 整档"，恰好差一档必须通过；没有这个容差，阈值取 1.0 时相邻档位的候选会被整批误拒，
# 而这类错拒不报错、只让搜索永远看不到那片区域。
_NOVELTY_COMPARISON_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """校验结果（`design.md` §6.7）。

    `rejected` 保留**原始取值**与具名原因的配对，而不只是计数：被拒候选要连同原因落
    `rejections` 表，那是搜索轨迹的一部分——"模型提过这个点但它越界了"与"模型没提过
    这个点"是两件不同的事，报告与后续分析都依赖这个区别。

    `notes` 只在 `local_refine` 模式下非空。它的存在是因为 `validate()` 按契约不得写
    数据库，而近邻标注需要一个输出去处。
    """

    accepted: tuple[Candidate, ...] = ()
    rejected: tuple[tuple[Mapping[str, float], str], ...] = ()
    notes: Mapping[str, str] = field(default_factory=dict)


def snap_to_tick(value: float, spec: DomainSpec, *, rel_tol: float) -> float | None:
    """命中档位则返回该档位值，不命中返回 `None`。

    判定为 `|value − tick| <= rel_tol × |tick|`。返回档位值本身而不是原样返回输入，
    是为了消掉输入里的浮点表示误差：模型输出 `18738.0` 与档位 `18738.000000000004`
    在容差内相等，落库时应统一为档位值，否则同一个设计点会因为末位差异生成两个不同的
    `candidate_id`，缓存判等与重复检测都会失效。

    不命中时返回 `None` 而不是最近档位——调用方据此拒绝，不做静默吸附（见模块 docstring）。
    """
    for literal in spec.ticks:
        tick = float(literal)
        if abs(value - tick) <= rel_tol * abs(tick):
            return tick
    return None


def tick_distance(
    a: Mapping[str, float], b: Mapping[str, float], domain: Mapping[str, DomainSpec]
) -> float:
    """两点之间的档位距离：各维度归一化对数距离取最大值。

        max_i |log10(a_i) − log10(b_i)| / log10(tick_ratio_i)

    单位是"档位数"：相邻档位距离恰为 1.0。用对数尺度是因为两个设计变量都是 log 刻度，
    在线性尺度上 1 kΩ→2 kΩ 与 99 kΩ→100 kΩ 的绝对差相同但搜索意义相差极大。

    `tick_ratio` 唯一有定义，因为档位强制等比（配置层已校验）。取各维度最大值而非欧氏
    距离：档位距离要回答的是"这两个点是否足够不同"，只要有一维差了一档就算不同，
    而欧氏距离会把两维各差半档判成距离 0.71 而误判为近邻。
    """
    distances = []
    for name, spec in domain.items():
        if name not in a or name not in b:
            continue
        if len(spec.ticks) < 2:
            continue

        first, second = float(spec.ticks[0]), float(spec.ticks[1])
        if first <= 0.0 or second <= 0.0:
            continue
        ratio = second / first
        if ratio <= 0.0 or ratio == 1.0:
            continue

        va, vb = a[name], b[name]
        if va <= 0.0 or vb <= 0.0:
            continue

        distances.append(
            abs(math.log10(va) - math.log10(vb)) / abs(math.log10(ratio))
        )

    return max(distances) if distances else 0.0


def decide_mode(
    *, no_improvement_rounds: int, has_current_best: bool
) -> ValidationMode:
    """按确定性规则决定校验模式（`design.md` §6.7）。

        no_improvement_rounds == 0                    ⟹ explore
        no_improvement_rounds >= 1 且已有最佳候选     ⟹ local_refine
        其余                                          ⟹ explore

    放在本模块而不是 `controller` 里，是为了让规则与它约束的检查写在一处；但**调用它
    的是 `controller`**，提案器不得调用。这个区别是权限边界：规则本身可以公开，选择权
    不能交给被校验方。

    `grid` 不由本函数产生——它由参考网格扫描固定传入，不是搜索过程中的一种状态。
    """
    if no_improvement_rounds >= 1 and has_current_best:
        return "local_refine"
    return "explore"


def _check_shape(
    raw: Mapping[str, float], domain: Mapping[str, DomainSpec]
) -> str | None:
    """键集合与数值合法性。返回拒绝原因，通过则返回 `None`。"""
    if set(raw) != set(domain):
        return "key_mismatch"
    for value in raw.values():
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return "non_finite_value"
        if float(value) <= 0.0:
            return "non_positive_value"
    return None


def validate(
    raw: Sequence[Mapping[str, float]],
    *,
    domain: Mapping[str, DomainSpec],
    tick_match_rel_tol: float,
    novelty_min_ticks: float,
    tested: Sequence[TestedPoint] = (),
    mode: ValidationMode = "explore",
) -> ValidationOutcome:
    """校验一批未经检查的原始取值。

    签名相对 `design.md` §6.7 的字面形式做了一处调整：不接收整个 `DesignSpace`，而是
    接收它实际用到的三样——展开后的合法域、档位命中容差、新颖度阈值。这与本代码库
    既有的惯例一致（例如 `check_budget_feasibility()` 只接两个标量而非整个
    `ProbeRecord`）：函数签名说明它真正读了什么，而不是它属于哪个配置节。

    同一批内的重复也会被拒绝：模型偶尔会在一次输出里给出两个相同的候选，若只与历史
    已测点比对，这两个会双双通过并各消耗一次仿真预算。

    检查顺序是从成本最低到最高（键集合 → 数值 → 合法域 → 档位 → 批内重复 → 历史重复
    → 新颖度），命中即拒绝、不继续。因此拒绝原因是"第一个"问题而非全部问题——候选被
    拒后不会进入仿真，逐条列全没有额外价值，而顺序固定保证了同一输入总得到同一原因。
    """
    accepted: list[Candidate] = []
    rejected: list[tuple[Mapping[str, float], str]] = []
    notes: dict[str, str] = {}

    tested_params = [dict(point.parameters_si) for point in tested]
    accepted_params: list[dict[str, float]] = []

    for item in raw:
        original = dict(item)

        shape_problem = _check_shape(original, domain)
        if shape_problem is not None:
            rejected.append((original, shape_problem))
            continue

        # 合法域：越界直接拒绝，不裁剪到边界。裁剪同样属于"静默修正语义"。
        out_of_domain = next(
            (
                name
                for name, spec in domain.items()
                if not (spec.low <= original[name] <= spec.high)
            ),
            None,
        )
        if out_of_domain is not None:
            rejected.append((original, f"out_of_domain:{out_of_domain}"))
            continue

        # 档位：命中则取档位值（消掉浮点表示误差），不命中则拒绝。
        snapped: dict[str, float] = {}
        off_tick_name: str | None = None
        for name, spec in domain.items():
            tick = snap_to_tick(original[name], spec, rel_tol=tick_match_rel_tol)
            if tick is None:
                off_tick_name = name
                break
            snapped[name] = tick
        if off_tick_name is not None:
            rejected.append((original, f"off_tick:{off_tick_name}"))
            continue

        if mode != "grid":
            duplicate_of_batch = any(
                tick_distance(snapped, earlier, domain) == 0.0
                for earlier in accepted_params
            )
            if duplicate_of_batch:
                rejected.append((original, "duplicate_in_batch"))
                continue

            duplicate_of_history = any(
                tick_distance(snapped, point, domain) == 0.0 for point in tested_params
            )
            if duplicate_of_history:
                rejected.append((original, "duplicate_of_tested"))
                continue

            if tested_params:
                nearest = min(
                    tick_distance(snapped, point, domain) for point in tested_params
                )
                if (
                    mode == "explore"
                    and nearest < novelty_min_ticks - _NOVELTY_COMPARISON_TOLERANCE
                ):
                    rejected.append((original, f"low_novelty:{_format_distance(nearest)}"))
                    continue

        candidate_id = compute_candidate_id(snapped)
        accepted.append(Candidate(candidate_id=candidate_id, parameters_si=snapped))
        accepted_params.append(snapped)

        if mode == "local_refine" and tested_params:
            nearest = min(
                tick_distance(snapped, point, domain) for point in tested_params
            )
            notes[candidate_id] = f"near_tested:{_format_distance(nearest)}"

    return ValidationOutcome(
        accepted=tuple(accepted), rejected=tuple(rejected), notes=notes
    )
