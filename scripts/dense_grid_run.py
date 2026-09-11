"""跑一次 12x12 参考扫描，画响应面，并把网格上的最优点记下来。

这是设计空间的"底牌"：144 个批准档位组合逐点跑完评价层，得到目标值与相位裕量在
整个二维域上的分布。有了它，"模型找到的那个点好不好"就有了确定的答案——不是靠
相信模型，而是靠把所有可能的点都算一遍。

这份数据能用来做什么、不能用来做什么
------------------------------------
R23.5 明确禁止基于 Dense Grid 建立 Regret 体系或搜索效率对照，术语表则把它的用途
限定为"响应面可视化与「Agent 是否具备搜索价值」的直接答案"。两者划出的界线是：

- **可以**比较**解的质量**：模型找到的点是不是档位上的最优点、差几档、目标值差多少。
  这是一个确定的事实判断，换个随机种子也不会变。
- **不可以**比较**搜索的效率**：诸如"用多少次仿真达到目标"、regret 曲线、收敛速度。
  单次跑的这类数字没有统计意义——同一个模型跑两遍就能给出不同的答案，把它当结论
  等于用噪声给设计背书。项目在 `test_report_wording.py` 里把这条禁令做成了断言。

这个界线不是形式主义。承认"我这次只跑了一遍，所以效率数字不可用"，比拿一个好看的
数字去支撑一个撑不住的结论要诚实。

预算是单独审批的
----------------
144 点 x 2（每点一次开关模型仿真 + 一次裕量分析）= 288 次引擎启动，超过 `task.yaml`
里寻优任务的 200。这不是冲突：`gate_budget_max_engine_starts` 是参数而不是从
`task_cfg` 读的值，正因为一次性参考扫描与寻优任务是两笔账。本脚本按 R23.4 的判据
先估算、再在完整扫描与 36 点分层回退之间选择，不硬编码跑哪一个。

用法::

    python scripts/dense_grid_run.py

不需要 API Key：参考扫描全程不调用 LLM，候选来自档位的笛卡尔积。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from poweragent.agent.validate import validate  # noqa: E402
from poweragent.config.hashing import (  # noqa: E402
    canonical_json,
    constraints_hash,
    metrics_hash,
)
from poweragent.config.loader import load_all  # noqa: E402
from poweragent.controller.run_task import (  # noqa: E402
    _compute_execution_env_hash,
)
from poweragent.controller.scenario import compute_scenario_set_hash  # noqa: E402
from poweragent.controller.search_state import legal_domain_from_config  # noqa: E402
from poweragent.reference.dense_grid import (  # noqa: E402
    dense_grid_scan,
    estimate_dense_grid_engine_starts,
    response_surface,
    tiered_grid_scan,
)
from poweragent.sim.backends.session import PythonSession  # noqa: E402
from poweragent.sim.engine import MatlabSession  # noqa: E402
from poweragent.sim.hashing import (  # noqa: E402
    fast_fingerprint,
    model_package_hash,
    resolve_dependency_closure,
)
from poweragent.store.artifacts import ArtifactStore  # noqa: E402
from poweragent.store.repo import Store  # noqa: E402

REPORT_PATH = REPO_ROOT / "artifacts" / "dense_grid_report.json"

# 为这次一次性参考扫描单独审批的预算。288 < 320，因此完整 144 点扫描可以跑；
# 若把它压到 288 以下，下面的 R23.4 判据会自动改跑 36 点分层回退。
GATE_MAX_ENGINE_STARTS = 320
GATE_MAX_WALLCLOCK_HOURS = 2.0

# 单次评价层运行的墙钟估算，用于 R23.4 的第二个判据。
#
# P-1 探针数据不存在（Simulink 许可证不可用，见 model.yaml 的注释），因此这里用
# D2-3 的实测值：开关模型 400 us 仿真约 360 ms，裕量分析约 100 ms，取 0.5 s 作为
# 保守上界。用实测值而不是留空，是因为判据要求一个具体数字才能比较；标注它的来源
# 与探针不同，是为了不把实测值伪装成探针值。
SINGLE_RUN_S = 0.5

# 后端选择：默认 python。
#
# 默认值不是"哪个更好"的判断，而是"不改变既有结论"：artifacts/ 下已入库的参考扫描
# 记录是 Python 后端产出的，默认切到 MATLAB 会让重跑本脚本得到一份不可与之比较的
# 记录（`execution_env_hash` 含 `sim_backend`，两个后端的结果按设计不互相命中缓存）。
#
# 不引入工厂函数或注册表：两个会话类的构造签名一致（都只接 `base_dir`），一个字典
# 查表就够。`sim/backends/__init__.py` 已明确"两个扩展点是函数边界而非抽象层"，
# 在 sim 包里加一个 open_session() 还会让 `import poweragent.sim` 连带 import
# PythonSession（依赖 scipy），破坏 engine.py 特意保持的"无 MATLAB/无 scipy 也能
# import"性质。
SESSION_CLASSES = {"python": PythonSession, "matlab": MatlabSession}


def _freeze_configs(store: Store, bundle) -> None:
    """写入 preflight 要求的两个冻结点（人工审批动作的脚本代劳，见 llm_search_run.py）。"""
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=REPO_ROOT)
    store.freeze(
        "model_package",
        hash=model_package_hash(closure, model_dump),
        detail="dense_grid_run.py",
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
    store.freeze("safety", hash=safety_hash, detail="dense_grid_run.py")


def _make_validate_fn(bundle):
    """把 `agent.validate.validate()` 适配成 `dense_grid` 期望的调用形状。

    `dense_grid.py` 按 design.md §6.7 的字面签名写了预期
    `validate_fn(raw, *, design_space, tested, mode)`，而实际落地的 `validate()`
    把 `design_space` 拆成了三个更精确的参数（`domain` / `tick_match_rel_tol` /
    `novelty_min_ticks`）——只给一个 `DesignSpace` 对象，校验器就得自己去里面找
    档位与容差，那等于把"配置长什么样"的知识散进校验逻辑里。

    差异用一个转接函数吸收，而不是改动任何一方的签名：`dense_grid` 已落地并被测试
    覆盖，`validate()` 的拆分是有意的。这个函数是两个都不该动的接口之间的胶水。
    """
    domain = legal_domain_from_config(bundle.constraints)

    def _validate_fn(raw, *, design_space, tested, mode):
        return validate(
            raw,
            domain=domain,
            tick_match_rel_tol=design_space.tick_match_rel_tol,
            novelty_min_ticks=design_space.novelty_min_ticks,
            tested=tested,
            mode=mode,
        )

    return _validate_fn


def _summarize(store: Store, task_id: str, bundle) -> dict:
    """从 SQLite 读出网格上的目标值分布与最优点。

    只统计"点位与取值"，不产出任何与搜索过程有关的量（R23.5）。
    """
    conn = store.connection
    objective_id = bundle.metrics.objective.primary.metric_id

    rows = conn.execute(
        """
        SELECT c.parameters_si,
               MAX(CASE WHEN m.metric_id=? THEN m.value END)  AS objective,
               MAX(CASE WHEN m.metric_id='phase_margin' THEN m.value END) AS pm,
               MIN(cr.feasible)                               AS feasible
        FROM candidates c
        JOIN runs r  ON r.candidate_id = c.candidate_id
        LEFT JOIN metric_results m     ON m.run_id = r.run_id AND m.valid = 1
        LEFT JOIN constraint_results cr ON cr.run_id = r.run_id
        WHERE c.task_id = ?
        GROUP BY c.candidate_id
        """,
        (objective_id, task_id),
    ).fetchall()

    points = []
    for params_json, objective, pm, feasible in rows:
        params = json.loads(params_json)
        points.append(
            {
                "rcomp": params["rcomp"],
                "ccomp": params["ccomp"],
                "objective": objective,
                "phase_margin": pm,
                "feasible": bool(feasible) if feasible is not None else False,
            }
        )

    feasible_points = [p for p in points if p["feasible"] and p["objective"] is not None]
    # 目标方向来自配置而不是写死 min：把"越小越好"硬编码在这里，配置改成
    # maximize 之后这段会安静地给出错的最优点。
    minimize = bundle.metrics.objective.primary.direction == "minimize"
    best = (
        min(feasible_points, key=lambda p: p["objective"])
        if minimize
        else max(feasible_points, key=lambda p: p["objective"])
    ) if feasible_points else None

    return {
        "scanned_points": len(points),
        "feasible_points": len(feasible_points),
        "infeasible_or_invalid_points": len(points) - len(feasible_points),
        "objective_metric_id": objective_id,
        "objective_direction": bundle.metrics.objective.primary.direction,
        "grid_best": best,
        "all_points": sorted(points, key=lambda p: (p["rcomp"], p["ccomp"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=tuple(SESSION_CLASSES),
        default="python",
        help="仿真后端（默认 python；matlab 需要 Simulink 许可证与 models/*.slx）",
    )
    args = parser.parse_args()

    bundle = load_all(REPO_ROOT / "configs")

    # ------------------------------------------------------------------
    # R23.4 判据：先估算，再在完整扫描与分层回退之间选择。
    # 判断留在调用方，`dense_grid_scan()` / `tiered_grid_scan()` 都只管
    # "被调用就把自己那个点数跑一遍"。
    # ------------------------------------------------------------------
    full_starts = estimate_dense_grid_engine_starts(144, bundle.task, bundle.metrics)
    full_wallclock_s = SINGLE_RUN_S * full_starts
    over_budget = (
        full_starts > GATE_MAX_ENGINE_STARTS
        or full_wallclock_s > GATE_MAX_WALLCLOCK_HOURS * 3600
    )

    scan = tiered_grid_scan if over_budget else dense_grid_scan
    point_count = 36 if over_budget else 144

    print(f"144 点估算: {full_starts} starts, {full_wallclock_s:.0f} s")
    print(f"审批上限  : {GATE_MAX_ENGINE_STARTS} starts,"
          f" {GATE_MAX_WALLCLOCK_HOURS * 3600:.0f} s")
    print(f"选择      : {scan.__name__}（{point_count} 点）")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workspace = REPO_ROOT / "artifacts" / "dense_grid" / stamp
    workspace.mkdir(parents=True, exist_ok=True)

    store = Store(workspace / "runs.db")
    _freeze_configs(store, bundle)
    artifacts = ArtifactStore(base_dir=workspace / "store")

    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=REPO_ROOT)

    print(f"workspace : {workspace}")
    print(f"backend   : {args.backend}")
    print("running...")

    with SESSION_CLASSES[args.backend](base_dir=REPO_ROOT) as session:
        result = scan(
            bundle.model,
            bundle.metrics,
            bundle.constraints,
            bundle.task,
            store,
            session=session,
            artifacts=artifacts,
            execution_env_hash=_compute_execution_env_hash(
                bundle.model, sim_backend=session.backend_id
            ),
            frozen_fingerprint=fast_fingerprint(closure),
            frozen_model_package_hash=model_package_hash(closure, model_dump),
            validate_fn=_make_validate_fn(bundle),
            gate_budget_max_engine_starts=GATE_MAX_ENGINE_STARTS,
            gate_budget_max_wallclock_hours=GATE_MAX_WALLCLOCK_HOURS,
            base_dir=REPO_ROOT,
        )

    figures = response_surface(
        result.task_id,
        store,
        workspace / "figures",
        metrics_cfg=bundle.metrics,
        constraints_cfg=bundle.constraints,
    )

    # 把当前有效的一份图同步到 docs/figures/ 并入库。工作区那份在带时间戳的目录里、
    # 属于过程产物；文档引用需要一个稳定路径，否则每跑一次都要改引用。
    docs_figures = REPO_ROOT / "docs" / "figures"
    docs_figures.mkdir(parents=True, exist_ok=True)
    for figure in figures:
        shutil.copy2(figure, docs_figures / figure.name)

    summary = _summarize(store, result.task_id, bundle)
    summary.update(
        {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": (
                "参考扫描的点位与取值记录。用途限于响应面可视化与解质量对照；"
                "按 R23.5 不用于 Regret 体系或搜索效率对照。"
            ),
            "task_id": result.task_id,
            "scan_function": scan.__name__,
            "requested_point_count": point_count,
            "figures": [str(p.relative_to(REPO_ROOT)).replace("\\", "/") for p in figures],
            "workspace": str(workspace.relative_to(REPO_ROOT)).replace("\\", "/"),
        }
    )

    print("=" * 68)
    print(f"scanned          : {summary['scanned_points']} points")
    print(f"feasible         : {summary['feasible_points']}")
    print(f"infeasible/invalid: {summary['infeasible_or_invalid_points']}")
    best = summary["grid_best"]
    if best:
        print(f"grid best        : rcomp={best['rcomp']:.6g} ccomp={best['ccomp']:.6g}")
        print(f"  {summary['objective_metric_id']:15s}: {best['objective']:.4g}")
        print(f"  phase_margin     : {best['phase_margin']:.4g} deg")
    else:
        print("grid best        : None (no feasible point)")
    print(f"figures          : {len(figures)}")
    print("=" * 68)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"report -> {REPORT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
