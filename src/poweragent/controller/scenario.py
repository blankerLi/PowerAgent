"""poweragent/controller/scenario.py

`screening_rows()` / `evaluation_rows()` / `robustness_rows()` / `freeze_scenario_set()`
(design.md §6.2.4；tasks.md 任务 14.1；需求追溯 R6.1, R6.2, R6.3)。

设计不变量（供后续静态断言类测试引用）
--------------------------------------
本模块只从 `task_cfg.scenarios`（`config.schema.TaskConfig` 解析 `task.yaml` 的
`scenarios` 显式行所得）按 `tier` 过滤并转换为跨模块契约类型，**不存在**对
`vin_v` / `temp_c` / `load_start_a` / `load_end_a` / `slew_a_per_us` 取值集合做
笛卡尔积或任何形式的组合展开的代码路径。`task.yaml` 的场景行本身就是显式、已确定
的电气量元组；本模块没有、也不应该有从取值集合“生成”场景行的能力。

`scenario_id` 在同一任务内的唯一性已由 `config.schema.TaskConfig._check_scenario_id_unique`
（任务 5.1）强制，本模块不重复校验，只依赖该上游保证。

`freeze_scenario_set()` 按 `task_cfg.scenarios` 的行序写入 `scenario_set` 表，因此
冻结后的行数与 `scenario_id` 集合与 `task.yaml` 的 `scenarios` 行天然一一对应
（行数相等、`scenario_id` 集合相等）；这不需要额外的断言代码来保证，但本模块仍在
写入后做一次开销很小的行数核对，便于审计（design.md 允许的"可选、但足够廉价"防御
性检查）。

两处暂定的软依赖（`store/repo.py` 尚未落地时的应对，任务 8.3 与本任务 14.1 并行派发）
--------------------------------------------------------------------------------
1. **`ScenarioSpec` 的两种形状**：`config.schema.ScenarioSpec`（pydantic，config 层，
   校验 `task.yaml` 用）与 design.md §5.2 文档化的 `store.repo.ScenarioSpec`
   （frozen dataclass，跨模块契约类型，`eval`/`sim`/`store` 层消费）字段并不完全相同——
   后者比前者多一个 `spec_version: str` 字段。`controller/scenario.py` 处于 config 层
   与其余层的边界上，因此本模块的三个 `*_rows()` 函数按设计应当返回契约类型而不是
   config 类型，避免 config 层的类型泄漏进 eval/sim/store 层。截至本次实现，
   `poweragent/store/repo.py` 尚不存在（任务 8.3 未落地），所以本模块在下方
   **临时**定义了一个与 design.md §5.2 字段形状一致的本地 `ScenarioSpec` dataclass，
   并补齐 `spec_version` 字段（取值见下）。**一旦 `store/repo.py` 落地，应删除本模块
   的本地定义、改为 `from poweragent.store.repo import ScenarioSpec` 并验证字段形状
   仍一致**——两侧字段除 `spec_version` 外是逐字段相同的映射，转换是显式的 1:1 拷贝
   （`_to_repo_scenario_spec`），不是简单地重新导出 pydantic 对象，以保持层边界清晰。
2. **`Store` 类**：design.md §6.2.4 的签名是 `freeze_scenario_set(task_cfg, store: Store)`，
   但 `poweragent/store/repo.py`（`Store` 类的落地位置）尚不存在；`store/db.py` 目前只
   提供裸的 `sqlite3.Connection` 助手（`connect()` / `tx()` / `init_db()`），没有 `Store`
   对象可供 `store.tx()` 调用。因此本模块直接接受一个 `sqlite3.Connection`（参数名
   `conn`），并复用 `poweragent.store.db.tx()` 做事务边界（`BEGIN IMMEDIATE` … 提交/回滚），
   这是当前代码库中实际可用、且与 design.md「原子性由构造保证」精神一致的最小方案。
   一旦 `Store` 落地，`freeze_scenario_set` 的签名应改为接受 `Store` 并委托其暴露的
   连接或专用写入方法，以贴合 design.md 的原始签名。

`spec_version` 的取值
----------------------
design.md 未给出 `spec_version` 的具体取值规则（它只在 `simulation_key` 的哈希输入
字段列表与 `scenario_set` 表列中出现，用途是“场景规格格式演进时的版本标记”）。本模块
把它落成一个模块级字面量常量 `SCENARIO_SPEC_VERSION = "1"`，作为“规格格式版本”的
占位约定，不代表任何物理量；格式若发生不兼容变更，才递增该常量。

`freeze_scenario_set()` 不改写 `tasks.scenario_set_hash`
--------------------------------------------------------
design.md §6.2.4 只要求「`tasks` 行的该列等于返回值」，未强制由本函数完成写入；
`tasks` 行的创建与整体落库时机属于任务 14.5（`controller/run_task.py` 主循环）的
职责范围。本函数只做一件事——写 `scenario_set` 表并返回 `scenario_set_hash`——调用方
负责把返回值写入 `tasks.scenario_set_hash`。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from poweragent.config.hashing import canonical_json
from poweragent.config.hashing import scenario_set_hash as _compute_scenario_set_hash
from poweragent.config.schema import ModelVariant, ScenarioTier, TaskConfig
from poweragent.config.schema import ScenarioSpec as ConfigScenarioSpec
from poweragent.store.db import tx

__all__ = [
    "SCENARIO_SPEC_VERSION",
    "ScenarioSpec",
    "screening_rows",
    "evaluation_rows",
    "robustness_rows",
    "freeze_scenario_set",
    "scenario_set_rows",
    "compute_scenario_set_hash",
]

# 场景规格格式版本占位约定，见模块 docstring「`spec_version` 的取值」。
SCENARIO_SPEC_VERSION = "1"

# 参与 spec_json（写入 scenario_set.spec_json 与 simulation_key）的电气量字段，
# design.md §4.1 的场景行电气量五项。
_ELECTRICAL_FIELDS: tuple[str, ...] = (
    "vin_v",
    "temp_c",
    "load_start_a",
    "load_end_a",
    "slew_a_per_us",
)


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """跨模块契约的场景行（design.md §5.2）。

    临时本地定义，见模块 docstring 软依赖说明第 1 条；一旦 `store/repo.py` 落地，
    应改为从该模块导入同名 dataclass。
    """

    scenario_id: str
    tier: ScenarioTier
    model_variant: ModelVariant
    require_margin: bool
    vin_v: float
    temp_c: float
    load_start_a: float
    load_end_a: float
    slew_a_per_us: float
    spec_version: str


def _to_repo_scenario_spec(spec: ConfigScenarioSpec) -> ScenarioSpec:
    """显式的 1:1 字段拷贝：config 层 pydantic `ScenarioSpec` → 跨模块契约
    dataclass `ScenarioSpec`，并补上契约类型独有的 `spec_version` 字段。
    """
    return ScenarioSpec(
        scenario_id=spec.scenario_id,
        tier=spec.tier,
        model_variant=spec.model_variant,
        require_margin=spec.require_margin,
        vin_v=spec.vin_v,
        temp_c=spec.temp_c,
        load_start_a=spec.load_start_a,
        load_end_a=spec.load_end_a,
        slew_a_per_us=spec.slew_a_per_us,
        spec_version=SCENARIO_SPEC_VERSION,
    )


def screening_rows(task_cfg: TaskConfig) -> tuple[ScenarioSpec, ...]:
    """`task_cfg.scenarios` 中 `tier == 'screening'` 的行，保持 `task.yaml` 声明顺序。

    纯过滤，不做任何组合展开（见模块 docstring「设计不变量」）。
    """
    return tuple(
        _to_repo_scenario_spec(spec)
        for spec in task_cfg.scenarios
        if spec.tier == "screening"
    )


def evaluation_rows(task_cfg: TaskConfig) -> tuple[ScenarioSpec, ...]:
    """`task_cfg.scenarios` 中 `tier == 'evaluation'` 的行，保持 `task.yaml` 声明顺序。"""
    return tuple(
        _to_repo_scenario_spec(spec)
        for spec in task_cfg.scenarios
        if spec.tier == "evaluation"
    )


def robustness_rows(task_cfg: TaskConfig) -> tuple[ScenarioSpec, ...]:
    """`task_cfg.scenarios` 中 `tier == 'robustness'` 的行，保持 `task.yaml` 声明顺序。"""
    return tuple(
        _to_repo_scenario_spec(spec)
        for spec in task_cfg.scenarios
        if spec.tier == "robustness"
    )


def _spec_json(spec: ConfigScenarioSpec) -> str:
    """场景电气量五项（`vin_v` / `temp_c` / `load_start_a` / `load_end_a` /
    `slew_a_per_us`）的规范化 JSON，写入 `scenario_set.spec_json` 列。
    """
    return canonical_json({field: getattr(spec, field) for field in _ELECTRICAL_FIELDS})


def scenario_set_rows(task_cfg: TaskConfig) -> list[dict[str, object]]:
    """按 `task_cfg.scenarios` 的行序构造（但不写入）`scenario_set` 表行的字典
    表示，逐字段对应 `store/ddl.sql` 的 `scenario_set` 表列。

    抽出为独立函数（`freeze_scenario_set()` 内联构造的同一段逻辑），供
    `controller/preflight.py` 的 `check_freeze_consistency()`（任务 14.2）在
    `freeze_scenario_set()` 实际写库**之前**算出「若现在冻结将得到的
    `scenario_set_hash`」、与已冻结的 `freezes(kind='safety')` 记录比较——两处
    共用同一份行构造逻辑，避免字段列表/顺序在两个模块间重复书写而漂移。
    """
    return [
        {
            "task_id": task_cfg.task_id,
            "scenario_id": spec.scenario_id,
            "tier": spec.tier,
            "model_variant": spec.model_variant,
            "require_margin": int(spec.require_margin),
            "spec_json": _spec_json(spec),
            "spec_version": SCENARIO_SPEC_VERSION,
        }
        for spec in task_cfg.scenarios
    ]


def compute_scenario_set_hash(task_cfg: TaskConfig) -> str:
    """`scenario_set_rows(task_cfg)` 的 `scenario_set_hash`，不写入数据库。

    供 `controller/preflight.py` 的 `check_freeze_consistency()` 在
    `freeze_scenario_set()` 尚未执行时（`preflight()` 先于该函数调用，见
    `design.md` §8.1 的 `run_task()` 伪代码）算出「当前配置对应的
    `scenario_set_hash`」用于跨冻结点一致性比较。
    """
    return _compute_scenario_set_hash(scenario_set_rows(task_cfg))


def freeze_scenario_set(task_cfg: TaskConfig, conn: sqlite3.Connection) -> str:
    """按 `task_cfg.scenarios` 的行序把全部场景行写入 `scenario_set` 表，返回
    `scenario_set_hash`。

    `conn` 为裸 `sqlite3.Connection`（见模块 docstring 软依赖说明第 2 条）；写入
    在单个 `poweragent.store.db.tx()` 事务内完成。调用方负责把返回值写入
    `tasks.scenario_set_hash`（本函数不改写 `tasks` 表）。
    """
    rows = scenario_set_rows(task_cfg)

    with tx(conn):
        for row in rows:
            conn.execute(
                "INSERT INTO scenario_set "
                "(task_id, scenario_id, tier, model_variant, require_margin, "
                "spec_json, spec_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["task_id"],
                    row["scenario_id"],
                    row["tier"],
                    row["model_variant"],
                    row["require_margin"],
                    row["spec_json"],
                    row["spec_version"],
                ),
            )

    # 廉价的审计性核对：写入行数应恰等于 task_cfg.scenarios 的长度（1:1 对应）。
    (written_count,) = conn.execute(
        "SELECT COUNT(*) FROM scenario_set WHERE task_id = ?",
        (task_cfg.task_id,),
    ).fetchone()
    assert written_count == len(task_cfg.scenarios), (
        f"freeze_scenario_set: 写入 {written_count} 行，"
        f"与 task_cfg.scenarios 的 {len(task_cfg.scenarios)} 行不一致"
    )

    return _compute_scenario_set_hash(rows)
