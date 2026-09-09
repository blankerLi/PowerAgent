"""结构化请求循环与输出契约的测试，全部用假客户端，不发网络请求。

这里测的核心是**两类失败被分开处理**。把它们混成一个重试计数会同时坏两件事：
一次网络抖动吃掉本该留给格式修复的配额；一个始终答错格式的模型被当成网络不稳定
反复重试。它们的根因不同、正确的应对不同、该放弃的时机也不同。

四个 `outcome` 取值各有一条测试，因为它们不只是日志字符串——`llm_calls.outcome`
是"一次通过率"与"修复后通过率"这两个指标的唯一数据来源，取值错了统计就错了。
"""

from __future__ import annotations

import json
from typing import Sequence

import pytest

from poweragent.agent.client import (
    LlmNetworkError,
    LlmResponse,
    request_proposal,
)
from poweragent.agent.schema import (
    DESIGN_VARIABLE_KEYS,
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    format_violations,
    parse_proposal_payload,
    response_format_instruction,
)

pytestmark = pytest.mark.agent


def _valid_payload_json(n_candidates: int = MIN_CANDIDATES) -> str:
    return json.dumps(
        {
            "search_hypothesis": "沿 Rcomp 增大方向试探，压低下冲",
            "target_bottleneck": "undershoot",
            "expected_tradeoff": "相位裕量下降",
            "stop_recommendation": False,
            "candidates": [
                {"rcomp": 12000.0 + i * 1000.0, "ccomp": 2.2e-9}
                for i in range(n_candidates)
            ],
        }
    )


class ScriptedClient:
    """按脚本逐次返回文本或抛网络异常的假客户端。

    脚本元素为 `str`（返回该文本）或 `Exception` 实例（抛出）。只实现 `LlmClient`
    协议要求的两个成员，不继承任何基类——协议是给类型检查和替身用的形状约定，
    不产生运行时继承关系。
    """

    def __init__(self, script: Sequence[object], *, model_id: str = "fake-model") -> None:
        self.model_id = model_id
        self._script = list(script)
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, user: str) -> LlmResponse:
        self.calls.append({"system": system, "user": user})
        if not self._script:
            raise AssertionError("假客户端脚本已用尽，说明被调用了预期之外的次数")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResponse(text=str(item), prompt_tokens=100, completion_tokens=50)


# --------------------------------------------------------------------------
# 四个 outcome 取值
# --------------------------------------------------------------------------


def test_first_attempt_valid_yields_ok() -> None:
    """首次即合格 ⟹ `outcome='ok'`，只发一次请求。"""
    client = ScriptedClient([_valid_payload_json()])

    result = request_proposal(
        client,
        system_prompt="sys",
        user_prompt="usr",
        max_repair_rounds=2,
        max_network_retries=2,
    )

    assert result.outcome == "ok"
    assert result.payload is not None
    assert len(client.calls) == 1
    assert result.repair_rounds_used == 0
    assert result.total_tokens == 150


def test_schema_failure_then_success_yields_repaired() -> None:
    """首次不合格、回灌后通过 ⟹ `outcome='repaired'`。

    "首次通过"与"修复后通过"必须是两个不同的取值：前者是模型的原生格式遵从度，
    后者包含了我们的补救。混成一个 `ok` 会让一次通过率虚高。
    """
    client = ScriptedClient(['{"search_hypothesis": "缺字段"}', _valid_payload_json()])

    result = request_proposal(
        client,
        system_prompt="sys",
        user_prompt="usr",
        max_repair_rounds=2,
        max_network_retries=2,
    )

    assert result.outcome == "repaired"
    assert result.payload is not None
    assert len(client.calls) == 2
    assert result.repair_rounds_used == 1
    # 修复轮的 token 也要计入：只算成功那次会让预算记账偏低。
    assert result.total_tokens == 300


def test_repair_rounds_exhausted_yields_schema_invalid() -> None:
    """修复轮用尽仍不合格 ⟹ `outcome='schema_invalid'`，且不再继续请求。

    总请求次数为 `max_repair_rounds + 1`：首次调用不算修复轮。
    """
    bad = '{"candidates": []}'
    client = ScriptedClient([bad, bad, bad])

    result = request_proposal(
        client,
        system_prompt="sys",
        user_prompt="usr",
        max_repair_rounds=2,
        max_network_retries=2,
    )

    assert result.outcome == "schema_invalid"
    assert result.payload is None
    assert len(client.calls) == 3, "应为首次 + 两轮修复"
    assert all(a.violations for a in result.attempts)


def test_network_retries_exhausted_yields_empty() -> None:
    """网络重试用尽 ⟹ `outcome='empty'`，等价于本轮无候选。

    模型没答上时没有可回灌的内容，继续修复轮没有意义，因此整体立即以 `empty`
    结束而不是把剩余修复轮也耗掉。`run_task()` 按空轮分支处理它，不引入新的
    失败分类。
    """
    client = ScriptedClient([LlmNetworkError("timeout")] * 3)

    result = request_proposal(
        client,
        system_prompt="sys",
        user_prompt="usr",
        max_repair_rounds=2,
        max_network_retries=2,
        backoff_base_s=0.0,  # 测试不真的睡
    )

    assert result.outcome == "empty"
    assert result.payload is None
    assert len(client.calls) == 3, "应为首次 + 两次网络重试"
    assert result.attempts == (), "没有拿到任何文本，不应记录尝试"
    assert len(result.network_failures) == 3


# --------------------------------------------------------------------------
# 两类失败的计数彼此独立
# --------------------------------------------------------------------------


def test_network_failure_does_not_consume_repair_rounds() -> None:
    """网络抖动后恢复，仍保有完整的修复轮配额。

    这是"两类计数分开"的直接后果。若共享计数，这里的一次网络失败会吃掉一轮修复
    配额，导致后面真正需要修复时提前放弃。
    """
    client = ScriptedClient(
        [
            LlmNetworkError("transient"),  # 网络抖动
            '{"bad": 1}',  # 恢复后格式不对 → 第 1 轮修复
            '{"bad": 2}',  # 仍不对 → 第 2 轮修复
            _valid_payload_json(),  # 终于对了
        ]
    )

    result = request_proposal(
        client,
        system_prompt="sys",
        user_prompt="usr",
        max_repair_rounds=2,
        max_network_retries=2,
        backoff_base_s=0.0,
    )

    assert result.outcome == "repaired"
    assert result.payload is not None
    assert result.repair_rounds_used == 2, "两轮修复应全部可用"
    assert len(result.network_failures) == 1


def test_repair_prompt_carries_the_violation_list() -> None:
    """修复轮的用户消息里必须带上具体的错误清单。

    否则修复轮就是在赌模型自己猜出格式要求，而修复轮数是有预算上限的。这里断言
    第二次请求的内容确实比第一次多了错误说明，且提到了出问题的字段。
    """
    client = ScriptedClient(
        ['{"search_hypothesis": "只给了一个字段"}', _valid_payload_json()]
    )

    request_proposal(
        client,
        system_prompt="sys",
        user_prompt="usr",
        max_repair_rounds=1,
        max_network_retries=0,
    )

    first, second = client.calls[0]["user"], client.calls[1]["user"]
    assert len(second) > len(first)
    assert "candidates" in second, "错误清单应指出缺少 candidates"
    assert "target_bottleneck" in second


# --------------------------------------------------------------------------
# 输出契约
# --------------------------------------------------------------------------


def test_valid_payload_exposes_raw_candidate_parameters() -> None:
    """合格输出能取出未校验的原始取值，形态为普通 dict。

    下游 `agent.validate` 与 `rejections` 表处理的都是这一形态，不应携带本模块
    的 pydantic 类型。
    """
    payload, violations = parse_proposal_payload(_valid_payload_json(4))

    assert violations == ()
    assert payload is not None
    params = payload.candidate_parameters()
    assert len(params) == 4
    for item in params:
        assert set(item) == set(DESIGN_VARIABLE_KEYS)
        assert all(isinstance(v, float) for v in item.values())


@pytest.mark.parametrize(
    ("raw", "expected_path_fragment"),
    [
        ("not json at all", "<root>"),
        ("[1, 2, 3]", "<root>"),
        ('{"search_hypothesis": "x"}', "candidates"),
        # 候选个数不足 / 过多
        (
            json.dumps(
                {
                    "search_hypothesis": "x",
                    "target_bottleneck": "y",
                    "expected_tradeoff": "z",
                    "stop_recommendation": False,
                    "candidates": [{"rcomp": 1.0, "ccomp": 1e-9}],
                }
            ),
            "candidates",
        ),
        # 候选里多了一个契约外的键
        (
            json.dumps(
                {
                    "search_hypothesis": "x",
                    "target_bottleneck": "y",
                    "expected_tradeoff": "z",
                    "stop_recommendation": False,
                    "candidates": [
                        {"rcomp": 1.0, "ccomp": 1e-9, "ccomp2": 1e-9}
                    ] * MIN_CANDIDATES,
                }
            ),
            "ccomp2",
        ),
        # 取值非正
        (
            json.dumps(
                {
                    "search_hypothesis": "x",
                    "target_bottleneck": "y",
                    "expected_tradeoff": "z",
                    "stop_recommendation": False,
                    "candidates": [{"rcomp": -1.0, "ccomp": 1e-9}] * MIN_CANDIDATES,
                }
            ),
            "rcomp",
        ),
    ],
)
def test_malformed_outputs_are_rejected_with_a_pointed_message(
    raw: str, expected_path_fragment: str
) -> None:
    """各类畸形输出都被拒绝，且错误清单指向具体位置。

    清单要可操作：说明哪个字段路径、错在哪、应该是什么。pydantic 的原始报错带
    `url` 与 `input_value` 的完整 repr，对"该怎么改"没有指向。
    """
    payload, violations = parse_proposal_payload(raw)

    assert payload is None
    assert violations
    text = format_violations(violations)
    assert expected_path_fragment in text
    assert "应为" in text


def test_markdown_fenced_json_is_not_silently_unwrapped() -> None:
    """裹在代码块里的 JSON 被判为不合格，不做剥壳容错。

    请求时已明确要求返回纯 JSON 对象。悄悄剥壳会让"模型是否遵守输出格式"永远
    测不出来，而那恰是要统计的指标。
    """
    fenced = f"```json\n{_valid_payload_json()}\n```"
    payload, violations = parse_proposal_payload(fenced)

    assert payload is None
    assert any("JSON" in v.problem for v in violations)


def test_format_instruction_stays_in_sync_with_the_validator() -> None:
    """给模型的格式说明与校验规则同源。

    两者若各写一份，改了校验忘了说明（或反之）会让模型按过时的要求作答，而失败
    原因看起来像是"模型不听话"。
    """
    instruction = response_format_instruction()

    for key in DESIGN_VARIABLE_KEYS:
        assert key in instruction
    assert str(MIN_CANDIDATES) in instruction
    assert str(MAX_CANDIDATES) in instruction


def test_upper_bound_on_candidate_count_is_enforced() -> None:
    """候选个数上限生效：一轮提太多会超出仿真预算的合理消耗。"""
    payload, violations = parse_proposal_payload(_valid_payload_json(MAX_CANDIDATES + 1))

    assert payload is None
    assert any(v.path == "candidates" for v in violations)
