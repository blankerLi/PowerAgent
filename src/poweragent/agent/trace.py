"""LLM 调用的埋点与产物落盘。

一次模型调用留下三处痕迹，各有不同用途：

1. **`llm_calls` 表**（事实源）：谁、用哪个模型、prompt 与上下文的哈希、烧了多少
   token、结果如何。只存哈希与计数，不存原文——事实表不该塞进可能上万字符的文本，
   而哈希已足够回答"这次调用用的是哪份 prompt"。
2. **`artifacts/<task>/llm/<id>.prompt.txt`**：完整的 prompt 原文。
3. **`artifacts/<task>/llm/<id>.output.json`**：全部尝试的原始返回，含失败那几次。

为什么要留失败尝试的原文
------------------------
一次格式失败的现场是分析"模型为什么答错"的唯一依据，而这类现场事后无法复现：同一
份 prompt 再问一次未必再犯同样的错。只保留成功那次的输出，等于把修复轮变成一个只有
计数、没有内容的黑箱，而"一次通过率"这个指标背后的原因也就查不下去了。

凭据不进任何一处
----------------
API Key 只存在于环境变量与客户端实例中，不进数据库、不进产物、不进日志。
`llm_calls` 只有哈希与 token 数（`design.md` §14）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from poweragent.agent.client import Attempt, StructuredRequest, prompt_hash
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import LlmCallRecord, Store

__all__ = ["LlmTrace", "record_llm_call", "ARTIFACT_KIND", "ROLE_PROPOSAL"]

# 产物子目录名，取自 `store/layout.py` 登记的七个子目录之一。
ARTIFACT_KIND = "llm"

# 阶段 1 只有一个 LLM 角色（`design.md`：LLM 角色 1）。写成常量而不是到处写字面量，
# 是为了让"系统里只有一个角色"这件事在代码里可见。
ROLE_PROPOSAL = "proposal"


@dataclass(frozen=True, slots=True)
class LlmTrace:
    """一次调用留下的痕迹的引用。

    `prompt_ref` / `output_ref` 是归档后的产物路径。`llm_call_id` 由
    `store.log_llm_call()` 生成，随后被用作产物文件名——因此产物与事实行天然同名，
    从任一侧都能找到另一侧。
    """

    llm_call_id: str
    prompt_ref: str
    output_ref: str
    tokens: int
    outcome: str


def _attempts_as_json(attempts: Sequence[Attempt], network_failures: Sequence[str]) -> str:
    """把全部尝试序列化为可读的 JSON。

    用缩进而非紧凑格式：这份产物是给人看的（事后复核模型的输出），不参与任何哈希
    比对，可读性优先。
    """
    return json.dumps(
        {
            "attempts": [
                {
                    "index": a.index,
                    "raw_text": a.raw_text,
                    "prompt_tokens": a.prompt_tokens,
                    "completion_tokens": a.completion_tokens,
                    "violations": [
                        {"path": v.path, "problem": v.problem, "expected": v.expected}
                        for v in a.violations
                    ],
                }
                for a in attempts
            ],
            "network_failures": list(network_failures),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def record_llm_call(
    request: StructuredRequest,
    *,
    store: Store,
    artifacts: ArtifactStore,
    task_id: str,
    model_id: str,
    context_hash: str,
    evidence_ids: Sequence[str] = (),
) -> LlmTrace:
    """写 `llm_calls` 行并落盘 prompt 与全部原始输出。

    先写库再落盘：`llm_call_id` 由 `log_llm_call()` 生成，产物文件名要用它。反过来
    做需要先自己造一个 id，那会出现"产物里的 id 与表里的 id 不一致"这种可能。

    `tokens` 取全部尝试之和，包括失败的修复轮——修复轮同样烧 token，只记成功那次
    会让预算记账偏低。首轮就网络失败、没有任何尝试时 token 为 0，仍然写一行
    `outcome='empty'`：这条记录本身是"这一轮确实试过、但模型没答上"的证据。

    `prompt_hash` 取 `request.sent_prompt`——首次实际发出的消息，而不是从 `attempts`
    里取。网络重试全部失败时 `attempts` 为空，但那一轮确实发过请求，prompt 必须留得
    下来；从 `attempts` 取会得到空串，落盘时还会被 `ArtifactStore` 以"产物为空"
    拒绝。后续修复轮的 prompt 是它加上错误清单，完整内容都在产物里。
    """
    attempts = request.attempts
    first_prompt = request.sent_prompt
    if not first_prompt:
        # 前置拒绝而不是让 ArtifactStore 以"产物为空"报错——那个消息不指向根因。
        # 一次调用必然发出过某段 prompt；这里为空说明构造 StructuredRequest 时漏了它。
        raise ValueError(
            "record_llm_call(): request.sent_prompt 为空。一次调用总有发出的 prompt，"
            "即使网络重试全部失败也是如此；请检查 StructuredRequest 的构造。"
        )

    llm_call_id = store.log_llm_call(
        LlmCallRecord(
            task_id=task_id,
            role=ROLE_PROPOSAL,
            model_id=model_id,
            prompt_hash=prompt_hash(first_prompt),
            context_hash=context_hash,
            tokens=request.total_tokens,
            outcome=request.outcome,
            evidence_ids=tuple(evidence_ids),
        )
    )

    prompt_tmp = artifacts.stage(
        task_id=task_id, kind=ARTIFACT_KIND, name=f"{llm_call_id}.prompt.txt"
    )
    prompt_tmp.write_text(first_prompt, encoding="utf-8")
    prompt_ref = artifacts.commit(prompt_tmp)

    output_tmp = artifacts.stage(
        task_id=task_id, kind=ARTIFACT_KIND, name=f"{llm_call_id}.output.json"
    )
    output_tmp.write_text(
        _attempts_as_json(attempts, request.network_failures), encoding="utf-8"
    )
    output_ref = artifacts.commit(output_tmp)

    return LlmTrace(
        llm_call_id=llm_call_id,
        prompt_ref=prompt_ref,
        output_ref=output_ref,
        tokens=request.total_tokens,
        outcome=request.outcome,
    )


def read_output_artifact(output_ref: str) -> dict:
    """读回落盘的输出产物。

    供报告渲染与事后分析使用（例如统计一次通过率、查看某次格式失败的原文）。
    放在本模块而不是让调用方自己 `json.loads`，是为了让产物格式只有一处知道。
    """
    return json.loads(Path(output_ref).read_text(encoding="utf-8"))
