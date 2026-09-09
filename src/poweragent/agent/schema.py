"""LLM 输出的强类型契约，以及校验失败时可回灌的错误清单。

职责边界：本模块只管**形状**
------------------------------
这里校验的是"模型返回的 JSON 结构对不对"：字段齐不齐、类型对不对、候选个数在不在
范围内、每个候选的键集合是否恰为设计变量、数值是否为正有限。

**不**校验取值是否落在合法域内、是否命中档位、是否与已测点重复——那是
`agent.validate` 的职责，且它是全系统唯一的确定性候选校验器。两者刻意分开：形状错
是"模型没按格式说话"，可以把错误清单回灌让它重说；取值错是"模型提了一个不合法的
设计点"，那是要落 `rejections` 表的搜索行为，不该退化成一次格式修复。

错误清单必须可操作
------------------
`format_violations()` 产出的不是 pydantic 的原始报错。原始报错里带 `url`、
`input_value` 的完整 repr、以及 `Input should be a valid number` 这类对人友好但对
"该怎么改"没有指向的措辞。回灌给模型的清单必须直接说明：哪个字段路径、错在哪、
应该是什么。否则修复轮就是在赌模型能自己猜出格式要求，而修复轮数是有预算上限的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "DESIGN_VARIABLE_KEYS",
    "MIN_CANDIDATES",
    "MAX_CANDIDATES",
    "ProposalPayload",
    "SchemaViolation",
    "parse_proposal_payload",
    "format_violations",
    "response_format_instruction",
]

# 设计变量键集合。与 `constraints.yaml` 的 `design_space.variables` 一致，
# 但这里独立写死而不是从配置读：本模块校验的是"模型有没有按约定的键说话"，
# 而 prompt 里告诉模型的键名同样来自这个常量。让两者共用一个来源，即可保证
# "要求模型返回什么"与"校验模型返回了什么"永远一致。
DESIGN_VARIABLE_KEYS: tuple[str, ...] = ("rcomp", "ccomp")

# 每轮候选个数的上下界（design.md §6.5：依 state 输出 3~6 候选）。
# 下界防止模型只提一个点、把搜索退化成单点爬坡；上界防止一轮消耗过多仿真预算。
MIN_CANDIDATES = 3
MAX_CANDIDATES = 6


class _CandidateModel(BaseModel):
    """单个候选：键集合恰为设计变量，取值为正有限实数。

    `extra="forbid"` 是有意的：模型多给一个键（比如自创一个 `ccomp2`）说明它没有
    按设计空间说话，这种输出不该被静默截断后放行。
    """

    model_config = ConfigDict(extra="forbid")

    rcomp: float = Field(gt=0.0)
    ccomp: float = Field(gt=0.0)

    @field_validator("rcomp", "ccomp")
    @classmethod
    def _must_be_finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("必须是有限实数，不接受 NaN 或 Infinity")
        return value


class ProposalPayload(BaseModel):
    """LLM 单次提案的完整输出结构。

    三个文本字段（`search_hypothesis` / `target_bottleneck` /
    `expected_tradeoff`）不是装饰：它们让工程师能判断模型的推理是否合理，而不是
    只看到一串坐标。这也是 LLM 相对纯数值优化器的实际价值之一，因此要求非空。
    """

    model_config = ConfigDict(extra="forbid")

    search_hypothesis: str = Field(min_length=1)
    target_bottleneck: str = Field(min_length=1)
    expected_tradeoff: str = Field(min_length=1)
    stop_recommendation: bool
    candidates: list[_CandidateModel] = Field(
        min_length=MIN_CANDIDATES, max_length=MAX_CANDIDATES
    )

    def candidate_parameters(self) -> tuple[dict[str, float], ...]:
        """候选的原始取值，按输出顺序。

        返回普通 dict 而非 pydantic 模型：下游 `agent.validate` 与 `rejections`
        表处理的都是"未校验的原始取值"这一形态，不需要也不应该携带本模块的类型。
        """
        return tuple(
            {key: getattr(item, key) for key in DESIGN_VARIABLE_KEYS}
            for item in self.candidates
        )


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """一条可回灌的结构性错误。

    `path` 用点号连接的字段路径（如 `candidates.0.rcomp`），`problem` 说明错在哪，
    `expected` 说明应该是什么。三者分开而不是拼成一句话，是为了让调用方既能拼成
    人读的文本，也能在测试里对具体某个字段的某类错误做断言。
    """

    path: str
    problem: str
    expected: str

    def as_line(self) -> str:
        return f"- `{self.path}`: {self.problem}。应为：{self.expected}"


def _describe(error: Mapping[str, Any]) -> SchemaViolation:
    """把一条 pydantic 错误翻译成可操作的说明。

    只对实际会出现的错误类型逐一给出针对性措辞，其余落到兜底分支并原样带上
    pydantic 的消息——兜底分支存在是因为把未预料的错误类型压成一句通用文案，会让
    修复轮失去方向；带上原始消息至少保留了信息。
    """
    path = ".".join(str(part) for part in error["loc"]) or "<root>"
    kind = error["type"]

    if kind == "missing":
        return SchemaViolation(path, "缺少该字段", "必须提供")
    if kind == "extra_forbidden":
        return SchemaViolation(
            path,
            "出现了契约之外的字段",
            f"只允许 {', '.join(DESIGN_VARIABLE_KEYS)} 这些设计变量键"
            if path.startswith("candidates.")
            else "移除该字段",
        )
    if kind in ("float_parsing", "float_type"):
        return SchemaViolation(path, "不是数值", "十进制数值，可用科学计数法如 1.87e4")
    if kind == "greater_than":
        return SchemaViolation(path, "取值不为正", "大于 0 的有限实数")
    if kind in ("string_too_short", "string_type"):
        return SchemaViolation(path, "为空或不是字符串", "一句非空的说明文字")
    if kind == "bool_parsing" or kind == "bool_type":
        return SchemaViolation(path, "不是布尔值", "true 或 false（JSON 布尔字面量）")
    if kind == "too_short":
        return SchemaViolation(
            path, "候选个数不足", f"{MIN_CANDIDATES} 到 {MAX_CANDIDATES} 个候选"
        )
    if kind == "too_long":
        return SchemaViolation(
            path, "候选个数过多", f"{MIN_CANDIDATES} 到 {MAX_CANDIDATES} 个候选"
        )
    if kind == "list_type":
        return SchemaViolation(path, "不是数组", "JSON 数组")
    if kind == "value_error":
        return SchemaViolation(path, str(error.get("msg", "取值非法")), "有限实数")

    return SchemaViolation(path, str(error.get("msg", kind)), f"符合契约的 {kind} 取值")


def parse_proposal_payload(
    raw_text: str,
) -> tuple[ProposalPayload | None, tuple[SchemaViolation, ...]]:
    """解析并校验模型返回的文本。

    成功返回 `(payload, ())`；失败返回 `(None, violations)`，`violations` 非空。
    不抛异常：调用方（重试循环）需要的是"错在哪、能不能回灌"，异常反而要求它在
    控制流里再解包一层。

    JSON 本身解析不了时也归为一条结构性错误。模型偶尔会在 JSON 外面裹一层
    markdown 代码块，这里**不做**剥壳容错：请求时已明确要求返回纯 JSON 对象，
    悄悄容错会让"模型是否遵守输出格式"这件事永远测不出来，而这恰是要统计的指标
    （一次通过率）。
    """
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, (
            SchemaViolation(
                "<root>",
                f"不是合法 JSON（{exc.msg}，位于第 {exc.lineno} 行第 {exc.colno} 列）",
                "一个 JSON 对象，且不要包裹在 markdown 代码块里",
            ),
        )

    if not isinstance(data, dict):
        return None, (
            SchemaViolation(
                "<root>", f"顶层是 {type(data).__name__} 而不是对象", "一个 JSON 对象"
            ),
        )

    try:
        return ProposalPayload.model_validate(data), ()
    except ValidationError as exc:
        return None, tuple(_describe(error) for error in exc.errors())


def format_violations(violations: Sequence[SchemaViolation]) -> str:
    """把错误清单拼成回灌给模型的文本。

    逐条列出而不是只报第一条：一次修复轮的成本与一次调用相同，把全部问题一次说清
    比让模型挤牙膏式地逐个修正更省预算。
    """
    lines = "\n".join(v.as_line() for v in violations)
    return (
        "上一次的输出不符合要求，请修正下列问题后重新输出完整的 JSON 对象：\n"
        f"{lines}\n"
        "只输出 JSON 对象本身，不要附加解释文字，也不要包裹代码块。"
    )


def response_format_instruction() -> str:
    """请求时附给模型的输出格式说明。

    与本模块的校验规则同源（键名取 `DESIGN_VARIABLE_KEYS`，个数取
    `MIN_CANDIDATES`/`MAX_CANDIDATES`），因此"要求什么"与"校验什么"不会漂移。
    """
    key_list = ", ".join(f'"{k}"' for k in DESIGN_VARIABLE_KEYS)
    candidate_example = ", ".join(f'"{k}": <数值>' for k in DESIGN_VARIABLE_KEYS)
    return (
        "只输出一个 JSON 对象，不要附加解释文字，也不要包裹在代码块里。结构如下：\n"
        "{\n"
        '  "search_hypothesis": "这一轮打算怎么搜索，以及为什么",\n'
        '  "target_bottleneck": "当前最主要的瓶颈指标或约束",\n'
        '  "expected_tradeoff": "预期要付出的代价",\n'
        '  "stop_recommendation": false,\n'
        '  "candidates": [\n'
        f"    {{{candidate_example}}}\n"
        "  ]\n"
        "}\n"
        f"candidates 需给出 {MIN_CANDIDATES} 到 {MAX_CANDIDATES} 个，"
        f"每个候选只含 {key_list} 两个键，取值为正实数，可用科学计数法。"
    )
