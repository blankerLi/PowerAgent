"""poweragent/cli.py

`poweragent` 命令行入口：恰好四个子命令 `run` / `report` / `apply` / `log`
（`design.md` §6.1；`tasks.md` T21 / 任务 15.1；需求追溯 R19.4, R20.1, R20.7, R20.9）。

## 本文件的分波排期

`tasks.md` 的波次编排注明 `cli.py` 由 15.1 / 15.2 / 15.5 分三波：

    15.1 -> 四命令骨架：参数解析、结构性校验、分发到下游函数
    15.2 -> cli.format_status() / RunSnapshot / build_run_snapshot()：单行状态
            输出（本任务已实现，为独立、零 I/O 的纯格式化函数 + 一个尚未被任何
            真实调用点引用的查询组装函数，详见下方专属小节；未接入 `cmd_run`
            的实际输出循环——`run_task()` 尚未落地，接入点是该函数落地后的
            14.4/14.5，或 15.5 的收尾范围）
    15.5 -> 退出码映射（run 的 stop_reason → exit code）与 API Key 环境变量口径的
            收尾。`_exit_code_for_stop_reason()` 在 15.2 撰写时已提前实现（它是
            一个简单、独立、可单独测试的纯函数），15.5 在此基础上补齐三项：
            (a) 把「参数或配置错误（含 `PreflightError`）⟹ 1」这一半的映射接入
            `cmd_run()`——`_exit_code_for_stop_reason()` 本身的定义域只覆盖四个
            `stop_reason` 取值，`PreflightError` 从不产出 `stop_reason`（断言
            失败时 `tasks` 行从未创建），必须在 `cmd_run()` 里单独 `except`；
            (b) 新增 `get_llm_api_key()`，`POWERAGENT_LLM_API_KEY` 在本代码库
            中唯一的读取入口（`agent/propose.py`，任务 23.x，尚未落地，届时
            改为调用本函数，不重复读取该环境变量）；
            (c) 确认 `--approver` 的取值只有命令行这一条来源——`report` 命令的
            `--approver`/`--second-approver` 两个 `click.Option` 均未设置
            `envvar=`，`cmd_report()` 的结构性校验只读取解析后的形参，不查
            `os.environ`、不 shell 出去读 `git config`。

本任务（15.1）只搭建四命令骨架：参数解析、结构性校验（缺参数 / `--checkpoint`
越域 / `--task` 指向不存在的 `task_id` / `--approve`&`--reject` 互斥）与向下游
函数的分发，不实现 `run` 的寻优主循环、`report` 的报告渲染、`apply` 的模型落盘。

## 软依赖（下游函数尚未落地时的过渡处理）

以下下游函数在本任务撰写时尚未落地，属于与本任务并行或晚于本任务派发的其他
任务的职责范围（`sim/hashing.py` 顶部对 `config/hashing.py` 的软依赖说明是同一
模式，此处沿用）：

- `sim.apply_model.apply_candidate()`（design.md §6.3.3，未落地）——`cmd_apply`
  按签名 `apply_candidate(candidate_id, *, approval_id, model_cfg, store,
  session) -> AppliedModel` 编写调用点；`sim/apply_model.py` 模块目前不存在。

一旦落地，`cli.py` 侧的调用点不需要改动（签名已按其设计文档的约定编写），只需
删除上面对应的软依赖说明段落。

已经解除的软依赖（保留记录，因为它们各自的接线方式是本文件的既定结构）：

- `controller.run_task.run_task()`（任务 14.4/14.5）已落地，`cmd_run` 直接调用。
- `report.render.render_report()` 已落地；`cmd_report` 的纯渲染分支仍保留
  `try: from ... import render_report except ImportError` 这一层，因为该模块
  import jinja2——渲染依赖缺失时应当给出一句提示并返回退出码 1，而不是让整个
  `poweragent` 命令组在 import 期崩掉（`log` 等与渲染无关的子命令不该被连带
  拖死）。
- `_compute_current_result_hash()` 此前是抛 `NotImplementedError` 的占位函数，
  其阻塞理由是"从数据库聚合出那 9 个字段是报告聚合层的职责，而该层未落地"。
  `report/context.py` 与 `eval/aggregate.py` 均已落地，桩已按当初写下的分工
  填实：聚合本身在 `report.context.compute_result_hash()`（9 键里 7 个的数据源
  它已为报告渲染各查过一遍），本文件只保留 CLI 边界该管的那一件事——配置目录
  默认取 `configs/`。

## `Store` 的构造与 `--task` 存在性校验

`Store(db_path)` 在 `db_path` 指向的文件不存在时会调用 `db.init_db()` **创建**
一个全新的空数据库（`store/repo.py` `__init__` 的既有行为）。这与本任务「未通过
校验时不写任何数据库行」的要求不冲突——创建一个空数据库文件本身不写入任何表行,
`ddl.sql` 的 `CREATE TABLE` 语句不算「数据行」。但为了不在校验失败路径上意外地
在磁盘上生成一个全新的空 `.db` 文件（这本身也是一种可观测的副作用，容易让人
以为发生了写入），`_open_store()` 在打开前先检查 `db_path` 是否已存在；不存在时
直接判定为「`task_id` 不存在」（一个从未 run 过的数据库里自然不含任何
`task_id`），不调用 `Store()`。

`db_path` 的取值来源：design.md §9.2 与全篇示例固定使用相对路径 `runs.db`
（未见任何配置项或 CLI 选项承载可配置的数据库路径），因此本文件使用同名的
模块级常量 `DEFAULT_DB_PATH = Path("runs.db")`，不新增 `--db` 一类的 CLI 选项
——design.md 没有给出这样一个选项，凭空新增会让「恰好四个命令」之外又多出一个
未登记的接口面。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

import click

from poweragent.config.loader import ConfigLoadError, load_all
from poweragent.config.schema import MetricsConfig
from poweragent.controller.budget import BudgetLedger
from poweragent.eval.aggregate import rank
from poweragent.store.repo import (
    ApprovalRecord,
    ApprovalValidationError,
    Store,
)

__all__ = [
    "cli",
    "cmd_run",
    "cmd_report",
    "cmd_apply",
    "cmd_log",
    "_exit_code_for_stop_reason",
    "LLM_API_KEY_ENV_VAR",
    "get_llm_api_key",
    "RunSnapshot",
    "format_status",
    "build_run_snapshot",
]


DEFAULT_DB_PATH = Path("runs.db")

# 配置目录的默认取值，与 `report.render.DEFAULT_CONFIG_DIR` 及 `cmd_run`
# （`config_dir = task_yaml.resolve().parent`）是同一约定：配置在仓库根的
# `configs/` 下，design.md 全篇示例均用相对路径，没有任何 CLI 选项承载可配置的
# 配置目录。不从 `report.render` 导入那个同名常量：该模块 import jinja2，而本
# 文件对报告渲染是软依赖（渲染不可用时要给出提示而不是崩在 import 上），为一个
# 路径字面量把整条软依赖链拉到模块级不划算。
_DEFAULT_CONFIG_DIR = Path("configs")

# design.md §6.1 / requirements.md Requirement 16 AC8, Requirement 20 AC8：
# LLM API Key 只从该环境变量读取，不写入四个 YAML、不入库（`llm_calls` 只存
# `prompt_hash`/`context_hash`/`tokens` 三项，见 `store/repo.py` `LlmCallRecord`
# 与 `ddl.sql` 的 `llm_calls` 表定义，均不含任何凭据字段）、不落 `artifacts/`。
LLM_API_KEY_ENV_VAR = "POWERAGENT_LLM_API_KEY"


def get_llm_api_key() -> str:
    """读取 `POWERAGENT_LLM_API_KEY` 环境变量，未设置或为空串时抛
    `click.UsageError`（退出码 1）。

    这是本代码库中读取该凭据的**唯一**入口——`agent/propose.py`（任务 23.x，
    尚未落地）需要调用 LLM API 时应改为调用本函数，而不是自行
    `os.environ[...]`，避免这个环境变量名字符串在多处重复、也避免出现第二个
    绕开本函数的读取路径。本函数只读取、只返回该字符串本身：不写入
    `configs/*.yaml` 四个文件、不通过 `Store` 写入任何表（`llm_calls` 表的六
    字段——`role`/`model_id`/`prompt_hash`/`context_hash`/`tokens`/`outcome`
    ——里没有一列可以承载凭据原文，`log_llm_call()` 的调用方也不应该把它塞进
    `evidence_ids`）、不写入 `artifacts/` 下的任何 prompt/输出落盘文件。

    未设置时的报错方式与其余「参数或配置错误」路径一致（`click.UsageError`，
    退出码 1），不单独发明第二种「凭据缺失」的退出码——设计文档对退出码的
    三分类映射（`no_improvement`/`target_reached` ⟹ 0；`budget_exhausted`/
    `stop_and_ask_human` ⟹ 2；其余参数或配置错误 ⟹ 1）已经完整覆盖这一情形。
    """
    import os

    value = os.environ.get(LLM_API_KEY_ENV_VAR)
    if not value:
        raise click.UsageError(
            f"environment variable {LLM_API_KEY_ENV_VAR} is not set (or empty); "
            "the LLM API key must be provided this way and never via a config "
            "file, the database, or a CLI argument"
        )
    return value

# design.md §6.1 / §11.2、requirements.md R19.14/R20 的固定退出码映射。
_EXIT_CODE_BY_STOP_REASON: dict[str, int] = {
    "no_improvement": 0,
    "target_reached": 0,
    "budget_exhausted": 2,
    "stop_and_ask_human": 2,
    # `checkpoint1_rejected`（任务 14.7 新增，见 `controller/run_task.py`
    # 模块 docstring "## 12.6" 一节）不是 `should_stop()` 的四值之一——它发生
    # 在 `WHILE` 主循环之前，从未经过 `should_stop()`。这不是一个随意补上的
    # 第五格：`run_task.py` §12.6 已经明确论证过它的语义归类——它既不是
    # "需要人介入的 Checkpoint 2"（`budget_exhausted`/`stop_and_ask_human`
    # ⟹ 2 的理由），也不是"正常收敛"（`no_improvement`/`target_reached`
    # ⟹ 0 的理由），而是"根本没有开始寻优"，与"参数或配置错误（含
    # `PreflightError`）⟹ 1"同一类（requirements.md R16 AC16）。因此这里
    # 取 1，不是 2：人在 Checkpoint 1 拒绝/未确认，其效果就是寻优主循环从未
    # 被进入，与配置错误未通过校验时"不进入寻优"是同一种"未进入"的结果，
    # 不是budget_exhausted/stop_and_ask_human 所代表的"已经在寻优过程中、
    # 需要人来决定下一步"的情形。
    "checkpoint1_rejected": 1,
}


def _exit_code_for_stop_reason(stop_reason: str) -> int:
    """`stop_reason` → 进程退出码的固定映射（design.md §6.1/§11.2）。

    `no_improvement` / `target_reached` ⟹ 0；`budget_exhausted` /
    `stop_and_ask_human` ⟹ 2；`checkpoint1_rejected` ⟹ 1（见
    `_EXIT_CODE_BY_STOP_REASON` 字典上方的注释，任务 14.7 新增）。

    本函数的定义域目前共五个取值，分属两个不同的来源：前四个是
    `should_stop()`（design.md §8.7 伪代码）的完整取值域，第五个
    `checkpoint1_rejected` 不经过 `should_stop()`（发生在主循环之前），是
    `run_task()` 自行产出的一个并列取值（`controller/run_task.py` 模块
    docstring "## 12.6" 一节已论证这一区分，与本函数的区分一致）。未登记的
    `stop_reason`（不应发生）视为异常情形，抛 `ValueError`——静默返回一个
    猜测的退出码会把「`run_task()` 产出了未登记的 `stop_reason`」这个更严重
    的错误藏起来。

    与「参数或配置错误（含 `PreflightError`）⟹ 1」的映射不在本函数内：那一类
    错误从不产出 `stop_reason`（在 `preflight()` 失败时 `run_task()` 直接抛出
    异常，任务的 `tasks` 行根本不创建），因此不属于本函数的定义域——尽管
    `checkpoint1_rejected` 恰好映射到同一个退出码 1，两者仍是不同的路径
    （一个是本函数的字典查找，一个是 `cmd_run()` 里单独的 `except
    PreflightError`）。
    """
    try:
        return _EXIT_CODE_BY_STOP_REASON[stop_reason]
    except KeyError:
        raise ValueError(
            f"_exit_code_for_stop_reason: unregistered stop_reason={stop_reason!r}; "
            f"expected one of {sorted(_EXIT_CODE_BY_STOP_REASON)}"
        ) from None


# ===========================================================================
# 任务 15.2：`RunSnapshot` / `format_status()` / `build_run_snapshot()`
#
# `format_status()` 是一个纯格式化函数（无 I/O），签名与字段来源均见
# `design.md` §6.1（"实现为单函数 `format_status(snapshot: RunSnapshot) ->
# str`，不做 TUI"）与其后的示例行（§9.1）：
#
#     phase=optimize  track=simulation_only  budget=137/400 starts
#     best=cand_7f3a91c2  worst_case_settling_time=38.2us  pm=52.1deg
#     interventions=0  wallclock=2.4h  llm_tokens=41200
#     stop_reason=no_improvement  artifacts=artifacts/t_2026_0901_transient/
#
# 本任务把该示例行的 key=value、双空格分隔风格原样延伸到本任务要求的完整字段
# 集合（当前阶段 / 验收轨道 / 已用与剩余预算 / 最佳候选 / cp1+cp3 与 cp2 两个
# 计数 / 累计墙钟 / llm token 合计 / 阻塞原因 / 产物目录），删除示例行里的
# `worst_case_settling_time` / `pm` 两项——它们是与具体指标绑定的展示字段，
# 而 `RunSnapshot` 只登记 `best_candidate_id`（候选标识本身），不登记该候选的
# 指标取值：`format_status()` 是通用状态行，不应该硬编码某个特定 `metric_id`
# 的展示口径（`primary`/`phase_margin` 换成别的指标后示例行的字段名就不成立
# 了）。示例行也没有把 `interventions` 拆成 cp1+cp3 与 cp2 两个计数，那是任务
# 15.2 描述在示例基础上新增的字段，本实现在示例的 `interventions=` 位置之后
# 按任务描述拆成 `cp1cp3=` 与 `cp2=` 两项。
#
# 「已删除状态行的『LLM 累计成本』字段」（design.md §0.5 判据 2）：只保留
# `llm_tokens` 一项，不登记任何单价或成本值。
# ===========================================================================


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """`format_status()` 的唯一输入：一份已解析、零 I/O 的状态快照
    （design.md §6.1 命名此类型但未给出字段，字段设计见下）。

    `phase` 是本类**唯一**内存态枚举字段（design.md §6.1「其余四项口径"当前
    阶段"」一节）：`{preflight, checkpoint1, optimize, finish}`，只用于显示、
    不参与任何判定、不新增任何表列——从 SQLite 重建后统一取 `optimize`（见
    `build_run_snapshot()`）。

    其余字段全部是对 `Store` / `BudgetLedger` 实时查询结果的**已解析快照**
    （纯值，不含任何数据库连接或可变账本对象），这正是"snapshot"这个名字的
    含义：`format_status()` 因此可以是一个不做任何查询的纯字符串格式化函数，
    在单元测试里可以直接构造任意 `RunSnapshot` 而不必先起一个 `Store`。

    - `task_id`：仅用于可能的调用方日志关联，不出现在 `format_status()` 的
      输出文本中（状态行本身不需要自报 `task_id`——它是"某个正在跑的任务"
      当次的状态行，调用方在外层上下文里已经知道是哪个任务）。
    - `simulation_only`：对应 `tasks.simulation_only`；`format_status()` 渲染
      为 `track=simulation_only` 或 `track=engineering`。
    - `budget_used` / `budget_remaining`：对应 `BudgetLedger.used()` /
      `.remaining()`。
    - `best_candidate_id`：当前最佳可行候选的 `candidate_id`；无则 `None`。
    - `cp1_plus_cp3_count` / `cp2_count`：`interventions` 按 `checkpoint`
      分组后 `cp1+cp3` 合计与 `cp2` 合计。
    - `wallclock_hours`：自 `tasks.started_at` 起的累计墙钟（小时，浮点，
      渲染时保留 1 位小数）。
    - `llm_tokens_total`：该 `task_id` 全部 `llm_calls.tokens` 之和。
    - `stop_reason` / `cause`：对应 `tasks.stop_reason` / `tasks.cause`；
      未停止（`stop_reason is None`）时两者均为 `None`。
    - `artifacts_dir`：产物目录路径（字符串）。
    """

    phase: Literal["preflight", "checkpoint1", "optimize", "finish"]
    task_id: str
    simulation_only: bool
    budget_used: int
    budget_remaining: int
    best_candidate_id: str | None
    cp1_plus_cp3_count: int
    cp2_count: int
    wallclock_hours: float
    llm_tokens_total: int
    stop_reason: str | None
    cause: str | None
    artifacts_dir: str


def format_status(snapshot: RunSnapshot) -> str:
    """把 `RunSnapshot` 渲染为单行状态文本（design.md §6.1；`tasks.md` 任务
    15.2；需求追溯 R20.2, R20.3）。

    纯函数、零 I/O：只读取 `snapshot` 的字段，不查询 `Store`、不触发任何
    Simulink 调用、不写任何文件。返回值不含换行符、不含光标定位 / 屏幕重绘 /
    分屏控件的转义序列——调用方（`run_task()`，尚未落地，任务 14.4/14.5）负责
    把返回值写到标准输出，本函数本身不调用 `click.echo()` / `print()`。

    渲染风格（key=value，双空格分隔）与字段选择见本节顶部模块内注释，逐字段
    延伸自 design.md §9.1 的示例行。
    """
    budget_total = snapshot.budget_used + snapshot.budget_remaining
    track = "simulation_only" if snapshot.simulation_only else "engineering"
    best = snapshot.best_candidate_id if snapshot.best_candidate_id is not None else "none"
    if snapshot.stop_reason is None:
        blocked = "none"
    else:
        blocked = f"{snapshot.stop_reason}:{snapshot.cause if snapshot.cause is not None else 'none'}"

    line = "  ".join(
        [
            f"phase={snapshot.phase}",
            f"track={track}",
            f"budget={snapshot.budget_used}/{budget_total} starts",
            f"best={best}",
            f"cp1cp3={snapshot.cp1_plus_cp3_count}",
            f"cp2={snapshot.cp2_count}",
            f"wallclock={snapshot.wallclock_hours:.1f}h",
            f"llm_tokens={snapshot.llm_tokens_total}",
            f"blocked={blocked}",
            f"artifacts={snapshot.artifacts_dir}",
        ]
    )
    assert "\n" not in line, "format_status: rendered line must not contain a newline"
    return line


def build_run_snapshot(
    store: Store,
    task_id: str,
    ledger: BudgetLedger,
    *,
    phase: Literal["preflight", "checkpoint1", "optimize", "finish"],
    metrics_cfg: MetricsConfig,
    artifacts_dir: str,
    top_n_for_best: int = 1,
) -> RunSnapshot:
    """查询 `store` / `ledger` 并组装一份 `RunSnapshot`（`tasks.md` 任务 15.2）。

    这是 `format_status()` 之外唯一做实际查询的函数，与 `format_status()` 本身
    的"零 I/O 纯格式化"职责分离——`format_status()` 只消费本函数（或测试代码）
    产出的快照，从不自己查询。

    **给 `run_task()`（任务 14.4/14.5，尚未落地）未来实现者的说明**：本函数
    应在设计要求的两个输出时机各调用一次——「每条 `runs` 行由 `running` 转
    终态时」与「每轮寻优结束时」——然后把返回值传给 `format_status()` 并写到
    标准输出。design.md §6.1 明确不引入定时器或按固定时间间隔输出的路径（长
    仿真期间打印的也只是同一份未变的快照，没有新信息），因此 `run_task()`
    的主循环不应该在这两个时机之外的任何地方调用本函数或 `format_status()`。
    本任务（15.2）不实现该主循环，`build_run_snapshot()` 因此在本任务范围内
    未被任何真实调用点引用——它是准备好等待 14.4/14.5 接线的"胶水"函数。

    - `phase`：由调用方直接给出（内存态枚举，不从任何持久化状态推断）；
      design.md §6.1 规定「从 SQLite 重建后取 `optimize`」，即进程重启后若
      调用方还没有更明确的阶段信息，应传入 `"optimize"`。
    - `best_candidate_id`：复用 `eval.aggregate.rank()`（任务 12.3）取
      `top_n_for_best=1` 的第一名——`rank()` 已实现"当前最佳可行候选"所需的
      完备性 / 可行性 / 三段定序逻辑，本函数不重新实现一遍同等的查询。空结果
      （尚无可行候选）时 `best_candidate_id` 取 `None`。
    - `cp1_plus_cp3_count` / `cp2_count`：`store.count_interventions_by_checkpoint()`
      按 checkpoint 分组的计数里，`cp1` 与 `cp3` 相加、`cp2` 单独取值。
    - `wallclock_hours`：`now - tasks.started_at` 的小时数；`started_at` 取
      `store.get_task_summary()` 返回的 ISO 8601 UTC 秒级时间戳
      （`store/repo.py` `_now_iso()` 的格式，`"%Y-%m-%dT%H:%M:%SZ"`）。
    - `task_id` 对应的 `tasks` 行不存在时抛 `ValueError`——这是调用方的用法
      错误（不应该为一个从未 `open` 过的任务构造状态快照），不是本函数需要
      静默兜底的情形。
    """
    from datetime import datetime, timezone

    summary = store.get_task_summary(task_id)
    if summary is None:
        raise ValueError(f"build_run_snapshot: unknown task_id={task_id!r}")

    ranked = rank(store, task_id, metrics_cfg, top_n=top_n_for_best)
    best_candidate_id = ranked[0].candidate_id if ranked else None

    checkpoint_counts = store.count_interventions_by_checkpoint(task_id)
    cp1_plus_cp3_count = checkpoint_counts["cp1"] + checkpoint_counts["cp3"]
    cp2_count = checkpoint_counts["cp2"]

    started_at = datetime.strptime(summary.started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    wallclock_hours = (datetime.now(timezone.utc) - started_at).total_seconds() / 3600.0

    return RunSnapshot(
        phase=phase,
        task_id=task_id,
        simulation_only=summary.simulation_only,
        budget_used=ledger.used(),
        budget_remaining=ledger.remaining(),
        best_candidate_id=best_candidate_id,
        cp1_plus_cp3_count=cp1_plus_cp3_count,
        cp2_count=cp2_count,
        wallclock_hours=wallclock_hours,
        llm_tokens_total=store.sum_llm_tokens(task_id),
        stop_reason=summary.stop_reason,
        cause=summary.cause,
        artifacts_dir=artifacts_dir,
    )


def _compute_current_result_hash(
    store: Store,
    task_id: str,
    candidate_id: str,
    *,
    config_dir: Path | None = None,
) -> str:
    """从库内当前状态为 `candidate_id` 重算 `result_hash`（`config.hashing.
    result_hash()` 的 9 键范围），供 `cmd_report` 的 Checkpoint 3 审批绑定。

    聚合逻辑本身在 `report.context.compute_result_hash()`——9 个键里有 7 个的
    数据源那里已经为报告渲染各查过一遍，在本文件重写那几段联表 SQL 会引入两处
    必须逐字符同步的查询（详见该函数上方的模块内说明）。本函数只承担 CLI 边界
    的一件事：**决定配置从哪读**。

    `config_dir` 默认 `configs/`，与 `report.render.DEFAULT_CONFIG_DIR` 及
    `cmd_run` 是同一约定（design.md 全篇示例均用相对路径，没有任何 CLI 选项
    承载可配置的配置目录）。它是 kwonly 参数只为让测试指向 fixture 配置，
    `cmd_report` 的调用点不传。

    配置漂移 / 候选不存在 / 候选未通过完备性过滤三种情形抛
    `ResultHashUnavailableError`，由调用方转译为 `click.UsageError`。
    """
    from poweragent.report.context import compute_result_hash

    bundle = load_all(config_dir or _DEFAULT_CONFIG_DIR)
    return compute_result_hash(
        task_id,
        store,
        candidate_id,
        metrics_cfg=bundle.metrics,
        constraints_cfg=bundle.constraints,
    )


def _open_store_for_existing_task(task_id: str) -> Store:
    """打开 `DEFAULT_DB_PATH` 处的 `Store` 并校验 `task_id` 存在；不存在（含
    数据库文件本身不存在）时抛 `click.UsageError`（退出码 1），不创建任何文件、
    不打开任何连接。

    `--task` 指向不存在的 `task_id` 是 `report`/`log`（以及间接地 `run` 的重跑
    路径）共用的结构性校验，收拢在此处避免三处重复实现。
    """
    if not DEFAULT_DB_PATH.exists():
        raise click.UsageError(
            f"task not found: task_id={task_id!r} (no database at "
            f"{DEFAULT_DB_PATH} — no task has ever been run)"
        )
    store = Store(DEFAULT_DB_PATH)
    if not store.task_exists(task_id):
        raise click.UsageError(f"task not found: task_id={task_id!r}")
    return store


# ===========================================================================
# click 命令组：恰好四个子命令
# ===========================================================================


@click.group(name="poweragent")
def cli() -> None:
    """PowerAgent：多相 Buck 供电模块参数寻优的单机 CLI。"""


@cli.command(name="run")
@click.argument("task_yaml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--resume", is_flag=True, default=False, help="从既有任务状态恢复。")
@click.option("--yes", is_flag=True, default=False, help="Checkpoint 1 直接判为确认。")
def run(task_yaml: Path, resume: bool, yes: bool) -> None:
    """`poweragent run <task.yaml>`：加载配置并寻优。"""
    exit_code = cmd_run(task_yaml, resume=resume, yes=yes)
    raise SystemExit(exit_code)


@cli.command(name="report")
@click.option("--task", "task_id", required=True, help="`tasks.task_id`。")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="报告输出路径。")
@click.option("--approve", "approve", default=None, metavar="CANDIDATE_ID")
@click.option("--reject", "reject", default=None, metavar="CANDIDATE_ID")
# design.md §6.1、requirements.md Requirement 16 AC13 / Requirement 19 AC13：
# 审批人身份只能来自这个命令行选项本身——刻意不设 `envvar=`，也不在 `cmd_report`
# 里对缺失的 `approver` 尝试任何来源的推断（环境变量、`git config user.name`
# 等）。自动推断身份会让误签变得无声，因此缺失时只能是 `click.UsageError`。
@click.option("--approver", default=None)
@click.option("--second-approver", "second_approver", default=None)
@click.option("--note", default=None)
def report(
    task_id: str,
    out: Path | None,
    approve: str | None,
    reject: str | None,
    approver: str | None,
    second_approver: str | None,
    note: str | None,
) -> None:
    """`poweragent report --task <id> [--approve ... | --reject ...]`：
    无 approve/reject 时仅渲染报告；给出其一时同时写
    `approvals(kind='final_recommendation')`（Checkpoint 3 审批入口）。"""
    exit_code = cmd_report(
        task_id,
        out=out,
        approve=approve,
        reject=reject,
        approver=approver,
        second_approver=second_approver,
        note=note,
    )
    raise SystemExit(exit_code)


@cli.command(name="apply")
@click.option("--candidate", "candidate_id", required=True, help="`candidates.candidate_id`。")
@click.option("--approval", "approval_id", required=True, help="`approvals.approval_id`。")
def apply(candidate_id: str, approval_id: str) -> None:
    """`poweragent apply --candidate <id> --approval <id>`：批准后另存新模型。"""
    exit_code = cmd_apply(candidate_id, approval_id=approval_id)
    raise SystemExit(exit_code)


@cli.command(name="log")
@click.option("--task", "task_id", required=True, help="`tasks.task_id`。")
@click.option("--reason", required=True, help="1~500 字符非空文本。")
@click.option(
    "--checkpoint",
    type=click.Choice(["cp1", "cp2", "cp3", "other"], case_sensitive=False),
    default="other",
)
@click.option("--action", default="", help="缺省空字符串。")
def log(task_id: str, reason: str, checkpoint: str, action: str) -> None:
    """`poweragent log --task <id> --reason <text> [--checkpoint <cp>] [--action <text>]`：
    过程埋点记账。"""
    exit_code = cmd_log(task_id, reason, checkpoint=checkpoint.lower(), action=action)
    raise SystemExit(exit_code)


# ===========================================================================
# cmd_* 函数：design.md §6.1 登记的签名，供 click 回调与测试直接调用
# ===========================================================================


def cmd_run(task_yaml: Path, *, resume: bool = False, yes: bool = False) -> int:
    """加载四个配置文件并调用 `run_task()`（软依赖，见模块 docstring）。

    - `ConfigLoadError`（含占位符 / 结构 / 字段类型 / unit 不一致 / 数值越界的
      任一不合规）⟹ 打印全部问题、返回退出码 1，不进入寻优。
    - `PreflightError`（`controller.preflight.PreflightError`，`run_task()` 内部
      在进入寻优主循环前调用 `preflight()` 时抛出）⟹ 打印错误信息、返回退出码
      1、不进入寻优——`preflight()` 失败时对应的 `tasks` 行从未创建，因此从不
      产出任何 `stop_reason`，这一路径不落在 `_exit_code_for_stop_reason()` 的
      四值映射域内，须在此单独 `except`（design.md §6.1/§11.2、requirements.md
      Requirement 16 AC16 「参数或配置错误（含 `PreflightError`）⟹ 1」）。
    - `run_task()` 尚未落地 ⟹ 捕获 `ImportError`/`ModuleNotFoundError`，打印
      过渡期提示，返回退出码 1（这不是「参数或配置错误」的字面情形，但在
      `run_task()` 落地前，`cmd_run` 无法产出任何 `stop_reason`，退出码 1 —
      「未进入寻优」— 是三值映射里唯一适用的取值）。
    - `run_task()` 成功返回 `TaskOutcome` ⟹ 按 `_exit_code_for_stop_reason()`
      映射其 `stop_reason` 为退出码。
    """
    config_dir = task_yaml.resolve().parent
    try:
        bundle = load_all(config_dir)
    except ConfigLoadError as exc:
        for issue in exc.issues:
            click.echo(f"{issue.category}: {issue.file}.{issue.field_path}: {issue.message}", err=True)
        return 1

    try:
        from poweragent.controller.run_task import run_task
    except ImportError as exc:
        click.echo(
            "run_task() 尚未落地（controller/run_task.py，任务 14.4/14.5）；"
            f"配置已通过校验，寻优主循环暂不可执行（{exc})",
            err=True,
        )
        return 1

    from poweragent.controller.preflight import PreflightError

    try:
        outcome = run_task(
            bundle.task,
            bundle.model,
            bundle.metrics,
            bundle.constraints,
            resume=resume,
            yes=yes,
        )
    except PreflightError as exc:
        click.echo(f"preflight failed: {exc}", err=True)
        return 1

    return _exit_code_for_stop_reason(outcome.stop_reason)


def cmd_report(
    task_id: str,
    *,
    out: Path | None = None,
    approve: str | None = None,
    reject: str | None = None,
    approver: str | None = None,
    second_approver: str | None = None,
    note: str | None = None,
) -> int:
    """无 approve/reject 时仅渲染报告；给出其一时同时写
    `approvals(kind='final_recommendation')`（design.md §6.1）。

    结构性校验（全部在任何写入之前完成，失败即 `click.UsageError`，退出码 1，
    不写任何数据库行、不生成任何产物文件）：

    - `--task` 指向不存在的 `task_id`。
    - `--approve` 与 `--reject` 同时给出（互斥）。
    - `--approve` 给出但缺 `--approver` 或 `--second-approver`。
    - `--reject` 给出但缺 `--approver`。
    - `result_hash` 算不出来：当前 `metrics.yaml`/`constraints.yaml` 的哈希与
      `tasks` 行记录的不一致（配置漂移）、`candidate_id` 不在 `candidates`
      表中、或该候选未通过 worst-case 聚合的完备性过滤（有场景没跑完，或在某个
      评价场景上不可行）。这一步排在 `record_approval()` 之前：审批行一旦落库
      即为不可否认的记录，不能先写行再发现它绑定的哈希算不出来。

    `approver` / `second_approver` 两个形参的取值只来自上面 `--approver` /
    `--second-approver` 两个 `click.Option`（均未设 `envvar=`）——本函数在
    `approver`/`second_approver` 为 `None` 时只报错，不读取 `os.environ`、不
    调用 `git config user.name` 之类的外部命令去猜一个默认值（design.md
    §6.1/§14、requirements.md Requirement 16 AC13、Requirement 19 AC13）。
    """
    store = _open_store_for_existing_task(task_id)

    if approve and reject:
        raise click.UsageError("--approve 与 --reject 互斥，一次只能给出其一")

    if approve:
        if not approver or not second_approver:
            raise click.UsageError(
                "--approve 需要同时给出 --approver 与 --second-approver"
            )
    elif reject:
        if not approver:
            raise click.UsageError("--reject 需要给出 --approver")

    candidate_id = approve or reject
    if candidate_id is not None:
        # 配置漂移 / 候选不存在 / 候选未通过 worst-case 完备性过滤三种情形下
        # 不存在可绑定的结果，转译为退出码 1 的清晰提示，不写 `approvals` 行。
        # 这一步刻意在 `record_approval()` 之前：审批行一旦落库就是不可否认的
        # 记录，不能先写行再发现绑定的哈希算不出来。
        from poweragent.report.context import ResultHashUnavailableError

        try:
            result_hash = _compute_current_result_hash(store, task_id, candidate_id)
        except ResultHashUnavailableError as exc:
            raise click.UsageError(str(exc)) from exc
        except ConfigLoadError as exc:
            raise click.UsageError(
                f"configs/ 加载失败，无法重算 result_hash：{exc}"
            ) from exc

        rec = ApprovalRecord(
            task_id=task_id,
            kind="final_recommendation",
            decision="approve" if approve else "reject",
            approver=approver,  # type: ignore[arg-type]
            second_approver=second_approver,
            candidate_id=candidate_id,
            result_hash=result_hash,
            note=note,
        )
        try:
            store.record_approval(rec)
        except ApprovalValidationError as exc:
            raise click.UsageError(str(exc)) from exc
        return 0

    try:
        from poweragent.report.render import render_report
    except ImportError as exc:
        click.echo(
            "render_report() 尚未落地（report/render.py，任务 26.x）；"
            f"报告渲染暂不可执行（{exc})",
            err=True,
        )
        return 1

    path = render_report(task_id, store=store, out=out)
    click.echo(str(path))
    return 0


def cmd_apply(candidate_id: str, *, approval_id: str) -> int:
    """批准后另存新模型（`sim.apply_model.apply_candidate`，软依赖，见模块
    docstring）。`controller` 与 `agent` 均无权调用此函数——`cmd_apply` 是唯一
    入口（design.md §7.6 / requirements.md R19.10）。"""
    try:
        from poweragent.sim.apply_model import apply_candidate
    except ImportError as exc:
        click.echo(
            "apply_candidate() 尚未落地（sim/apply_model.py）；"
            f"模型落盘暂不可执行（{exc})",
            err=True,
        )
        return 1

    apply_candidate(candidate_id, approval_id=approval_id)
    return 0


def cmd_log(task_id: str, reason: str, *, checkpoint: str = "other", action: str = "") -> int:
    """`store.log_intervention()` 记账（design.md §6.1；已落地、可完整实现）。

    结构性校验（失败即 `click.UsageError`，退出码 1，不写任何数据库行）：
    `--task` 指向不存在的 `task_id`；`reason` 长度须在 1~500 字符（`click.Choice`
    已在参数解析阶段挡住 `--checkpoint` 越域，不在此重复）。
    """
    if not (1 <= len(reason) <= 500):
        raise click.UsageError(
            f"--reason 长度须在 1~500 字符之间，实际为 {len(reason)}"
        )

    store = _open_store_for_existing_task(task_id)
    store.log_intervention(task_id, checkpoint, reason, action)
    return 0


def main(argv: list[str] | None = None) -> int:
    """`design.md` §6.1 登记的入口签名：`main(argv) -> int`。

    委托 `cli.main(standalone_mode=True)`（click 默认行为：捕获
    `click.ClickException`/`click.UsageError` 等并转译为错误消息 + 该异常自带的
    退出码），但拦截其内部触发的 `SystemExit`、转成普通返回值，使本函数遵守
    `-> int` 的字面签名、不主动终止调用方进程（`standalone_mode=False` 不满足
    这一点——它会让 `ClickException` 原样向外传播而不是被转译为退出码，与
    `poweragent log --task <nonexistent>` 一类的正常拒绝路径不符）。
    """
    try:
        cli.main(args=argv, standalone_mode=True)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
