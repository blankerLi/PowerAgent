"""LLM 客户端与结构化请求循环。

两类失败必须分开计数
--------------------
这是本模块最重要的一条设计。模型调用有两种完全不同的失败：

- **输出不合 schema**：模型答了，但格式不对。处理方式是把错误清单回灌让它重说，
  计入 `budget.max_llm_repair_rounds`。
- **网络失败或超时**：模型没答。处理方式是指数退避重试，计入
  `llm.max_network_retries`。

把两者混在同一个重试计数里会同时坏两件事：一次网络抖动会吃掉本该留给格式修复的
配额；而一个始终答错格式的模型会被当成网络不稳定反复重试。它们的根因、正确的
应对、以及"重试多少次才该放弃"都不一样，所以计数分开、退出条件分开、落库的
`outcome` 也分开。

网络重试耗尽等价于本轮无候选（`outcome='empty'`），由 `run_task()` 按空轮分支
处理，不引入新的失败分类。修复轮耗尽则是 `outcome='schema_invalid'`——模型确实
答了，只是始终不合格式，这是一条需要被统计的质量信号（一次通过率与修复后通过率
都从这里来）。

凭据只从环境变量读
------------------
API Key 只从 `POWERAGENT_LLM_API_KEY` 读取，不写入配置文件、不入库、不进产物。
`llm_calls` 表只存 prompt 哈希与 token 数（`design.md` §14）。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence

from poweragent.agent.schema import (
    ProposalPayload,
    SchemaViolation,
    format_violations,
    parse_proposal_payload,
    response_format_instruction,
)

__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "MODEL_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "LlmResponse",
    "LlmClient",
    "LlmNetworkError",
    "DeepSeekClient",
    "Attempt",
    "StructuredRequest",
    "request_proposal",
    "prompt_hash",
    "total_tokens_of",
]

API_KEY_ENV = "POWERAGENT_LLM_API_KEY"
BASE_URL_ENV = "POWERAGENT_LLM_BASE_URL"
MODEL_ENV = "POWERAGENT_LLM_MODEL"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """一次成功往返的结果。

    token 分 prompt / completion 两项返回，但落库时合并为单列——`llm_calls.tokens`
    的口径是两者之和（`design.md` §5.1 明确不分列：分列的唯一用途是成本核算，
    而成本已从状态行删除）。这里仍分开返回，是因为"prompt 占了多少上下文"对判断
    压缩是否有效有用，而那是本模块之外的分析。
    """

    text: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LlmNetworkError(RuntimeError):
    """网络层失败：连接错误、超时、服务端 5xx、限流。

    与"输出不合 schema"严格区分——只有本异常才触发指数退避重试。客户端实现负责把
    底层 SDK 的各种异常归入本类，使重试循环不必知道任何供应商细节。
    """


class LlmClient(Protocol):
    """客户端接口。

    用 `Protocol` 而不是抽象基类：它只是给类型检查与测试替身一个共同形状，不产生
    运行时继承关系，也不需要谁去 `register`。测试里的假客户端只要有这两个成员即可，
    不必导入本模块。
    """

    model_id: str

    def complete(self, *, system: str, user: str) -> LlmResponse: ...


class DeepSeekClient:
    """DeepSeek 的 OpenAI 兼容接口实现。

    走 `openai` SDK 并把 `base_url` 指向 DeepSeek，而不是自己拼 HTTP 请求：重试
    语义、超时处理、错误类型都由 SDK 负责，本类只做两件事——注入凭据与配置，
    以及把 SDK 的异常归一化成 `LlmNetworkError`。

    `response_format={"type": "json_object"}` 让服务端保证返回合法 JSON。即使如此，
    `agent.schema` 的校验仍然必须做：JSON 合法不等于结构符合契约，候选个数、键集合、
    取值范围都还得查。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model_id: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        if not key:
            raise RuntimeError(
                f"缺少 API Key：请设置环境变量 {API_KEY_ENV}。"
                "凭据不从配置文件读取，也不写入数据库或产物。"
            )

        from openai import OpenAI  # 延迟导入：无需 LLM 的测试与 CI 不应因此失败

        self.model_id = model_id or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self._timeout_s = timeout_s
        self._client = OpenAI(
            api_key=key,
            base_url=base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL,
            timeout=timeout_s,
            max_retries=0,  # 重试由 request_proposal() 统一管，避免两层退避叠加
        )

    def complete(self, *, system: str, user: str) -> LlmResponse:
        from openai import APIError, APITimeoutError, RateLimitError

        try:
            completion = self._client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=1.0,
            )
        except (APITimeoutError, RateLimitError) as exc:
            raise LlmNetworkError(f"{type(exc).__name__}: {exc}") from exc
        except APIError as exc:
            # 4xx 里的参数错误不是网络问题，但把它也归为网络类会让重试白跑几次后
            # 以 empty 收尾——这比伪装成 schema 失败要好：那会污染一次通过率统计。
            raise LlmNetworkError(f"{type(exc).__name__}: {exc}") from exc

        usage = completion.usage
        return LlmResponse(
            text=completion.choices[0].message.content or "",
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    """一次模型往返的完整记录，供落盘与事后复核。

    保留 `raw_text` 原文而非仅保留解析结果：一次格式失败的现场是分析"模型为什么
    答错"的唯一依据，而这类分析事后无法复现（同一 prompt 再问一次未必重现）。
    """

    index: int
    user_prompt: str
    raw_text: str
    prompt_tokens: int
    completion_tokens: int
    violations: tuple[SchemaViolation, ...]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class StructuredRequest:
    """结构化请求的最终结果。

    `outcome` 与 `llm_calls.outcome` 的四值枚举同源：

    - `ok`：首次调用即通过 schema 校验
    - `repaired`：首次不合格，回灌错误清单后通过
    - `schema_invalid`：修复轮耗尽，始终不合格式
    - `empty`：网络重试耗尽，模型始终没答上

    `payload` 仅在 `ok` / `repaired` 时非空。`attempts` 含全部**拿到了文本**的尝试，
    `total_tokens` 是它们的和——修复轮同样烧 token，记账不能只算成功那一次。

    `sent_prompt` 单独记录首次实际发出的用户消息，不依赖 `attempts`：prompt 是"我们
    发了什么"，与"是否收到回复"无关。网络重试全部失败时 `attempts` 为空，但那一轮
    确实发过请求，prompt 必须留得下来——否则 `outcome='empty'` 的记录会既无 token
    也无 prompt，退化成一条只说明"失败过"的空壳，事后无从判断当时问的是什么。
    """

    payload: ProposalPayload | None
    outcome: Literal["ok", "schema_invalid", "repaired", "empty"]
    sent_prompt: str = ""
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)
    network_failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_tokens(self) -> int:
        return sum(a.total_tokens for a in self.attempts)

    @property
    def repair_rounds_used(self) -> int:
        """实际用掉的修复轮数（首次调用不算修复轮）。"""
        return max(0, len(self.attempts) - 1)


def _sleep_backoff(attempt_index: int, *, base_s: float) -> None:
    """指数退避：base * 2^n。attempt_index 从 0 起。"""
    time.sleep(base_s * (2**attempt_index))


def _complete_with_network_retry(
    client: LlmClient,
    *,
    system: str,
    user: str,
    max_network_retries: int,
    backoff_base_s: float,
    failures: list[str],
) -> LlmResponse | None:
    """发一次请求，网络类失败按指数退避重试；耗尽返回 `None`。

    重试次数不与修复轮共享计数（见模块 docstring）。`max_network_retries` 的语义是
    "重试次数"，因此总尝试次数为它加一。
    """
    for attempt_index in range(max_network_retries + 1):
        try:
            return client.complete(system=system, user=user)
        except LlmNetworkError as exc:
            failures.append(f"attempt {attempt_index}: {exc}")
            if attempt_index >= max_network_retries:
                return None
            _sleep_backoff(attempt_index, base_s=backoff_base_s)
    return None


def request_proposal(
    client: LlmClient,
    *,
    system_prompt: str,
    user_prompt: str,
    max_repair_rounds: int,
    max_network_retries: int,
    backoff_base_s: float = 1.0,
) -> StructuredRequest:
    """请求一次提案，处理两类失败并返回带 `outcome` 的结果。

    控制流：每一轮先做带网络退避的单次往返；拿到文本后校验 schema。通过即结束
    （首轮 `ok`，非首轮 `repaired`）；不通过则把错误清单拼进下一轮的用户消息，
    直到修复轮用尽（`schema_invalid`）。任一轮的网络重试耗尽即整体以 `empty`
    结束——模型没答上时没有可回灌的内容，继续修复轮没有意义。

    本函数不写数据库、不落盘、不生成 `llm_call_id`：那些是调用方
    （`agent.propose`）的职责。这里只负责"把一次结构化请求做对"，因此可以完全用
    假客户端测试，不需要数据库或文件系统。
    """
    attempts: list[Attempt] = []
    network_failures: list[str] = []
    current_user = f"{user_prompt}\n\n{response_format_instruction()}"
    sent_prompt = current_user  # 首次实际发出的消息，与是否收到回复无关

    for round_index in range(max_repair_rounds + 1):
        response = _complete_with_network_retry(
            client,
            system=system_prompt,
            user=current_user,
            max_network_retries=max_network_retries,
            backoff_base_s=backoff_base_s,
            failures=network_failures,
        )
        if response is None:
            return StructuredRequest(
                payload=None,
                outcome="empty",
                sent_prompt=sent_prompt,
                attempts=tuple(attempts),
                network_failures=tuple(network_failures),
            )

        payload, violations = parse_proposal_payload(response.text)
        attempts.append(
            Attempt(
                index=round_index,
                user_prompt=current_user,
                raw_text=response.text,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                violations=violations,
            )
        )

        if payload is not None:
            return StructuredRequest(
                payload=payload,
                outcome="ok" if round_index == 0 else "repaired",
                sent_prompt=sent_prompt,
                attempts=tuple(attempts),
                network_failures=tuple(network_failures),
            )

        if round_index >= max_repair_rounds:
            break

        current_user = (
            f"{user_prompt}\n\n{response_format_instruction()}\n\n"
            f"{format_violations(violations)}"
        )

    return StructuredRequest(
        payload=None,
        outcome="schema_invalid",
        sent_prompt=sent_prompt,
        attempts=tuple(attempts),
        network_failures=tuple(network_failures),
    )


def prompt_hash(text: str) -> str:
    """prompt 文本的 sha256，落 `llm_calls.prompt_hash`。

    只存哈希不存原文：原文落在 `artifacts/llm/` 下的产物里，数据库保留哈希即可
    完成"这次调用用的是哪份 prompt"的追溯，同时避免把可能很长的文本塞进事实表。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def total_tokens_of(attempts: Sequence[Attempt]) -> int:
    """全部尝试的 token 之和，落 `llm_calls.tokens`。"""
    return sum(a.total_tokens for a in attempts)
