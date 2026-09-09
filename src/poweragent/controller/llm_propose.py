"""把 LLM 提案器接成主循环认识的 `propose_fn`。

`run_task` 只知道一件事：给它一个函数，交进去本轮状态，拿回本轮候选。它不知道候选
是模型提的、网格枚举的、还是人手填的。这一层就是那个转接头——它把"提案"这件事内部
的三个步骤（重建上下文 → 问模型 → 校验）包成主循环的单一调用。

为什么校验在这一层、而不在 `agent.propose` 里
---------------------------------------------
`propose()` 负责把模型的话变成结构化候选，它对"这些取值是否合法"没有话语权：合法域
与档位来自配置，新颖度要查已测点，两者都在确定性域。若把校验塞进 `propose()`，被
校验的一方就同时握有校验规则，"模型不能自己放宽约束"这条性质便只剩口头保证。

因此顺序是固定的：先拿模型的原始输出，再由这里用配置里的规则筛。模型答什么都不会
改变筛法。

被拒候选一律落库
----------------
`validate()` 拒掉的候选带着具名原因写进 `rejections`，并关联到产生它的 `llm_call_id`。
只留下被接受的候选会让"一次通过率"这个指标无从计算，而那是评价提案质量最直接的数字。
拒绝也是数据。
"""

from __future__ import annotations

from poweragent.agent.propose import propose
from poweragent.agent.validate import decide_mode, validate
from poweragent.config.schema import ConstraintsConfig, MetricsConfig, TaskConfig
from poweragent.controller.run_task import ProposeFn, ProposeResult, RoundContext
from poweragent.controller.search_state import (
    build_search_state,
    legal_domain_from_config,
)
from poweragent.controller.stop import SearchState as ControllerSearchState
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Store

__all__ = ["make_llm_propose_fn"]


def make_llm_propose_fn(
    *,
    client,
    store: Store,
    artifacts: ArtifactStore,
    task_id: str,
    task_cfg: TaskConfig,
    constraints_cfg: ConstraintsConfig,
    metrics_cfg: MetricsConfig,
) -> ProposeFn:
    """组装一个 LLM 驱动的 `propose_fn`。

    返回闭包而不是类：捕获的全是配置与服务对象（不可变的配置、数据库与产物句柄），
    没有一项是搜索状态。搜索状态每轮从 SQLite 重建，因此这个闭包被调用两次、中间
    进程重启过，行为也一致。若闭包里攒了轮次计数或历史最佳，断点续跑就会与不中断
    的跑法产生分歧，而那种分歧不报错、只是让某一轮的提案基于错的信息。
    """
    # 档位展开只依赖配置，在这里算一次而不是每轮算：配置在一次任务内不变。
    domain = legal_domain_from_config(constraints_cfg)
    design_space = constraints_cfg.design_space

    def _propose(
        state: ControllerSearchState, *, round_context: RoundContext
    ) -> ProposeResult:
        full_state = build_search_state(
            store,
            task_id,
            constraints_cfg=constraints_cfg,
            metrics_cfg=metrics_cfg,
            remaining_budget=round_context.remaining_budget,
            current_best=state.current_best,
        )

        output = propose(
            full_state,
            client=client,
            store=store,
            artifacts=artifacts,
            task_id=task_id,
            max_repair_rounds=task_cfg.budget.max_llm_repair_rounds,
            max_network_retries=task_cfg.llm.max_network_retries,
        )

        # 模型答不出合格格式、或网络重试耗尽时 `output.candidates` 为空。这里不分支：
        # 空序列进 `validate()` 自然得到空的接受集，与"提了但全被拒"走同一条路径。
        # `output.outcome` 已经把两种成因的区别记在 `llm_calls` 里，不必在控制流里再分一次。
        verdict = validate(
            output.candidates,
            domain=domain,
            tick_match_rel_tol=design_space.tick_match_rel_tol,
            novelty_min_ticks=design_space.novelty_min_ticks,
            tested=full_state.tested_candidates,
            mode=decide_mode(
                no_improvement_rounds=round_context.no_improvement_rounds,
                has_current_best=state.current_best is not None,
            ),
        )

        for raw_parameters, reason in verdict.rejected:
            store.record_rejection(
                task_id=task_id,
                round_index=round_context.round_index,
                llm_call_id=output.llm_call_id,
                raw_parameters=raw_parameters,
                reason=reason,
            )

        return ProposeResult(
            candidates=verdict.accepted,
            stop_recommendation=output.stop_recommendation,
            llm_call_id=output.llm_call_id,
        )

    return _propose
