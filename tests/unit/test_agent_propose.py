"""提案器的组装与失败传递。

`propose()` 本身几乎不含逻辑——它把上下文构建、请求循环、埋点串起来，三者各自已有
测试。这里验证的是串接处：输出契约是否完整、两类失败是否如实向上传递、以及无论成败
是否都留下了痕迹。

最后一条最容易被漏掉。失败也是事实：`outcome='empty'` 的记录说明"这一轮确实试过、
但模型没答上"，缺了它之后无法区分"没调用过"与"调用失败了"，而那正是判断环境稳定性
所需的证据。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from poweragent.agent.client import LlmNetworkError, LlmResponse
from poweragent.agent.propose import propose
from poweragent.agent.state import (
    ConstraintSpec,
    DomainSpec,
    SearchState,
    TestedPoint,
)
from poweragent.agent.trace import read_output_artifact
from poweragent.controller.stop import BestRecord
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Store

pytestmark = pytest.mark.agent

TASK_ID = "propose_task"


class ScriptedClient:
    """按脚本返回文本或抛网络异常的假客户端；不发任何网络请求。"""

    def __init__(self, script: Sequence[object], *, model_id: str = "fake-model") -> None:
        self.model_id = model_id
        self._script = list(script)
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, user: str) -> LlmResponse:
        self.calls.append({"system": system, "user": user})
        if not self._script:
            raise AssertionError("假客户端脚本已用尽")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResponse(text=str(item), prompt_tokens=120, completion_tokens=80)


def _payload_json(n: int = 3) -> str:
    return json.dumps(
        {
            "search_hypothesis": "沿增大 rcomp 方向提升带宽",
            "target_bottleneck": "恢复时间",
            "expected_tradeoff": "相位裕量下降",
            "stop_recommendation": False,
            "candidates": [
                {"rcomp": 18738.0 + i, "ccomp": 8.111e-10} for i in range(n)
            ],
        }
    )


@pytest.fixture()
def store(tmp_path: Path) -> Store:
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


@pytest.fixture()
def state() -> SearchState:
    return SearchState(
        legal_domain={
            "rcomp": DomainSpec(
                unit="ohm", low=1e3, high=1e5, scale="log",
                ticks=("1000", "18738", "100000"),
            ),
            "ccomp": DomainSpec(
                unit="F", low=1e-10, high=1e-8, scale="log",
                ticks=("1e-10", "8.111e-10", "1e-08"),
            ),
        },
        hard_constraints={
            "vout_min": ConstraintSpec(value=0.75, sense="lower", unit="V"),
            "phase_margin_min": ConstraintSpec(value=45.0, sense="lower", unit="deg"),
        },
        current_best=BestRecord(candidate_id="cand_base", value=47.0),
        tested_candidates=(
            TestedPoint(
                candidate_id="cand_base",
                parameters_si={"rcomp": 12000.0, "ccomp": 2.2e-9},
                feasible=True,
                objective_value=47.0,
                features=None,
            ),
        ),
        failed_regions=(),
        remaining_budget=194,
    )


def _propose(client, store, artifacts, state, *, repair=2, retries=1):
    return propose(
        state,
        client=client,
        store=store,
        artifacts=artifacts,
        task_id=TASK_ID,
        max_repair_rounds=repair,
        max_network_retries=retries,
    )


# --------------------------------------------------------------------------
# 成功路径
# --------------------------------------------------------------------------


def test_successful_proposal_returns_the_full_contract(
    store, artifacts, state
) -> None:
    """成功时七字段齐备，候选是未经校验的原始取值。

    候选刻意带上 `+i` 的偏移（18739、18740 不是档位值）：提案器不做校验，原始取值
    应原样传出，由 `agent.validate` 去拒绝。若这里返回的是被修正过的值，说明提案器
    越权做了校验器的事。
    """
    client = ScriptedClient([_payload_json()])

    result = _propose(client, store, artifacts, state)

    assert result.outcome == "ok"
    assert result.has_candidates
    assert len(result.candidates) == 3
    assert result.candidates[1]["rcomp"] == 18739.0, "原始取值应原样传出，不被修正"

    assert result.search_hypothesis
    assert result.target_bottleneck
    assert result.expected_tradeoff
    assert result.stop_recommendation is False
    assert result.llm_call_id
    assert result.context_hash
    assert result.tokens == 200
    assert result.evidence_ids == ()


def test_system_prompt_and_context_are_actually_sent(store, artifacts, state) -> None:
    """发出的消息里确实带了系统提示与由状态构建的上下文。

    这条防的是"组装漏了一环"：`build_prompt` 与 `SYSTEM_PROMPT` 各自有测试，但如果
    `propose()` 忘了把它们传下去，那些测试仍然全绿。
    """
    client = ScriptedClient([_payload_json()])

    _propose(client, store, artifacts, state)

    sent = client.calls[0]
    assert "确定性校验器" in sent["system"], "系统提示应说明权限边界"
    assert "18738" in sent["user"], "上下文应含展开后的档位"
    assert "≥ 45.0 deg" in sent["user"], "上下文应含约束方向"
    assert "194" in sent["user"], "上下文应含剩余预算"


def test_repaired_outcome_is_reported_distinctly(store, artifacts, state) -> None:
    """修复后成功的 `outcome` 是 `repaired`，不与首次通过混为一谈。

    这两个值是"一次通过率"与"修复后通过率"的唯一来源。
    """
    client = ScriptedClient(['{"bad": 1}', _payload_json()])

    result = _propose(client, store, artifacts, state)

    assert result.outcome == "repaired"
    assert result.has_candidates
    # 两次尝试的 token 都计入。
    assert result.tokens == 400


# --------------------------------------------------------------------------
# 两类失败如实传递
# --------------------------------------------------------------------------


def test_schema_invalid_yields_no_candidates_but_keeps_the_outcome(
    store, artifacts, state
) -> None:
    """修复轮耗尽：候选为空，但 `outcome` 记下是格式问题。

    与网络失败区分开很重要：一个始终答错格式的模型是模型质量问题，而网络失败是环境
    问题，两者的应对完全不同。
    """
    client = ScriptedClient(['{"bad": 1}'] * 3)

    result = _propose(client, store, artifacts, state, repair=2)

    assert result.outcome == "schema_invalid"
    assert not result.has_candidates
    assert result.llm_call_id, "失败也要留下调用记录"


def test_empty_outcome_when_network_retries_are_exhausted(
    store, artifacts, state
) -> None:
    """网络重试耗尽：`outcome='empty'`，候选为空。"""
    client = ScriptedClient([LlmNetworkError("timeout")] * 2)

    result = _propose(client, store, artifacts, state, retries=1)

    assert result.outcome == "empty"
    assert not result.has_candidates
    assert result.tokens == 0, "没拿到任何回复，token 为 0"


def test_failure_leaves_text_fields_empty_rather_than_fabricated(
    store, artifacts, state
) -> None:
    """失败时三个文本字段留空，不填一句"调用失败"。

    那种占位文本会在报告里显示成模型说过的话，而它其实是我们写的。
    """
    client = ScriptedClient([LlmNetworkError("timeout")] * 2)

    result = _propose(client, store, artifacts, state, retries=1)

    assert result.search_hypothesis == ""
    assert result.target_bottleneck == ""
    assert result.expected_tradeoff == ""
    assert result.stop_recommendation is False


# --------------------------------------------------------------------------
# 埋点：无论成败都留痕
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "expected_outcome"),
    [
        ([_payload_json()], "ok"),
        (['{"bad": 1}', _payload_json()], "repaired"),
        (['{"bad": 1}'] * 3, "schema_invalid"),
        ([LlmNetworkError("timeout")] * 2, "empty"),
    ],
    ids=["ok", "repaired", "schema_invalid", "empty"],
)
def test_every_outcome_writes_a_row_and_artefacts(
    store, artifacts, state, script, expected_outcome
) -> None:
    """四种结果都写一行 `llm_calls` 并落两份产物。

    失败也是事实。缺了这行记录，之后无法区分"没调用过"与"调用失败了"，而那是判断
    环境稳定性所需的证据。
    """
    client = ScriptedClient(list(script))

    result = _propose(client, store, artifacts, state, repair=2, retries=1)

    assert result.outcome == expected_outcome

    row = store.connection.execute(
        "SELECT task_id, role, model_id, outcome, context_hash FROM llm_calls "
        "WHERE llm_call_id=?",
        (result.llm_call_id,),
    ).fetchone()
    assert row == (TASK_ID, "proposal", "fake-model", expected_outcome, result.context_hash)


def test_artefacts_record_every_attempt_including_failures(
    store, artifacts, state, tmp_path: Path
) -> None:
    """产物里保留全部尝试，含失败那次的原文与错误清单。

    一次格式失败的现场事后无法复现，它是分析"模型为什么答错"的唯一依据。
    """
    client = ScriptedClient(['{"search_hypothesis": "只给一个字段"}', _payload_json()])

    result = _propose(client, store, artifacts, state)

    # 产物文件名与 llm_call_id 同名，因此从任一侧都能定位另一侧。这里用 fixture 传给
    # ArtifactStore 的那个根目录来拼路径。
    llm_dir = tmp_path / "artifacts" / TASK_ID / "llm"
    output_path = llm_dir / f"{result.llm_call_id}.output.json"
    prompt_path = llm_dir / f"{result.llm_call_id}.prompt.txt"

    assert output_path.is_file() and prompt_path.is_file()

    recorded = read_output_artifact(str(output_path))
    assert len(recorded["attempts"]) == 2
    assert "只给一个字段" in recorded["attempts"][0]["raw_text"]
    assert recorded["attempts"][0]["violations"], "失败尝试应带错误清单"
    assert recorded["attempts"][1]["violations"] == []

    # prompt 产物应是真实发出的上下文，而不是空文件。
    assert "18738" in prompt_path.read_text(encoding="utf-8")


def test_context_hash_is_stable_across_calls_with_the_same_state(
    store, artifacts, state
) -> None:
    """相同状态两次提案得到相同 `context_hash`。

    它标识"模型看到的是哪个状态"，是缓存判等与复现性的依据。
    """
    first = _propose(ScriptedClient([_payload_json()]), store, artifacts, state)
    second = _propose(ScriptedClient([_payload_json()]), store, artifacts, state)

    assert first.context_hash == second.context_hash
    assert first.llm_call_id != second.llm_call_id, "两次调用是两条记录"
