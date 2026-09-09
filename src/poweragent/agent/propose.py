"""提案器 —— 全系统唯一的 LLM 角色。

它把三件已经各自可测的东西串起来：`prompt.build_prompt()` 造上下文、
`client.request_proposal()` 发请求并处理两类失败、`trace.record_llm_call()` 落埋点与
产物。本模块自己几乎不含逻辑，这是有意的——串接处越薄，出错的余地越小。

四件它不做
----------
**不调仿真。** 提案器不持有 `MatlabSession` / `PythonSession`，也不 import `sim`。
候选是否可行由仿真与 `eval` 判定，与提案器无关。

**不写事实状态。** 它写 `llm_calls` 行与 `artifacts/llm/` 产物——那是对"发生过一次
调用"的记录，不是对搜索结论的断言。候选、指标、约束判定、被拒记录都由 `controller`
写入。

**不校验候选。** 输出的是**未经校验的原始取值**。校验由 `agent.validate` 完成，
且提案器无权决定它的严格度。

**不决定停止。** `stop_recommendation` 只是建议，`controller.should_stop()` 才有决定权。

两类失败如实向上传递
--------------------
`ProposalOutput.candidates` 为空有两种成因：模型始终答不出合格格式
（`outcome='schema_invalid'`），或者网络重试耗尽、根本没答上（`outcome='empty'`）。
两者都表现为"本轮无候选"，但 `outcome` 保留了区别，因为它们对"模型质量"与"环境稳定性"
是完全不同的证据。本模块不把它们合并成一个布尔量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from poweragent.agent.client import (
    LlmClient,
    StructuredRequest,
    request_proposal,
)
from poweragent.agent.prompt import SYSTEM_PROMPT, build_prompt
from poweragent.agent.state import SearchState
from poweragent.agent.trace import record_llm_call
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Store

__all__ = ["ProposalOutput", "propose"]


@dataclass(frozen=True, slots=True)
class ProposalOutput:
    """一次提案的完整输出（`design.md` §6.6）。

    三个文本字段是提案的可解释部分：它们让工程师能判断模型的推理是否合理，而不是只看到
    一串坐标。这也是 LLM 相对纯数值优化器的实际价值之一，因此它们进报告。

    `candidates` 是**未经校验**的原始取值。`llm_call_id` 把这次提案与 `llm_calls` 行、
    以及 `artifacts/llm/` 下的 prompt 与原始输出关联起来——被拒候选落 `rejections` 表时
    也带这个 id，因此"哪次调用提出了这个被拒的点"可以追溯。

    `evidence_ids` 恒为空：工程知识检索在范围裁剪中被移除。保留该字段是因为它出现在
    `llm_calls.evidence_ids` 列里，而那一列属于事实表的既有结构。
    """

    search_hypothesis: str
    target_bottleneck: str
    expected_tradeoff: str
    stop_recommendation: bool
    candidates: tuple[Mapping[str, float], ...]
    llm_call_id: str
    outcome: str
    tokens: int
    context_hash: str
    evidence_ids: Sequence[str] = field(default_factory=tuple)

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)


def propose(
    state: SearchState,
    *,
    client: LlmClient,
    store: Store,
    artifacts: ArtifactStore,
    task_id: str,
    max_repair_rounds: int,
    max_network_retries: int,
) -> ProposalOutput:
    """依只读状态提出一批候选。

    相对 `design.md` §6.6 的字面签名（`propose(state, *, client, max_repair_rounds)`）
    多了 `store` / `artifacts` / `task_id` / `max_network_retries` 四项。前三项是埋点
    落地所需：`llm_calls` 行与产物必须在调用发生时写下，事后无法补录（原始输出不可
    复现）。`max_network_retries` 是因为两类失败的重试上限来自不同配置节
    （`budget.max_llm_repair_rounds` 与 `llm.max_network_retries`），把它们合成一个
    参数就等于承认两者可以共用一个计数——而分开正是本项目的设计要点。

    无论成败都写一条 `llm_calls` 行。失败也是事实：`outcome='empty'` 的记录说明"这一轮
    确实试过、但模型没答上"，缺了它之后无法区分"没调用过"与"调用失败了"。
    """
    user_prompt, context_hash = build_prompt(state)

    request: StructuredRequest = request_proposal(
        client,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_repair_rounds=max_repair_rounds,
        max_network_retries=max_network_retries,
    )

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=task_id,
        model_id=client.model_id,
        context_hash=context_hash,
    )

    payload = request.payload
    if payload is None:
        # 两类失败都落到这里，但 outcome 保留了它们的区别。文本字段留空而不是编造
        # 一句"调用失败"——那会让报告里出现一条看似是模型说的话。
        return ProposalOutput(
            search_hypothesis="",
            target_bottleneck="",
            expected_tradeoff="",
            stop_recommendation=False,
            candidates=(),
            llm_call_id=trace.llm_call_id,
            outcome=request.outcome,
            tokens=trace.tokens,
            context_hash=context_hash,
        )

    return ProposalOutput(
        search_hypothesis=payload.search_hypothesis,
        target_bottleneck=payload.target_bottleneck,
        expected_tradeoff=payload.expected_tradeoff,
        stop_recommendation=payload.stop_recommendation,
        candidates=payload.candidate_parameters(),
        llm_call_id=trace.llm_call_id,
        outcome=request.outcome,
        tokens=trace.tokens,
        context_hash=context_hash,
    )
