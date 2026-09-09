"""用假 client 把"LLM 提案 → 校验 → 仿真 → 落库"整条链路跑一遍。

`test_run_task_smoke.py` 验证的是主循环本身（注入固定候选，不经提案器）。这条测试
补上另一半：候选真的从 `agent.propose` 出来、经 `agent.validate` 筛过、被拒的进
`rejections`、被接受的进仿真。两条测试合起来覆盖 `propose_fn` 边界的两侧。

为什么不用真实 LLM
------------------
真实模型每次答的不一样，"被拒候选是否落库"这件事就没法稳定断言——某次它恰好全答对，
测试便什么也没验证。假 client 让每一条路径都必然被走到：合法档位点被接受、越界点被
拒、重复点被新颖度拒。真实模型的验证是另一件事（属于测量而非测试），它衡量的是提案
质量，不是接线正确性。

档位必须逐字精确
----------------
`tick_match_rel_tol = 1e-6` 意味着候选取值要给到 7 位有效数字：`18738.1742286` 才是
档位，`18738` 不是（相对偏差 9.3e-6，超容差近十倍）。这条测试因此用展开后的档位字符串
构造候选，而不是手写好看的整数。这也正是 prompt 把档位表原样列给模型的原因——它需要
复制，而不是估算。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from poweragent.agent.client import LlmResponse
from poweragent.agent.schema import MIN_CANDIDATES
from poweragent.config.hashing import canonical_json, constraints_hash, metrics_hash
from poweragent.config.loader import load_all
from poweragent.config.ticks import expand_all_ticks
from poweragent.controller.llm_propose import make_llm_propose_fn
from poweragent.controller.run_task import run_task
from poweragent.controller.scenario import compute_scenario_set_hash
from poweragent.sim.backends.session import PythonSession
from poweragent.sim.hashing import model_package_hash, resolve_dependency_closure
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Store

pytestmark = pytest.mark.integration


def _freeze_configs(store: Store, bundle, repo_root: Path) -> None:
    """写入 preflight 要求的两个冻结点（与冒烟测试同一套动作，理由见那里）。"""
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    store.freeze(
        "model_package",
        hash=model_package_hash(closure, model_dump),
        detail="llm loop e2e freeze",
    )
    safety_hash = hashlib.sha256(
        canonical_json(
            {
                "constraints_hash": constraints_hash(
                    bundle.constraints.model_dump(mode="json")
                ),
                "metrics_hash": metrics_hash(bundle.metrics.model_dump(mode="json")),
                "scenario_set_hash": compute_scenario_set_hash(bundle.task),
            }
        ).encode("utf-8")
    ).hexdigest()
    store.freeze("safety", hash=safety_hash, detail="llm loop e2e freeze")


def _reply(candidates: list[dict[str, float]], *, stop: bool = False) -> str:
    """按 `agent.schema` 的契约拼一份合格的模型回复。"""
    return json.dumps(
        {
            "search_hypothesis": "沿相位裕量方向抬高 Rcomp",
            "target_bottleneck": "settling_time",
            "expected_tradeoff": "带宽换裕量",
            "stop_recommendation": stop,
            "candidates": candidates,
        }
    )


class _ScriptedClient:
    """按调用次序返回预写回复的假 client。

    实现 `LlmClient` 协议的唯一方法 `complete()`。不发网络请求，因此这条测试在 CI 上
    与本地行为一致，也不消耗额度。
    """

    model_id = "fake-scripted-v1"

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.prompts: list[str] = []

    def complete(self, *, system: str, user: str) -> LlmResponse:
        self.prompts.append(user)
        index = len(self.prompts) - 1
        # 脚本用尽后重复最后一条：主循环可能因停止判定的时序多问一轮，
        # 那种情况不该让测试以 IndexError 失败——它不是被测行为。
        text = self._replies[min(index, len(self._replies) - 1)]
        return LlmResponse(text=text, prompt_tokens=900, completion_tokens=120)


@pytest.fixture(scope="module")
def bundle(config_dir: Path):
    return load_all(config_dir)


@pytest.fixture(scope="module")
def ticks(bundle):
    return expand_all_ticks(bundle.constraints)


@pytest.fixture(scope="module")
def scripted(bundle, ticks):
    """三轮脚本：全部接受 → 全部被拒 → 建议停止。

    每轮给 `MIN_CANDIDATES` 个候选（schema 要求至少 3 个），这样回复本身合格，
    被拒只可能来自校验器而不是格式解析——两类失败不能混在一条测试里。
    """
    rc = [float(t) for t in ticks["rcomp"]]
    cc = [float(t) for t in ticks["ccomp"]]

    # 第 1 轮：三个精确落在档位上、彼此相距多档的点，应当全部通过校验。
    legal = [
        {"rcomp": rc[6], "ccomp": cc[8]},
        {"rcomp": rc[7], "ccomp": cc[5]},
        {"rcomp": rc[5], "ccomp": cc[7]},
    ]
    # 第 2 轮：三种拒绝原因各一个——越界、偏离档位、与已测点重合。
    illegal = [
        {"rcomp": rc[-1] * 2.0, "ccomp": cc[4]},          # 超出合法域上界
        {"rcomp": (rc[3] + rc[4]) / 2.0, "ccomp": cc[4]},  # 两档之间
        dict(legal[0]),                                    # 第 1 轮已测过
    ]
    # 第 3 轮：越界点（任何 mode 都拒）+ 建议停止。给越界点是为了不再触发仿真——
    # 本轮要验证的是"建议停止能收尾"，多跑三个候选只是拖长测试。
    stopping = [
        {"rcomp": rc[-1] * 3.0, "ccomp": cc[k]} for k in (1, 2, 3)
    ]

    assert len(legal) == MIN_CANDIDATES
    return _ScriptedClient(
        [_reply(legal), _reply(illegal), _reply(stopping, stop=True)]
    ), legal, illegal


@pytest.fixture(scope="module")
def outcome(bundle, repo_root: Path, tmp_path_factory, scripted):
    client, legal, illegal = scripted
    workspace = tmp_path_factory.mktemp("llm_e2e")
    store = Store(workspace / "runs.db")
    _freeze_configs(store, bundle, repo_root)
    artifacts = ArtifactStore(base_dir=workspace / "artifacts")

    propose_fn = make_llm_propose_fn(
        client=client,
        store=store,
        artifacts=artifacts,
        task_id=bundle.task.task_id,
        task_cfg=bundle.task,
        constraints_cfg=bundle.constraints,
        metrics_cfg=bundle.metrics,
    )

    with PythonSession(base_dir=repo_root) as session:
        result = run_task(
            bundle.task,
            bundle.model,
            bundle.metrics,
            bundle.constraints,
            yes=True,
            propose_fn=propose_fn,
            session=session,
            store=store,
            artifacts=artifacts,
            base_dir=repo_root,
        )
    return result, store, client, legal, illegal


# --------------------------------------------------------------------------
# 接线：模型的候选真的进了仿真
# --------------------------------------------------------------------------


def test_accepted_candidates_were_simulated(outcome) -> None:
    """第 1 轮那三个合法档位点全部落进 `candidates` 表并被仿真。

    `origin` 必须是 `'agent'`：候选来源是审计链的一环，模型提的点被记成 `manual`
    或 `dense_grid` 会让"这个设计是谁提出的"这个问题永久失去答案。
    """
    result, store, _client, legal, _illegal = outcome

    rows = store.connection.execute(
        "SELECT parameters_si, origin, origin_llm_call_id FROM candidates "
        "WHERE task_id=? AND origin='agent'",
        (result.task_id,),
    ).fetchall()
    assert len(rows) >= MIN_CANDIDATES, f"agent 来源的候选过少: {len(rows)}"

    persisted = {
        (round(json.loads(r[0])["rcomp"], 6), round(json.loads(r[0])["ccomp"], 22))
        for r in rows
    }
    for params in legal:
        key = (round(params["rcomp"], 6), round(params["ccomp"], 22))
        assert key in persisted, f"合法候选未落库: {params}"

    for _params, origin, llm_call_id in rows:
        assert origin == "agent"
        assert llm_call_id, "agent 候选必须关联到产生它的 llm_call"

    simulated = store.connection.execute(
        "SELECT COUNT(DISTINCT candidate_id) FROM runs WHERE task_id=?",
        (result.task_id,),
    ).fetchone()[0]
    assert simulated >= MIN_CANDIDATES, f"被仿真的候选过少: {simulated}"


def test_llm_calls_are_logged_for_every_round(outcome) -> None:
    """每一轮提案都留下一条 `llm_calls` 行，含 prompt 与上下文哈希。"""
    result, store, client, _legal, _illegal = outcome

    rows = store.connection.execute(
        "SELECT role, model_id, prompt_hash, context_hash, tokens, outcome "
        "FROM llm_calls WHERE task_id=?",
        (result.task_id,),
    ).fetchall()

    assert len(rows) == len(client.prompts), (
        f"llm_calls 行数 {len(rows)} 与实际调用次数 {len(client.prompts)} 不符"
    )
    for role, model_id, prompt_hash, context_hash, tokens, call_outcome in rows:
        assert role == "proposal"
        assert model_id
        assert len(prompt_hash) == 64 and len(context_hash) == 64
        assert tokens > 0
        assert call_outcome


# --------------------------------------------------------------------------
# 拒绝路径：被拒候选带具名原因落库，且不进仿真
# --------------------------------------------------------------------------


def test_rejected_candidates_are_persisted_with_named_reasons(outcome) -> None:
    """第 2、3 轮的候选全部被拒，每条都带原因并关联到 `llm_calls`。

    原因必须具名（`out_of_domain` / `off_tick` / `low_novelty` 之类），不能是
    `invalid` 这种笼统说法：一次通过率低下去之后，要能从原因分布看出模型是"不会
    抄档位"还是"不看已测点"，这两件事的改法完全不同。
    """
    result, store, _client, _legal, illegal = outcome

    rows = store.connection.execute(
        "SELECT reason, raw_parameters, llm_call_id, round_index "
        "FROM rejections WHERE task_id=?",
        (result.task_id,),
    ).fetchall()

    assert len(rows) >= len(illegal), f"被拒候选过少: {len(rows)}"

    logged_calls = {
        row[0]
        for row in store.connection.execute(
            "SELECT llm_call_id FROM llm_calls WHERE task_id=?", (result.task_id,)
        ).fetchall()
    }

    categories: set[str] = set()
    for reason, raw_json, llm_call_id, round_index in rows:
        assert reason, "拒绝原因不得为空"
        categories.add(reason.split(":", 1)[0])
        assert json.loads(raw_json), "原始取值必须原样留存"
        assert llm_call_id in logged_calls, "被拒候选必须能追回产生它的那次调用"
        assert round_index >= 0

    # 三类原因都被触发过，说明校验器的分支不是只有一条路被走到。
    #
    # 这里不含 `low_novelty`：当前配置 `novelty_min_ticks = 1.0` 让它成为不可达分支。
    # 要触发它，候选得落在档位上、又与已测点距离**小于** 1 档——但档位上相邻两点的
    # 距离恰好是 1.0，不小于阈值；不在档位上的点则先被 `off_tick` 拦下。所以这条
    # 分支只在阈值被调高（例如 Dense Grid 之外的探索阶段收紧到 2 档）时才有意义。
    assert {"out_of_domain", "off_tick", "duplicate_of_tested"} <= categories, (
        f"未覆盖全部拒绝类别，实际: {sorted(categories)}"
    )


def test_rejected_candidates_never_reached_the_simulator(outcome) -> None:
    """被拒候选不占预算、不留 run 行。

    这是分层拒绝的意义所在：校验在仿真之前，一个越界的取值不该消耗一次引擎启动。
    """
    result, store, _client, _legal, illegal = outcome

    rejected_params = {
        canonical_json(json.loads(row[0]))
        for row in store.connection.execute(
            "SELECT raw_parameters FROM rejections WHERE task_id=?", (result.task_id,)
        ).fetchall()
    }
    simulated_params = {
        canonical_json(json.loads(row[0]))
        for row in store.connection.execute(
            "SELECT DISTINCT c.parameters_si FROM candidates c "
            "JOIN runs r ON r.candidate_id = c.candidate_id WHERE c.task_id=?",
            (result.task_id,),
        ).fetchall()
    }

    # 第 2 轮的"重复已测点"会同时出现在两边：它作为第 1 轮候选被仿真过，又作为
    # 第 2 轮候选被新颖度拒绝。因此只断言越界与偏离档位的那些从未被仿真。
    never_simulated = {canonical_json(illegal[0]), canonical_json(illegal[1])}
    assert never_simulated <= rejected_params
    assert not (never_simulated & simulated_params), "越界候选不应进入仿真"


# --------------------------------------------------------------------------
# 预算与状态
# --------------------------------------------------------------------------


def test_budget_accounts_only_for_simulated_candidates(outcome) -> None:
    """预算账本与 `runs.budget_units` 之和一致，且远低于上限。"""
    result, store, _client, _legal, _illegal = outcome

    assert result.engine_starts_used == store.sum_budget_units(result.task_id)
    assert 0 < result.engine_starts_used < 200


def test_state_rebuilds_and_search_state_reflects_history(outcome, bundle) -> None:
    """重建的 `SearchState` 含已测点与失败区域。

    这条断言的对象是"下一轮模型能看到什么"。若已测点没进去，模型会反复提同一批点；
    若失败区域没进去，它不知道哪片区域已经被判死。两者都不会报错，只会让搜索退化成
    随机游走——所以要显式验证。
    """
    from poweragent.controller.recovery import rebuild_state
    from poweragent.controller.search_state import build_search_state

    result, store, _client, legal, _illegal = outcome
    rebuilt = rebuild_state(
        store, result.task_id, metrics_cfg=bundle.metrics, budget=bundle.task.budget
    )

    state = build_search_state(
        store,
        result.task_id,
        constraints_cfg=bundle.constraints,
        metrics_cfg=bundle.metrics,
        remaining_budget=rebuilt.remaining_budget,
        current_best=rebuilt.current_best,
    )

    assert len(state.tested_candidates) >= len(legal)
    assert all(p.features is not None for p in state.tested_candidates)
    assert state.failed_regions, "被拒候选应归纳出失败区域"
    assert {r.failure_type for r in state.failed_regions} >= {"out_of_domain"}

    # 合法域与硬约束是配置的投影，不受历史影响。
    assert set(state.legal_domain) == {"rcomp", "ccomp"}
    assert all(
        spec.sense in {"lower", "upper"} for spec in state.hard_constraints.values()
    ), f"方向取值异常: {[s.sense for s in state.hard_constraints.values()]}"
    # 单位来自 metrics.yaml 而不是约束配置，验证那次查找真的查到了东西。
    assert state.hard_constraints["phase_margin_min"].unit == "deg"
    assert state.hard_constraints["peak_current_max"].unit == "A"
