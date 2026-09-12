"""异常恢复三条路径：孤儿 run 回收、`engine_transient` 重试、`resume` 续跑。

这三条此前一行测试都没有。它们的共同特征是**只在出错时才被执行**，正常跑一次
寻优永远碰不到，所以"代码在那里"与"它真的对"之间隔着的距离比别处大。

- `reap_orphan_runs()`：进程被杀掉后重启，上次留下的 `running` 行怎么收。
- `engine_transient` 重试：引擎抽风一次，是否新开 `attempt+1` 行再试，而不是
  改写已有那行。
- `resume=True`：同一个 `task_id` 二次进入，是否复用既有任务行、且不重复烧预算。

第二、三条注入的是**故障**而不是整个后端：`FlakySession` 包住真实的
`PythonSession`，只把指定次数的 `pa.simulate_once` 换成 `engine_transient`，其余
调用（波形归档、裕量提取）全走真实路径。若把整个 session 替换成假的，这两条测试
就只能证明 `run_task()` 的分支写对了，证明不了它与真实产物路径接得上。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from poweragent.config.hashing import canonical_json, constraints_hash, metrics_hash
from poweragent.controller.recovery import reap_orphan_runs, rebuild_state
from poweragent.controller.run_task import ProposeResult, run_task
from poweragent.controller.scenario import compute_scenario_set_hash
from poweragent.sim.backends.session import PythonSession
from poweragent.sim.hashing import model_package_hash, resolve_dependency_closure
from poweragent.store.artifacts import ArtifactStore
from poweragent.store.repo import Candidate, ScenarioSpec, SimulationResult, Store

pytestmark = pytest.mark.integration

GRID_BEST = {"rcomp": 18738.0, "ccomp": 8.111e-10}

# 两个 resume 断言各用一组不同的参数：它们共享 module 作用域的 `first_run` 库，
# 而 `candidate_id` 由参数决定，重复用同一组就会撞 `runs` 的唯一约束。三组都取自
# 参考网格的可行点，因此都能跑通评价层。
RESUME_PARAMS_A = {"rcomp": 28480.3586844, "ccomp": 1e-10}
RESUME_PARAMS_B = {"rcomp": 28480.3586844, "ccomp": 1.51991108295e-10}


@pytest.fixture(scope="module")
def bundle(config_dir: Path):
    """四份配置。本文件自带一份而不是共用 `test_run_task_smoke.py` 的同名
    fixture：那个定义是模块级的、跨文件不可见，而把它提到 `conftest.py` 会与那边
    已有的定义重名并被遮蔽。
    """
    from poweragent.config.loader import load_all

    return load_all(config_dir)


def _freeze_configs(store: Store, bundle, repo_root: Path) -> None:
    """写入 preflight 要求的两个冻结点（与 `test_run_task_smoke.py` 同一做法）。"""
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    store.freeze(
        "model_package",
        hash=model_package_hash(closure, model_dump),
        detail="recovery test freeze",
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
    store.freeze("safety", hash=safety_hash, detail="recovery test freeze")


def _one_candidate_propose_fn(params: dict[str, float]):
    """第一轮给一个候选，之后返回空候选并建议停止。

    `candidate_id` 按 `config.hashing.candidate_id()` 由参数算出，不写死一个字面
    量：`runs` 的唯一约束是 `(task_id, candidate_id, scenario_id, attempt)`，写死
    id 会让"换了参数"在库的眼里仍是同一个候选，于是续跑时撞唯一约束——那是测试
    自己造出来的假象，不是被测代码的问题。真实链路里这个 id 同样由参数决定
    （`agent/validate.py` 正是靠它做去重）。
    """
    from poweragent.config.hashing import candidate_id as compute_candidate_id

    calls: list[int] = []

    def propose(state, *, round_context) -> ProposeResult:
        index = len(calls)
        calls.append(index)
        if index == 0:
            return ProposeResult(
                candidates=(
                    Candidate(
                        candidate_id=compute_candidate_id(params),
                        parameters_si=params,
                    ),
                ),
                stop_recommendation=False,
            )
        return ProposeResult(candidates=(), stop_recommendation=True)

    propose.calls = calls  # type: ignore[attr-defined]
    return propose


class FlakySession:
    """包住真实 `PythonSession`，把前 `fail_times` 次 `pa.simulate_once` 换成
    `engine_transient`。

    `backend_id` 必须透传而不是另取一个值：它进 `execution_env_hash`，取别的值
    会让这条测试与真实 Python 后端的缓存互不命中，那样测的就不是同一条路径了。
    """

    def __init__(self, inner: PythonSession, *, fail_times: int) -> None:
        self._inner = inner
        self._remaining_failures = fail_times
        self.injected: list[str] = []

    backend_id = PythonSession.backend_id

    def __enter__(self) -> "FlakySession":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._inner.__exit__(*exc_info)

    def call(self, fn: str, /, *args: object, nargout: int = 1) -> object:
        if fn == "pa.simulate_once" and self._remaining_failures > 0:
            self._remaining_failures -= 1
            self.injected.append(fn)
            # status != 'ok' 时 simulate() 不读 waveform_path，三个字段就够
            # （见 sim/simulate.py 步骤 3 的条件判断）。
            return {"status": "engine_transient", "elapsed_ms": 7, "engine_starts": 1}
        return self._inner.call(fn, *args, nargout=nargout)


# --------------------------------------------------------------------------
# 1. 孤儿 run 回收
# --------------------------------------------------------------------------


def test_reap_orphan_runs_closes_running_rows_and_keeps_budget(
    bundle, tmp_path: Path
) -> None:
    """`running` 行被收为 `failed/transient_error/process_restart`，`budget_units`
    原样保留。

    保留预算是刻意的（design.md §8.8「budget_units 保留」）：那次仿真可能已经真的
    启动过引擎，只是进程没活到写终态。预算宁可多计不可少计——少计会让下一次运行
    以为还有额度，把同一份额度花第二遍。
    """
    store = Store(tmp_path / "runs.db")
    store.create_task(
        task_id="t_orphan",
        simulation_only=True,
        task_kind="optimize",
        model_package_hash="mph",
        metrics_hash="mh",
        constraints_hash="ch",
        scenario_set_hash="ssh",
        execution_env_hash="eeh",
        calibration_hash="",
        budget_max_starts=100,
    )
    candidate = Candidate(candidate_id="cand_o", parameters_si=GRID_BEST)
    store.persist_candidate(candidate, task_id="t_orphan", origin="agent", round_index=0)

    scenario = ScenarioSpec(
        scenario_id="scr_nom", tier="screening", model_variant="averaged",
        require_margin=False, vin_v=12.0, temp_c=25.0,
        load_start_a=100.0, load_end_a=150.0, slew_a_per_us=100.0, spec_version="1",
    )

    # 一行留在 running（模拟进程被杀），一行正常收尾。
    orphan_id = store.open_run(
        task_id="t_orphan", candidate=candidate, scenario=scenario, attempt=1,
        simulation_key="simkey_orphan", budget_units=3, cache_hit=False,
    )
    done_id = store.open_run(
        task_id="t_orphan", candidate=candidate, scenario=scenario, attempt=2,
        simulation_key="simkey_done", budget_units=1, cache_hit=False,
    )
    store.close_run_ok(
        done_id,
        SimulationResult(
            run_id=done_id, status="ok", waveform_ref=None, observable_ref=None,
            elapsed_ms=11, engine_starts=1,
        ),
    )

    assert reap_orphan_runs(store) == 1, "只应收走那一行 running"

    reaped = store.connection.execute(
        "SELECT status, failure_class, cause, ended_at, budget_units "
        "FROM runs WHERE run_id=?", (orphan_id,),
    ).fetchone()
    assert reaped[0] == "failed"
    assert reaped[1] == "transient_error"
    assert reaped[2] == "process_restart"
    assert reaped[3], "ended_at 必须非空"
    assert reaped[4] == 3, "budget_units 必须原样保留"

    untouched = store.connection.execute(
        "SELECT status, failure_class, cause FROM runs WHERE run_id=?", (done_id,),
    ).fetchone()
    assert untouched == ("done", None, None), "已终态的行不得被触及"

    assert reap_orphan_runs(store) == 0, "再次调用应无行可收（幂等）"


def test_reap_orphan_runs_is_unconditional_on_an_empty_database(tmp_path: Path) -> None:
    """空库上调用返回 0 而不是报错。

    `reap_orphan_runs()` 在 `run_task()` 里是无条件执行的第一行（design.md L16-4
    移除了原先的 `resume` 门控），因此首次运行、库里一行都没有时它也会被调用。
    """
    store = Store(tmp_path / "empty.db")
    assert reap_orphan_runs(store) == 0


# --------------------------------------------------------------------------
# 2. engine_transient 重试
# --------------------------------------------------------------------------


def test_engine_transient_opens_a_new_attempt_row(
    bundle, repo_root: Path, tmp_path: Path
) -> None:
    """一次 `engine_transient` 之后新开 `attempt=2` 行重试，不改写 `attempt=1` 行。

    两件事必须同时成立（R16.4）：
      失败那行留在库里，`status='failed'`、`cause='engine_transient'`；
      重试是**另一行**，`attempt=2`。

    若实现改成"原地重置那行再跑一次"，第一条断言会失败——而那种实现会让"这次仿真
    重试过几次"在库里查不到，预算与耗时也会被覆盖成最后一次的值。
    """
    store = Store(tmp_path / "runs.db")
    _freeze_configs(store, bundle, repo_root)
    propose = _one_candidate_propose_fn(GRID_BEST)

    with PythonSession(base_dir=repo_root) as inner:
        session = FlakySession(inner, fail_times=1)
        result = run_task(
            bundle.task, bundle.model, bundle.metrics, bundle.constraints,
            yes=True, propose_fn=propose, session=session, store=store,
            artifacts=ArtifactStore(base_dir=tmp_path / "artifacts"),
            base_dir=repo_root,
        )
        assert len(session.injected) == 1, "应恰好注入过一次 engine_transient"

    # 不限定 candidate_id：这条测试用的是自己的库，里面只有 propose_fn 提的那一个
    # 候选。写死 id 反而脆弱——它由参数哈希决定，改参数就得同步改这里。
    rows = store.connection.execute(
        "SELECT scenario_id, attempt, status, failure_class, cause, budget_units "
        "FROM runs WHERE task_id=? ORDER BY scenario_id, attempt",
        (result.task_id,),
    ).fetchall()

    failed = [r for r in rows if r[2] == "failed"]
    assert len(failed) == 1, f"应恰有一行失败，实际 {rows}"
    assert failed[0][1] == 1, "失败的是第一次尝试"
    assert failed[0][3] == "transient_error"
    assert failed[0][4] == "engine_transient"

    # 同一场景上必须存在 attempt=2 的重试行，且它是新行（attempt=1 那行还在）。
    failed_scenario = failed[0][0]
    same_scenario = [r for r in rows if r[0] == failed_scenario]
    attempts = sorted(r[1] for r in same_scenario)
    assert attempts == [1, 2], (
        f"场景 {failed_scenario} 应有 attempt 1 与 2 两行，实际 {attempts}"
    )
    retry = next(r for r in same_scenario if r[1] == 2)
    assert retry[2] == "done", "重试那次应成功收尾"

    # 失败那次也要计预算：引擎确实被启动过。
    assert failed[0][5] >= 1, "失败的尝试同样占用预算，不能记 0"


def test_retry_exhaustion_does_not_write_a_stop_reason(
    bundle, repo_root: Path, tmp_path: Path
) -> None:
    """重试耗尽只让该候选被跳过，不把整个任务停在 `transient_error` 上。

    `max_attempts_per_scenario=2` 表示最多重试 1 次，所以连续注入 2 次故障就会耗尽。
    按 R16.5，此时的正确行为是关闭该行、继续下一个候选，**不**写
    `tasks.stop_reason`——引擎抽风是环境问题，不是"这次寻优该结束了"的理由。
    """
    store = Store(tmp_path / "runs.db")
    _freeze_configs(store, bundle, repo_root)
    propose = _one_candidate_propose_fn(GRID_BEST)

    assert bundle.task.budget.max_attempts_per_scenario == 2

    with PythonSession(base_dir=repo_root) as inner:
        session = FlakySession(inner, fail_times=2)
        result = run_task(
            bundle.task, bundle.model, bundle.metrics, bundle.constraints,
            yes=True, propose_fn=propose, session=session, store=store,
            artifacts=ArtifactStore(base_dir=tmp_path / "artifacts"),
            base_dir=repo_root,
        )

    # 关键是不能升级成需要人介入的终止：引擎抽风由重试机制吸收，吸收不了就跳过
    # 这个候选，任务本身仍按正常判据收尾。
    assert result.stop_reason != "stop_and_ask_human", (
        f"engine_transient 耗尽不应触发人工介入，实际 stop_reason="
        f"{result.stop_reason!r} cause={result.cause!r}"
    )
    assert result.stop_reason in {
        "no_improvement", "target_reached", "budget_exhausted",
    }, f"未预期的停止原因: {result.stop_reason!r}"

    failed = store.connection.execute(
        "SELECT attempt FROM runs WHERE task_id=? AND cause='engine_transient' "
        "ORDER BY attempt", (result.task_id,),
    ).fetchall()
    assert [r[0] for r in failed] == [1, 2], (
        f"两次尝试都应留下失败行，实际 {failed}"
    )


# --------------------------------------------------------------------------
# 3. resume 续跑
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def first_run(bundle, repo_root: Path, tmp_path_factory):
    """跑一次完整任务，把库留给 resume 断言复用。"""
    workspace = tmp_path_factory.mktemp("resume")
    store = Store(workspace / "runs.db")
    _freeze_configs(store, bundle, repo_root)

    with PythonSession(base_dir=repo_root) as session:
        result = run_task(
            bundle.task, bundle.model, bundle.metrics, bundle.constraints,
            yes=True, propose_fn=_one_candidate_propose_fn(GRID_BEST),
            session=session, store=store,
            artifacts=ArtifactStore(base_dir=workspace / "artifacts"),
            base_dir=repo_root,
        )
    return result, store, workspace


def test_resume_reuses_the_existing_task_row(
    first_run, bundle, repo_root: Path
) -> None:
    """`resume=True` 二次进入同一 `task_id`：复用既有任务行，不重复冻结场景集。

    `resume` 当前只影响这一件事（见 `run_task()` docstring）：是否跳过
    `create_task()` 与 `freeze_scenario_set()`。`tasks.task_id` 是主键、
    `scenario_set` 的主键是 `(task_id, scenario_id)`，两者重复写入都会抛
    `IntegrityError`——所以这条测试若失败，失败方式是异常而不是断言不成立。
    """
    result, store, workspace = first_run

    before = store.connection.execute(
        "SELECT COUNT(*) FROM scenario_set WHERE task_id=?", (result.task_id,)
    ).fetchone()[0]

    with PythonSession(base_dir=repo_root) as session:
        second = run_task(
            bundle.task, bundle.model, bundle.metrics, bundle.constraints,
            resume=True, yes=True,
            propose_fn=_one_candidate_propose_fn(RESUME_PARAMS_A),
            session=session, store=store,
            artifacts=ArtifactStore(base_dir=workspace / "artifacts"),
            base_dir=repo_root,
        )

    assert second.task_id == result.task_id

    task_rows = store.connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_id=?", (result.task_id,)
    ).fetchone()[0]
    assert task_rows == 1, "不得新增第二条 tasks 行"

    after = store.connection.execute(
        "SELECT COUNT(*) FROM scenario_set WHERE task_id=?", (result.task_id,)
    ).fetchone()[0]
    assert after == before, "场景集不得被重复冻结"


def test_resume_does_not_disturb_the_completed_runs(
    first_run, bundle, repo_root: Path
) -> None:
    """续跑不改动首次跑完的 `runs` 行。

    这是"可恢复"的实际含义：已经完成的证据留在原处，续跑只往后追加。若某处把
    已终态的行重新打开或覆盖，那次仿真的耗时、预算与产物引用就被改写成第二次的
    值，而库是唯一事实源——改写之后就再也查不出第一次到底发生了什么。

    第二次刻意提一个**不同**的候选。真实流程里 `agent/validate.py` 的
    `duplicate_of_tested` 会拦掉已测候选，所以"续跑时重复提同一个候选"这条路径
    不会发生；注入式的 `propose_fn` 绕过了那层校验，若在这里重复提同一候选，
    `run_tier()` 的缓存命中路径会以 `attempt=1` 再开一行，撞上 `runs` 的
    `(task_id, candidate_id, scenario_id, attempt)` 唯一约束。两级缓存本身另有
    专门的断言，见本文件末尾一节。
    """
    result, store, workspace = first_run

    before = {
        row[0]: row[1:]
        for row in store.connection.execute(
            "SELECT run_id, status, budget_units, elapsed_ms, waveform_ref "
            "FROM runs WHERE task_id=?",
            (result.task_id,),
        ).fetchall()
    }
    assert before, "首次运行应留下 runs 行"

    with PythonSession(base_dir=repo_root) as session:
        run_task(
            bundle.task, bundle.model, bundle.metrics, bundle.constraints,
            resume=True, yes=True,
            propose_fn=_one_candidate_propose_fn(RESUME_PARAMS_B),
            session=session, store=store,
            artifacts=ArtifactStore(base_dir=workspace / "artifacts"),
            base_dir=repo_root,
        )

    after = {
        row[0]: row[1:]
        for row in store.connection.execute(
            "SELECT run_id, status, budget_units, elapsed_ms, waveform_ref "
            "FROM runs WHERE task_id=?",
            (result.task_id,),
        ).fetchall()
    }

    for run_id, values in before.items():
        assert run_id in after, f"首次运行的 run_id={run_id} 在续跑后消失"
        assert after[run_id] == values, (
            f"run_id={run_id} 被续跑改动：{values} -> {after[run_id]}"
        )
    assert len(after) > len(before), "续跑提出了新候选，应追加新的 runs 行"


def test_rebuilt_state_reflects_the_database_not_a_snapshot(
    first_run, bundle, repo_root: Path
) -> None:
    """重建出的状态与**当前库内事实**一致，而不是某次运行结束时的内存快照。

    刻意不拿 `first_run` 的 `TaskOutcome.best_candidate_id` 做基准：本文件上面两条
    resume 断言各往同一个库里追加了一个候选，其中一个（`RESUME_PARAMS_A`，即参考
    网格的最优点）比首次那个更好。于是"首次跑完时的最佳"已经过期——若拿它做基准，
    这条测试断言的就变成"库不许被后续运行改变"，那与 `rebuild_state()` 要验证的
    性质正好相反。

    正确的基准是同一时刻用 `rank()` 从库里算出来的第一名：两条独立路径
    （`rebuild_state()` 与 `rank()`）读同一个库应得同一答案。
    """
    from poweragent.eval.aggregate import rank

    result, store, _workspace = first_run

    rebuilt = rebuild_state(
        store, result.task_id,
        metrics_cfg=bundle.metrics, budget=bundle.task.budget,
    )
    ranked = rank(store, result.task_id, bundle.metrics, top_n=1)

    assert ranked, "库里应有可行候选"
    assert rebuilt.current_best is not None
    assert rebuilt.current_best.candidate_id == ranked[0].candidate_id
    assert rebuilt.current_best.value == pytest.approx(ranked[0].worst_case_value)

    # 剩余预算同样由 runs 求和推出，不从上次进程继承。
    assert rebuilt.remaining_budget == (
        bundle.task.budget.max_engine_starts - store.sum_budget_units(result.task_id)
    )


# --------------------------------------------------------------------------
# 4. 两级缓存命中
# --------------------------------------------------------------------------
#
# 缓存与恢复是同一件事的两面：都是"这份结果已经算过了，别再算一遍"。放在同一个
# 文件里，因为它们共用同一个前提——事实全在库里，不在内存里。
#
# 这一层此前没有任何测试。走 `run_tier()` 而不是 `run_task()`，原因是缓存的真实
# 命中场景在 `run_task()` 之上被 preflight 挡住了：
#
#   `simulation_key` 不含 `task_id`，所以跨任务本该能命中；但
#   `scenario_set_hash` 含 `task_id`（`controller/scenario.py:scenario_set_rows`），
#   而 `freezes(kind='safety')` 绑定它、`check_freeze_consistency()` 每次进入都要
#   比对，于是同一个库里放不下两个 `task_id`。
#
# 结果是单次正常寻优里缓存几乎不会命中（用户一次真实运行的 24 行 runs 全部
# `cache_hit=0`）。命中真正会发生在同一候选同一场景被再次评价时，`attempt` 不同
# 而 `simulation_key` 相同——这正是下面构造的情形，也是重试与续跑路径上会走到的
# 那条。


def _ledger_for(store: Store, task_id: str, bundle) -> object:
    from poweragent.controller.budget import BudgetLedger

    return BudgetLedger(
        store, task_id,
        bundle.task.budget.max_engine_starts,
        bundle.task.budget.max_wallclock_hours * 3600.0,
    )


def test_second_evaluation_of_the_same_candidate_hits_the_cache(
    bundle, repo_root: Path, tmp_path: Path
) -> None:
    """同一候选同一场景第二次评价时命中缓存：不烧预算，但照样写一行 `runs`。

    两个断言方向都必要：

    `budget_units=0` / `cache_hit=1` —— 缓存起作用了；
    **仍然写了一行 `runs`** —— CP-6 要求的完备性判定依赖 `runs` 行的存在，命中
    时若跳过落库，worst-case 聚合的 `HAVING COUNT(DISTINCT scenario_id)` 就会把
    这个候选当成"场景没跑全"而排除掉。省一行的代价是整个候选从排名里消失。

    `run_tier()` 而非 `run_task()`：理由见上方小节说明。
    """
    from poweragent.controller.run_task import run_tier
    from poweragent.controller.scenario import evaluation_rows
    from poweragent.sim.hashing import fast_fingerprint

    store = Store(tmp_path / "runs.db")
    _freeze_configs(store, bundle, repo_root)

    task_id = bundle.task.task_id
    store.create_task(
        task_id=task_id, simulation_only=True, task_kind="optimize",
        model_package_hash="mph", metrics_hash="mh", constraints_hash="ch",
        scenario_set_hash="ssh", execution_env_hash="eeh", calibration_hash="",
        budget_max_starts=bundle.task.budget.max_engine_starts,
    )
    candidate = Candidate(candidate_id="cand_cache", parameters_si=GRID_BEST)
    store.persist_candidate(candidate, task_id=task_id, origin="agent", round_index=0)

    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=repo_root)
    frozen_fingerprint = fast_fingerprint(closure)
    frozen_package_hash = model_package_hash(closure, model_dump)

    # 只取第一个评价场景：这条断言与场景数量无关，跑一个就够，多跑只是更慢。
    scenario = tuple(
        ScenarioSpec(
            scenario_id=s.scenario_id, tier=s.tier, model_variant=s.model_variant,
            require_margin=s.require_margin, vin_v=s.vin_v, temp_c=s.temp_c,
            load_start_a=s.load_start_a, load_end_a=s.load_end_a,
            slew_a_per_us=s.slew_a_per_us, spec_version=s.spec_version,
        )
        for s in evaluation_rows(bundle.task)
    )[:1]

    common = {
        "model_cfg": bundle.model,
        "metrics_cfg": bundle.metrics,
        "constraints_cfg": bundle.constraints,
        "store": store,
        "artifacts": ArtifactStore(base_dir=tmp_path / "artifacts"),
        "budget_ledger": _ledger_for(store, task_id, bundle),
        "task_id": task_id,
        "execution_env_hash": "eeh",
        "frozen_fingerprint": frozen_fingerprint,
        "frozen_model_package_hash": frozen_package_hash,
        "base_dir": repo_root,
    }

    with PythonSession(base_dir=repo_root) as session:
        first = run_tier(candidate, "evaluation", scenario, session=session,
                         attempt=1, **common)
        assert first.passed, f"首次评价应通过：{first}"

        spent_after_first = store.sum_budget_units(task_id)
        assert spent_after_first > 0, "首次评价必须真的烧掉预算"

        second = run_tier(candidate, "evaluation", scenario, session=session,
                          attempt=2, **common)
        assert second.passed, f"第二次评价应通过：{second}"

    assert store.sum_budget_units(task_id) == spent_after_first, (
        "第二次评价命中缓存，不应新增预算消耗"
    )

    rows = store.connection.execute(
        "SELECT attempt, cache_hit, budget_units, status, waveform_ref "
        "FROM runs WHERE task_id=? AND candidate_id=? ORDER BY attempt",
        (task_id, candidate.candidate_id),
    ).fetchall()
    assert len(rows) == 2, f"两次评价应各写一行 runs，实际 {rows}"

    fresh, cached = rows
    assert (fresh[1], fresh[3]) == (0, "done"), f"首次不应是缓存命中：{fresh}"
    assert fresh[2] > 0, "首次必须计预算"

    assert cached[1] == 1, f"第二次应标记 cache_hit=1：{cached}"
    assert cached[2] == 0, f"第二次不应计预算：{cached}"
    assert cached[3] == "done"
    assert cached[4] == fresh[4], "复用的应是同一份波形产物引用"

    # 指标与约束判定同样要落库，且与首次一致——命中缓存复用的是结果，不是"跳过评价"。
    metric_counts = store.connection.execute(
        "SELECT r.attempt, COUNT(*) FROM runs r "
        "JOIN metric_results m ON m.run_id = r.run_id "
        "WHERE r.task_id=? AND r.candidate_id=? GROUP BY r.attempt ORDER BY r.attempt",
        (task_id, candidate.candidate_id),
    ).fetchall()
    assert len(metric_counts) == 2, f"两行都应有指标：{metric_counts}"
    assert metric_counts[0][1] == metric_counts[1][1], (
        f"缓存命中那行的指标条数应与首次相同：{metric_counts}"
    )
