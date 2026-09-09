"""用真实 DeepSeek 跑一次完整寻优，并把这一次的表现统计成一份记录。

这是第一次让整条链路在真实模型下运转：状态从 SQLite 重建 → prompt 确定性生成 →
模型提候选 → 确定性校验器筛 → 仿真评价 → 落库 → 下一轮。假 client 的集成测试证明
接线正确，这个脚本回答的是另一个问题：**模型在这个任务上表现如何**。

产出的是测量记录，不是门禁证据
------------------------------
`artifacts/margin_cross_check.json` 那类记录是确定性的——重跑得到同一个哈希，因此
可以被 preflight 当作断言依据。这份报告不是：模型每次答的不一样，报告里的数字会变。
它带 UTC 时间戳与 `model_id`，说明"某个模型在某个时刻的一次表现"，用途是写进文档与
面试时拿得出手，而不是给程序做判断。把两类记录混为一谈会让门禁失去意义。

关注的数字
----------
- **一次通过率** `accepted / (accepted + rejected)`：模型有多大比例的输出是合法的。
  这是提示词工程质量最直接的指标。
- **拒绝原因分布**：低通过率的成因决定改法。`off_tick` 占比高说明模型抄不准档位
  （档位要给到 7 位有效数字），要改的是 prompt 里档位表的呈现方式；
  `duplicate_of_tested` 占比高说明它没在看已测点，要改的是历史信息的组织方式。
  两者的改法完全不同，合并成一个"失败率"就无从下手。
- **token 用量**：波形压缩的收益在这里兑现。

用法::

    python scripts/llm_search_run.py

需要 `POWERAGENT_LLM_API_KEY`（可放在仓库根的 `.env`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from poweragent.agent.client import API_KEY_ENV, DeepSeekClient  # noqa: E402
from poweragent.config.env import load_local_env  # noqa: E402
from poweragent.config.hashing import (  # noqa: E402
    canonical_json,
    constraints_hash,
    metrics_hash,
)
from poweragent.config.loader import load_all  # noqa: E402
from poweragent.controller.llm_propose import make_llm_propose_fn  # noqa: E402
from poweragent.controller.run_task import run_task  # noqa: E402
from poweragent.controller.scenario import compute_scenario_set_hash  # noqa: E402
from poweragent.eval.aggregate import rank  # noqa: E402
from poweragent.sim.backends.session import PythonSession  # noqa: E402
from poweragent.sim.hashing import (  # noqa: E402
    model_package_hash,
    resolve_dependency_closure,
)
from poweragent.store.artifacts import ArtifactStore  # noqa: E402
from poweragent.store.repo import Store  # noqa: E402

REPORT_PATH = REPO_ROOT / "artifacts" / "llm_search_report.json"


def _freeze_configs(store: Store, bundle) -> None:
    """写入 preflight 要求的两个冻结点。

    这一步在真实流程里是 M0 出口的人工审批动作，脚本代劳只是为了能独立跑起来。
    `run_task()` 自己从不创建冻结记录——让被校验方补签通行证就等于没有校验。
    """
    model_dump = bundle.model.model_dump(mode="json")
    closure = resolve_dependency_closure(model_dump, base_dir=REPO_ROOT)
    store.freeze(
        "model_package",
        hash=model_package_hash(closure, model_dump),
        detail="llm_search_run.py",
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
    store.freeze("safety", hash=safety_hash, detail="llm_search_run.py")


def _collect_stats(store: Store, task_id: str, bundle, result) -> dict:
    """从 SQLite 统计这一次跑的表现。

    全部数字都来自数据库，而不是在主循环里顺手累加的计数器：那样统计口径会与"任务
    结束后回看数据库能得出什么"产生分歧，而报告要能被别人用同一个库复算出来。
    """
    conn = store.connection

    llm_rows = conn.execute(
        "SELECT outcome, tokens FROM llm_calls WHERE task_id=?", (task_id,)
    ).fetchall()
    accepted = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE task_id=? AND origin='agent'", (task_id,)
    ).fetchone()[0]
    rejection_rows = conn.execute(
        "SELECT reason FROM rejections WHERE task_id=?", (task_id,)
    ).fetchall()
    rejected = len(rejection_rows)

    proposed = accepted + rejected
    reasons = Counter(row[0].split(":", 1)[0] for row in rejection_rows)

    ranked = rank(store, task_id, bundle.metrics, top_n=1)
    best: dict | None = None
    if ranked:
        params = conn.execute(
            "SELECT parameters_si FROM candidates WHERE candidate_id=?",
            (ranked[0].candidate_id,),
        ).fetchone()
        target = bundle.task.objective_target.target_value
        best = {
            "candidate_id": ranked[0].candidate_id,
            "parameters_si": json.loads(params[0]) if params else None,
            "worst_case_objective_us": round(ranked[0].worst_case_value, 4),
            "objective_target_us": target,
            "target_reached": ranked[0].worst_case_value <= target,
        }

    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "一次真实 LLM 寻优的测量记录，非确定性证据；重跑数字会变。"
            "确定性门禁证据见 margin_cross_check.json 与 dual_model_consistency.json。"
        ),
        "task_id": task_id,
        "stop_reason": result.stop_reason,
        "cause": result.cause,
        "wallclock_s": round(result.wallclock_s, 2),
        "rounds": len(llm_rows),
        "llm": {
            "model_id": os.environ.get("POWERAGENT_LLM_MODEL", "deepseek-chat"),
            "total_tokens": sum(row[1] or 0 for row in llm_rows),
            "outcomes": dict(Counter(row[0] for row in llm_rows)),
        },
        "candidates": {
            "proposed": proposed,
            "accepted": accepted,
            "rejected": rejected,
            # 一次通过率：模型输出中有多大比例通过了确定性校验。分母为 0 时留 None
            # 而不是 0.0——"没提过候选"与"提的全被拒"是两件事。
            "first_pass_rate": round(accepted / proposed, 4) if proposed else None,
        },
        "rejection_reasons": dict(sorted(reasons.items())),
        "budget": {
            "engine_starts_used": result.engine_starts_used,
            "max_engine_starts": bundle.task.budget.max_engine_starts,
        },
        "best": best,
    }


def _print_summary(stats: dict) -> None:
    """只用 ASCII 符号打印：Windows 控制台是 GBK，'µ' 之类会直接抛编码错误。"""
    c = stats["candidates"]
    print("=" * 68)
    print(f"stop_reason      : {stats['stop_reason']} ({stats['cause']})")
    print(f"rounds           : {stats['rounds']}")
    print(f"wallclock        : {stats['wallclock_s']} s")
    print(f"engine starts    : {stats['budget']['engine_starts_used']}"
          f" / {stats['budget']['max_engine_starts']}")
    print(f"tokens           : {stats['llm']['total_tokens']}")
    print(f"llm outcomes     : {stats['llm']['outcomes']}")
    print(f"candidates       : proposed={c['proposed']} accepted={c['accepted']}"
          f" rejected={c['rejected']}")
    print(f"first pass rate  : {c['first_pass_rate']}")
    print(f"rejection reasons: {stats['rejection_reasons']}")
    best = stats["best"]
    if best:
        print(f"best candidate   : {best['parameters_si']}")
        print(f"  objective      : {best['worst_case_objective_us']} us"
              f" (target {best['objective_target_us']} us,"
              f" reached={best['target_reached']})")
    else:
        print("best candidate   : None (no candidate passed the evaluation tier)")
    print("=" * 68)


def main() -> int:
    load_local_env(REPO_ROOT / ".env")
    if not os.environ.get(API_KEY_ENV):
        print(f"缺少 {API_KEY_ENV}，无法调用真实模型。", file=sys.stderr)
        return 2

    bundle = load_all(REPO_ROOT / "configs")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workspace = REPO_ROOT / "artifacts" / "llm_search" / stamp
    workspace.mkdir(parents=True, exist_ok=True)

    store = Store(workspace / "runs.db")
    _freeze_configs(store, bundle)
    artifacts = ArtifactStore(base_dir=workspace / "store")

    client = DeepSeekClient(timeout_s=bundle.task.llm.timeout_s)
    propose_fn = make_llm_propose_fn(
        client=client,
        store=store,
        artifacts=artifacts,
        task_id=bundle.task.task_id,
        task_cfg=bundle.task,
        constraints_cfg=bundle.constraints,
        metrics_cfg=bundle.metrics,
    )

    print(f"workspace: {workspace}")
    print(f"model    : {client.model_id}")
    print("running...")

    with PythonSession(base_dir=REPO_ROOT) as session:
        result = run_task(
            bundle.task,
            bundle.model,
            bundle.metrics,
            bundle.constraints,
            yes=True,  # 非交互：Checkpoint 1 的人工确认在脚本里无从进行
            propose_fn=propose_fn,
            session=session,
            store=store,
            artifacts=artifacts,
            base_dir=REPO_ROOT,
        )

    stats = _collect_stats(store, result.task_id, bundle, result)
    stats["workspace"] = str(workspace.relative_to(REPO_ROOT)).replace("\\", "/")
    _print_summary(stats)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"report -> {REPORT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
