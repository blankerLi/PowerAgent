"""解质量对照：只换提案来源，其余评价路径完全相同。

问题是"模型提的候选，好在哪里"。要回答它，必须把除提案之外的一切都固定住：同一份
配置、同一套档位、同一个确定性校验器、同一条双层仿真路径、同一组硬约束、同一个停止
判定。本脚本因此复用 `run_task()` 本身，只替换 `propose_fn`——被对照的变量恰好只有
一个。

参照系是参考扫描：144 个批准档位组合里有 36 个可行点，把它们按主目标排序，就得到
一把尺子。"找到了第 1 名"与"找到了第 15 名"是关于解质量的确定事实，换个随机种子也
不会变。

不做效率对照
------------
按 R23.5，本记录不比较"用了多少次引擎启动"，不计算 regret，不使用效率类措辞。理由
不是形式上的合规：单次运行的效率数字没有统计意义——同一个模型跑两遍就能给出不同的
答案，把它当结论等于用噪声支撑结论。解的排名不同，它是确定的。

多次运行的意义也在这里：一次找到最优解可能是碰巧，所以随机侧跑多个固定种子、LLM 侧
跑多次，看的是排名的分布而不是某一次的数字。

用法::

    python scripts/solution_quality_compare.py --random 5
    python scripts/solution_quality_compare.py --llm 2

结果累加进 `artifacts/solution_quality_report.json`，可以分批跑。LLM 侧需要
`POWERAGENT_LLM_API_KEY`；随机侧不需要，纯本地仿真。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
# 同目录脚本作为模块复用。`_freeze_configs()` 是 M0 出口人工动作的脚本代劳，三个
# 脚本需要完全一致的冻结行为——各写一份拷贝的话，某一份改了而另两份没改时，
# 三个脚本的 preflight 前提就悄悄分了岔。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_search_run import _collect_stats, _freeze_configs  # noqa: E402

from poweragent.agent.client import API_KEY_ENV, DeepSeekClient  # noqa: E402
from poweragent.agent.validate import decide_mode, validate  # noqa: E402
from poweragent.config.env import load_local_env  # noqa: E402
from poweragent.config.loader import load_all  # noqa: E402
from poweragent.config.ticks import expand_all_ticks  # noqa: E402
from poweragent.controller.llm_propose import make_llm_propose_fn  # noqa: E402
from poweragent.controller.run_task import ProposeResult, run_task  # noqa: E402
from poweragent.controller.search_state import (  # noqa: E402
    build_search_state,
    legal_domain_from_config,
)
from poweragent.sim.backends.session import PythonSession  # noqa: E402
from poweragent.store.artifacts import ArtifactStore  # noqa: E402
from poweragent.store.repo import Store  # noqa: E402

GRID_REPORT = REPO_ROOT / "artifacts" / "dense_grid_report.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "solution_quality_report.json"

# 随机侧每轮提出的候选数。取 5 是为了落在 LLM 侧的 [3, 6] 区间中段——"每轮提几个"
# 若两侧不同，被对照的就不止提案来源一个变量了。
RANDOM_CANDIDATES_PER_ROUND = 5


# ==========================================================================
# 排名参系
# ==========================================================================


def load_grid_ranking() -> tuple[list[dict], dict]:
    """把参考扫描的可行点按主目标排序，作为解质量的尺子。

    返回 `(ranking, meta)`。`ranking` 按名次升序，每项含参数、目标值与名次。
    """
    if not GRID_REPORT.exists():
        raise SystemExit(
            f"缺少参考扫描记录 {GRID_REPORT.relative_to(REPO_ROOT)}；"
            f"先跑 python scripts/dense_grid_run.py"
        )
    grid = json.loads(GRID_REPORT.read_text(encoding="utf-8"))

    feasible = [
        point
        for point in grid["all_points"]
        if point["feasible"] and point["objective"] is not None
    ]
    reverse = grid["objective_direction"] != "minimize"
    feasible.sort(key=lambda p: p["objective"], reverse=reverse)

    ranking = [
        {
            "rank": index,
            "rcomp": point["rcomp"],
            "ccomp": point["ccomp"],
            "objective": point["objective"],
            "phase_margin": point["phase_margin"],
        }
        for index, point in enumerate(feasible, start=1)
    ]
    meta = {
        "grid_task_id": grid["task_id"],
        "scan_function": grid["scan_function"],
        "scanned_points": grid["scanned_points"],
        "feasible_points": len(ranking),
        "objective_metric_id": grid["objective_metric_id"],
        "objective_direction": grid["objective_direction"],
        "best_objective": ranking[0]["objective"] if ranking else None,
        "median_objective": (
            ranking[len(ranking) // 2]["objective"] if ranking else None
        ),
        "worst_objective": ranking[-1]["objective"] if ranking else None,
    }
    return ranking, meta


def lookup_rank(
    ranking: list[dict], parameters: dict | None
) -> tuple[int | None, str]:
    """查一个解在可行点排名里的位置。

    档位取值是从同一份配置展开的同一组浮点数，因此用相对容差比较而不是精确相等：
    两侧都经过 JSON 往返，末位可能差一个 ulp。容差取 1e-9，远小于相邻档位比
    （约 1.52），不可能把两个不同档位认成同一个。
    """
    if not parameters:
        return None, "no_feasible_candidate"

    for entry in ranking:
        same_rcomp = abs(entry["rcomp"] - parameters["rcomp"]) <= 1e-9 * abs(
            entry["rcomp"]
        )
        same_ccomp = abs(entry["ccomp"] - parameters["ccomp"]) <= 1e-9 * abs(
            entry["ccomp"]
        )
        if same_rcomp and same_ccomp:
            return entry["rank"], "matched"

    # 落到这里说明找到的解不在参考扫描的可行点集里。两侧的可行性判据完全相同，
    # 所以这是一个需要人看的异常，不是可以四舍五入过去的小事——如实标注。
    return None, "not_in_reference_feasible_set"


# ==========================================================================
# 随机提案：与 LLM 侧共用校验与落库路径
# ==========================================================================


def make_random_propose_fn(
    *,
    seed: int,
    store: Store,
    task_id: str,
    constraints_cfg,
    metrics_cfg,
):
    """从批准档位里均匀随机取点，走与 LLM 侧完全相同的后续路径。

    刻意**不**去重：重复提同一个点会被 `duplicate_of_tested` 拒掉，而那正是均匀随机
    采样的固有代价。在采样器里加一层去重会让对照偏向随机侧——被对照的是"提案来源"，
    不是"提案来源加上一个我们额外送给它的记忆"。

    校验、被拒落库、`RoundContext` 的用法都与 `controller/llm_propose.py` 一致。
    这段逻辑在这里重写而不是复用那个工厂函数：那个函数的入参是一个 LLM client，
    把它改造成"接受任意提案源"需要动已落地且被测试覆盖的代码，而本脚本是一次性
    实验装置。两处的一致性由它们都调用同一个 `validate()` 与 `decide_mode()` 保证。
    """
    rng = random.Random(seed)
    ticks = expand_all_ticks(constraints_cfg)
    rcomp_ticks = [float(t) for t in ticks["rcomp"]]
    ccomp_ticks = [float(t) for t in ticks["ccomp"]]
    domain = legal_domain_from_config(constraints_cfg)
    design_space = constraints_cfg.design_space

    def _propose(state, *, round_context) -> ProposeResult:
        full_state = build_search_state(
            store,
            task_id,
            constraints_cfg=constraints_cfg,
            metrics_cfg=metrics_cfg,
            remaining_budget=round_context.remaining_budget,
            current_best=state.current_best,
        )
        raw = [
            {"rcomp": rng.choice(rcomp_ticks), "ccomp": rng.choice(ccomp_ticks)}
            for _ in range(RANDOM_CANDIDATES_PER_ROUND)
        ]
        verdict = validate(
            raw,
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
                # 随机提案没有 LLM 调用可关联。留 None 而不是编一个 id：
                # `rejections.llm_call_id` 是可空外键，正是为这种来源留的。
                llm_call_id=None,
                raw_parameters=raw_parameters,
                reason=reason,
            )
        return ProposeResult(
            candidates=verdict.accepted, stop_recommendation=False, llm_call_id=None
        )

    return _propose


# ==========================================================================
# 单次运行
# ==========================================================================


def run_once(bundle, *, source: str, seed: int | None, label: str) -> dict:
    """跑一次完整寻优，返回该次的统计与解质量排名。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workspace = REPO_ROOT / "artifacts" / "quality_compare" / f"{label}_{stamp}"
    workspace.mkdir(parents=True, exist_ok=True)

    store = Store(workspace / "runs.db")
    _freeze_configs(store, bundle)
    artifacts = ArtifactStore(base_dir=workspace / "store")
    task_id = bundle.task.task_id

    if source == "llm":
        client = DeepSeekClient(timeout_s=bundle.task.llm.timeout_s)
        propose_fn = make_llm_propose_fn(
            client=client,
            store=store,
            artifacts=artifacts,
            task_id=task_id,
            task_cfg=bundle.task,
            constraints_cfg=bundle.constraints,
            metrics_cfg=bundle.metrics,
        )
        model_id = client.model_id
    else:
        assert seed is not None
        propose_fn = make_random_propose_fn(
            seed=seed,
            store=store,
            task_id=task_id,
            constraints_cfg=bundle.constraints,
            metrics_cfg=bundle.metrics,
        )
        model_id = None

    with PythonSession(base_dir=REPO_ROOT) as session:
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
            base_dir=REPO_ROOT,
        )

    stats = _collect_stats(store, result.task_id, bundle, result)

    # `stats["rounds"]` 数的是 `llm_calls` 行，随机侧不打那个埋点，恒为 0。提案轮数
    # 要从两侧都会写的地方取：候选与被拒记录都带 `round_index`。
    round_row = store.connection.execute(
        "SELECT MAX(round_index) FROM ("
        "  SELECT round_index FROM candidates WHERE task_id=? AND round_index IS NOT NULL"
        "  UNION ALL"
        "  SELECT round_index FROM rejections WHERE task_id=?"
        ")",
        (task_id, task_id),
    ).fetchone()
    proposal_rounds = (round_row[0] + 1) if round_row and round_row[0] is not None else 0

    return {
        "source": source,
        "seed": seed,
        "model_id": model_id,
        "recorded_at_utc": stats["recorded_at_utc"],
        "stop_reason": stats["stop_reason"],
        "proposal_rounds": proposal_rounds,
        "llm_calls": stats["rounds"],
        "candidates": stats["candidates"],
        "rejection_reasons": stats["rejection_reasons"],
        "best": stats["best"],
        "workspace": str(workspace.relative_to(REPO_ROOT)).replace("\\", "/"),
    }


# ==========================================================================
# 汇总
# ==========================================================================


def summarize(runs: list[dict], ranking: list[dict]) -> dict:
    """按提案来源汇总排名分布。

    列的是排名的最好值、中位数与最差值，以及"找到第 1 名"的次数。不算平均值：
    排名是序数，平均排名没有对应的实际含义（第 1 名与第 5 名的差距不等于第 30 名与
    第 34 名的差距，因为目标值的间隔不均匀）。目标值另列，那个可以谈差距。
    """
    by_source: dict[str, dict] = {}
    for source in sorted({run["source"] for run in runs}):
        subset = [run for run in runs if run["source"] == source]
        ranks = [run["rank"] for run in subset if run["rank"] is not None]
        objectives = [
            run["best"]["worst_case_objective_us"]
            for run in subset
            if run["best"] is not None
        ]
        ranks_sorted = sorted(ranks)
        by_source[source] = {
            "runs": len(subset),
            "runs_with_feasible_solution": len(ranks),
            "rank_best": ranks_sorted[0] if ranks_sorted else None,
            "rank_median": (
                ranks_sorted[len(ranks_sorted) // 2] if ranks_sorted else None
            ),
            "rank_worst": ranks_sorted[-1] if ranks_sorted else None,
            "times_found_rank_1": sum(1 for r in ranks if r == 1),
            "objective_best": min(objectives) if objectives else None,
            "objective_worst": max(objectives) if objectives else None,
            "all_ranks": ranks_sorted,
        }
    return by_source


def load_existing() -> dict:
    if REPORT_PATH.exists():
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    return {"runs": []}


def print_summary(report: dict) -> None:
    """ASCII only：Windows 控制台是 GBK，'µ' 之类会抛编码错误。"""
    ref = report["reference"]
    print("=" * 74)
    print(f"reference    : {ref['feasible_points']} feasible of "
          f"{ref['scanned_points']} scanned  ({ref['scan_function']})")
    print(f"  objective  : best {ref['best_objective']:.4g} / "
          f"median {ref['median_objective']:.4g} / worst {ref['worst_objective']:.4g} us")
    print("-" * 74)
    header = "%-8s %5s %6s %8s %8s %8s %8s" % (
        "source", "runs", "rank#1", "rank_best", "rank_med", "rank_wst", "obj_best"
    )
    print(header)
    for source, agg in report["summary"].items():
        print("%-8s %5d %6d %8s %8s %8s %8s" % (
            source,
            agg["runs"],
            agg["times_found_rank_1"],
            agg["rank_best"],
            agg["rank_median"],
            agg["rank_worst"],
            "None" if agg["objective_best"] is None else f"{agg['objective_best']:.4g}",
        ))
    print("-" * 74)
    for run in report["runs"]:
        best = run["best"]
        params = (
            f"rcomp={best['parameters_si']['rcomp']:.6g} "
            f"ccomp={best['parameters_si']['ccomp']:.6g}"
            if best
            else "no feasible solution"
        )
        print("%-8s seed=%-9s rank=%-5s rounds=%-3s %-22s %s" % (
            run["source"],
            run["seed"],
            run["rank"],
            run.get("proposal_rounds", "?"),
            run["stop_reason"],
            params,
        ))
    print("=" * 74)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--random", type=int, default=0, help="随机对照的运行次数")
    parser.add_argument("--llm", type=int, default=0, help="LLM 提案的运行次数")
    parser.add_argument(
        "--seed-base", type=int, default=20260909, help="随机种子基数，用于可复现"
    )
    args = parser.parse_args()

    if args.random <= 0 and args.llm <= 0:
        parser.error("至少给出 --random 或 --llm 其一")

    load_local_env(REPO_ROOT / ".env")
    if args.llm > 0 and not os.environ.get(API_KEY_ENV):
        print(f"缺少 {API_KEY_ENV}，无法跑 LLM 侧。", file=sys.stderr)
        return 2

    bundle = load_all(REPO_ROOT / "configs")
    ranking, meta = load_grid_ranking()
    report = load_existing()
    existing_random = sum(1 for r in report["runs"] if r["source"] == "random")

    for index in range(args.random):
        # 种子从既有随机运行数往后接续，重复调用脚本不会跑出同一批种子——那样
        # 追加的运行只是把同一个结果记了两遍，看起来样本变多而其实没有。
        seed = args.seed_base + existing_random + index
        print(f"[random {index + 1}/{args.random}] seed={seed} ...")
        run = run_once(bundle, source="random", seed=seed, label=f"random_seed{seed}")
        run["rank"], run["rank_status"] = lookup_rank(
            ranking, run["best"]["parameters_si"] if run["best"] else None
        )
        report["runs"].append(run)
        print(f"    rank={run['rank']} ({run['rank_status']})")

    for index in range(args.llm):
        print(f"[llm {index + 1}/{args.llm}] ...")
        run = run_once(bundle, source="llm", seed=None, label=f"llm_{index}")
        run["rank"], run["rank_status"] = lookup_rank(
            ranking, run["best"]["parameters_si"] if run["best"] else None
        )
        report["runs"].append(run)
        print(f"    rank={run['rank']} ({run['rank_status']})")

    report["reference"] = meta
    report["summary"] = summarize(report["runs"], ranking)
    report["note"] = (
        "解质量对照：只更换提案来源，配置、档位、校验器、双层仿真、硬约束与停止判定"
        "全部相同。排名相对参考扫描的可行点集。按 R23.5 不比较引擎启动次数、"
        "不建立 Regret 体系。"
    )
    report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print_summary(report)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"report -> {REPORT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
