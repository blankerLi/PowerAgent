"""真实调用 DeepSeek 的连通性测试。

**默认跳过**：未设置 `POWERAGENT_LLM_API_KEY` 时整个模块跳过，因此 CI 与无凭据的
开发环境不受影响，也不会因为网络或额度问题产生随机失败。

它验证的是别处测不到的那一层：假客户端能覆盖全部失败路径与契约，但覆盖不了
"真实服务端会不会按 `response_format` 返回合法 JSON"、"SDK 的异常类型有没有被正确
归一化"、以及"模型在给定格式说明下的原生遵从度如何"。最后一条是要写进报告的指标
（一次通过率），只有真实调用才有意义。

要跑它：

    $env:POWERAGENT_LLM_API_KEY = "..."
    python -m pytest tests/conditional -m live_llm -v
"""

from __future__ import annotations

import os

import pytest

from poweragent.agent.client import API_KEY_ENV, DeepSeekClient, request_proposal
from poweragent.agent.schema import (
    DESIGN_VARIABLE_KEYS,
    MAX_CANDIDATES,
    MIN_CANDIDATES,
)

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(
        not os.environ.get(API_KEY_ENV),
        reason=f"未设置 {API_KEY_ENV}，跳过真实 LLM 调用",
    ),
]

# 一段最小但真实的提案请求：给出设计空间与一个当前最佳点，要求模型提候选。
# 刻意不用完整的 prompt 构建器（那是 D5 的 agent/prompt.py），本测试只验证连通性
# 与格式遵从度，不验证 prompt 质量。
SYSTEM_PROMPT = (
    "你是电源环路整定的助手。用户会给出补偿网络两个参数的合法范围与当前最佳点，"
    "你需要提出若干候选取值。只输出 JSON，不要解释。"
)
USER_PROMPT = (
    "设计变量与合法范围：\n"
    "- rcomp: 1000 ~ 100000 欧姆（对数刻度）\n"
    "- ccomp: 1e-10 ~ 1e-8 法拉（对数刻度）\n"
    "当前最佳点 rcomp=12000, ccomp=2.2e-9，最恶劣工况恢复时间 47 微秒，目标 25 微秒。\n"
    "已知：增大 rcomp 提高环路带宽、减小负载阶跃下冲，但过大会使增益裕量不足；\n"
    "减小 ccomp 会把补偿零点推高，若高于穿越频率则相位裕量不足。\n"
    "请提出下一批候选。"
)


@pytest.fixture(scope="module")
def client() -> DeepSeekClient:
    return DeepSeekClient(timeout_s=120.0)


def test_live_call_returns_a_contract_conforming_proposal(client) -> None:
    """真实调用能返回符合契约的提案。

    允许修复轮：这里测的是"整条请求循环在真实服务端上能不能拿到合格输出"，而不是
    模型的一次通过率——后者需要多次采样才有统计意义，不适合放在一条测试里。
    """
    result = request_proposal(
        client,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        max_repair_rounds=2,
        max_network_retries=2,
    )

    assert result.outcome in ("ok", "repaired"), (
        f"未能取得合格输出：outcome={result.outcome}，"
        f"网络失败={result.network_failures}，"
        f"最后一次的错误清单={result.attempts[-1].violations if result.attempts else None}"
    )
    assert result.payload is not None

    payload = result.payload
    assert MIN_CANDIDATES <= len(payload.candidates) <= MAX_CANDIDATES
    for params in payload.candidate_parameters():
        assert set(params) == set(DESIGN_VARIABLE_KEYS)
        assert all(value > 0.0 for value in params.values())

    # 三个文本字段是 LLM 相对纯数值优化器的实际价值所在，不该是空话。
    assert payload.search_hypothesis.strip()
    assert payload.target_bottleneck.strip()
    assert payload.expected_tradeoff.strip()

    # token 记账应真实反映消耗，含修复轮。
    assert result.total_tokens > 0


def test_live_call_reports_token_usage_per_attempt(client) -> None:
    """每次尝试都带回 token 计数，供 `llm_calls.tokens` 记账。

    若服务端不返回 usage，记账会静默变成 0，而 token 消耗是要写进报告的指标之一。
    """
    result = request_proposal(
        client,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        max_repair_rounds=1,
        max_network_retries=2,
    )

    assert result.attempts, "至少应有一次拿到文本的尝试"
    for attempt in result.attempts:
        assert attempt.prompt_tokens > 0, "prompt token 计数缺失"
        assert attempt.completion_tokens > 0, "completion token 计数缺失"
