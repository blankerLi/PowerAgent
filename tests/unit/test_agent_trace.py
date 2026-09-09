"""LLM 调用埋点与产物落盘。

一次调用留下三处痕迹：`llm_calls` 表行、prompt 原文产物、全部原始输出产物。三者
用同一个 `llm_call_id` 关联，从任一侧都能找到另一侧。

这里重点验证两件容易做错的事：

- **token 记账含失败的修复轮**。只记成功那次会让预算偏低，而修复轮同样烧 token。
- **失败尝试的原文要留下**。一次格式失败的现场事后无法复现（同一 prompt 再问未必
  再犯同样的错），只留成功输出会让修复轮变成只有计数没有内容的黑箱。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poweragent.agent.client import Attempt, StructuredRequest, prompt_hash
from poweragent.agent.schema import SchemaViolation
from poweragent.agent.trace import (
    ARTIFACT_KIND,
    ROLE_PROPOSAL,
    read_output_artifact,
    record_llm_call,
)
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Store

pytestmark = pytest.mark.agent

TASK_ID = "trace_task"
CONTEXT_HASH = "c" * 64


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    """带一条 `tasks` 行的空库。`llm_calls.task_id` 有外键约束，必须先有任务行。"""
    s = Store(tmp_path / "runs.db")
    s.create_task(
        task_id=TASK_ID,
        simulation_only=True,
        task_kind="optimize",
        model_package_hash="m" * 64,
        metrics_hash="e" * 64,
        constraints_hash="n" * 64,
        scenario_set_hash="s" * 64,
        execution_env_hash="x" * 64,
        calibration_hash="",
        budget_max_starts=200,
    )
    return s


@pytest.fixture()
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(base_dir=tmp_path / "artifacts")


SENT_PROMPT = "prompt round 0"


def _attempt(index: int, *, raw: str, violations=()) -> Attempt:
    return Attempt(
        index=index,
        user_prompt=f"prompt round {index}",
        raw_text=raw,
        prompt_tokens=100,
        completion_tokens=50,
        violations=tuple(violations),
    )


def _request(
    outcome: str,
    attempts: tuple[Attempt, ...] = (),
    *,
    network_failures: tuple[str, ...] = (),
    sent_prompt: str = SENT_PROMPT,
) -> StructuredRequest:
    """构造一个 `StructuredRequest`。

    `payload` 恒为 `None`：本模块只落痕迹，不关心解析结果。`sent_prompt` 必须给，
    它是"我们发出去了什么"，与是否收到回复无关。
    """
    return StructuredRequest(
        payload=None,
        outcome=outcome,  # type: ignore[arg-type]
        sent_prompt=sent_prompt,
        attempts=attempts,
        network_failures=network_failures,
    )


def test_successful_call_writes_row_and_two_artefacts(store, artifacts) -> None:
    """`ok` 的调用写一行并落两份产物，文件名与 `llm_call_id` 同名。"""
    request = _request("ok", (_attempt(0, raw='{"ok": true}'),))

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        model_id="deepseek-chat",
        context_hash=CONTEXT_HASH,
    )

    assert trace.llm_call_id
    assert Path(trace.prompt_ref).is_file()
    assert Path(trace.output_ref).is_file()
    assert trace.llm_call_id in Path(trace.prompt_ref).name
    assert ARTIFACT_KIND in Path(trace.prompt_ref).parts

    row = store.connection.execute(
        "SELECT task_id, role, model_id, prompt_hash, context_hash, tokens, outcome "
        "FROM llm_calls WHERE llm_call_id=?",
        (trace.llm_call_id,),
    ).fetchone()
    assert row == (
        TASK_ID,
        ROLE_PROPOSAL,
        "deepseek-chat",
        prompt_hash("prompt round 0"),
        CONTEXT_HASH,
        150,
        "ok",
    )


def test_token_accounting_includes_failed_repair_rounds(store, artifacts) -> None:
    """token 取全部尝试之和，含失败的修复轮。

    修复轮同样发请求、同样烧 token。只记成功那一次会让 `max_engine_starts` 之外的
    token 预算系统性偏低，而 token 消耗是要写进报告的指标之一。
    """
    request = _request(
        "repaired",
        (
            _attempt(0, raw="{}", violations=[SchemaViolation("candidates", "缺少", "必须提供")]),
            _attempt(1, raw='{"ok": true}'),
        ),
    )

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        model_id="deepseek-chat",
        context_hash=CONTEXT_HASH,
    )

    assert trace.tokens == 300, "两次尝试各 150"
    stored = store.connection.execute(
        "SELECT tokens FROM llm_calls WHERE llm_call_id=?", (trace.llm_call_id,)
    ).fetchone()[0]
    assert stored == 300


def test_failed_attempts_are_preserved_in_the_output_artefact(store, artifacts) -> None:
    """失败尝试的原文与错误清单都落盘。

    这是分析"模型为什么答错"的唯一依据，而现场事后无法复现。
    """
    request = _request(
        "repaired",
        (
            _attempt(
                0,
                raw='{"search_hypothesis": "只给了一个字段"}',
                violations=[SchemaViolation("candidates", "缺少该字段", "必须提供")],
            ),
            _attempt(1, raw='{"ok": true}'),
        ),
    )

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        model_id="deepseek-chat",
        context_hash=CONTEXT_HASH,
    )

    recorded = read_output_artifact(trace.output_ref)
    assert len(recorded["attempts"]) == 2

    failed = recorded["attempts"][0]
    assert "只给了一个字段" in failed["raw_text"]
    assert failed["violations"][0]["path"] == "candidates"
    assert failed["violations"][0]["problem"] == "缺少该字段"

    succeeded = recorded["attempts"][1]
    assert succeeded["violations"] == []


def test_empty_outcome_still_writes_a_row(store, artifacts) -> None:
    """网络重试耗尽、没有任何尝试时，仍写一行 `outcome='empty'`、token 为 0。

    这条记录本身是"这一轮确实试过、但模型没答上"的证据。不写行会让这一轮在事实源
    里彻底消失，之后无法区分"没调用过"与"调用失败了"。
    """
    request = _request(
        "empty",
        (),
        network_failures=("attempt 0: timeout", "attempt 1: timeout"),
    )

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        model_id="deepseek-chat",
        context_hash=CONTEXT_HASH,
    )

    assert trace.outcome == "empty"
    assert trace.tokens == 0

    row = store.connection.execute(
        "SELECT tokens, outcome FROM llm_calls WHERE llm_call_id=?",
        (trace.llm_call_id,),
    ).fetchone()
    assert row == (0, "empty")

    # 网络失败原因落在产物里，供事后判断是超时还是限流。
    recorded = read_output_artifact(trace.output_ref)
    assert recorded["attempts"] == []
    assert len(recorded["network_failures"]) == 2


def test_database_stores_hashes_not_prompt_text(store, artifacts) -> None:
    """事实表只存哈希，不存 prompt 原文。

    原文可能上万字符，塞进事实表既臃肿又让"这次调用用的是哪份 prompt"这个问题
    需要做字符串比对。哈希足以回答它，原文留在产物里。
    """
    secret_marker = "THIS_TEXT_MUST_NOT_REACH_THE_DATABASE"
    request = _request(
        "ok",
        (
            Attempt(
                index=0,
                user_prompt=f"prompt containing {secret_marker}",
                raw_text="{}",
                prompt_tokens=10,
                completion_tokens=5,
                violations=(),
            ),
        ),
        sent_prompt=f"prompt containing {secret_marker}",
    )

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        model_id="deepseek-chat",
        context_hash=CONTEXT_HASH,
    )

    dumped = "\n".join(store.connection.iterdump())
    assert secret_marker not in dumped, "prompt 原文不应出现在数据库中"

    # 但产物里必须有，否则追溯断链。
    assert secret_marker in Path(trace.prompt_ref).read_text(encoding="utf-8")


def test_output_artefact_is_valid_json_with_stable_key_order(store, artifacts) -> None:
    """输出产物是排好序的 JSON，便于 diff 与人工阅读。"""
    request = _request("ok", (_attempt(0, raw='{"a": 1}'),))

    trace = record_llm_call(
        request,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        model_id="deepseek-chat",
        context_hash=CONTEXT_HASH,
    )

    text = Path(trace.output_ref).read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert set(parsed) == {"attempts", "network_failures"}
    # sort_keys=True ⟹ attempts 在 network_failures 之前出现
    assert text.index('"attempts"') < text.index('"network_failures"')
