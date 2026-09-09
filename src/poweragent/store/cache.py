"""poweragent/store/cache.py

两级缓存键（`design.md` §5.3，`tasks.md` T7 / 任务 9.1）。

本模块只提供两个纯函数，不做任何 I/O、不读配置文件、不查数据库：

- `simulation_key()`：参与哈希的字段集合**封闭为七项**——
  `model_package_hash`、`model_variant`、`execution_env_hash`、
  `candidate_normalized`、`scenario_id`、`scenario_spec_version`、
  `margin_primary_method`。哈希算法与序列化规则复用
  `poweragent.config.hashing.canonical_json`（§5.3.1，全文唯一登记处），本模块
  不重新定义。
- `evaluation_key()`：在 `simulation_key` 之上叠加 `metrics_hash` 与
  `constraints_hash` 两项，两者**不进** `simulation_key` 的字段集合。

## `candidate_normalized` 取 `candidate.parameters_si`，不取 `candidate.candidate_id`

`candidate_id` 本身是 `sha256(canonical_json(parameters_si))[:16]`（`config/hashing.py`
的 `candidate_id()`），即从 `parameters_si` 派生而来。若 `simulation_key` 再取
`candidate_id` 而不是 `parameters_si`，键就间接依赖一个截断哈希而非原始参数值，
既多绕一层、又让 `simulation_key` 的可追溯性弱于直接引用参数——因此本模块直接把
`candidate.parameters_si`（一个 `Mapping[str, float]`）放进七字段字典，交给
`canonical_json` 规范化，不改用 `candidate_id`。

## `margin_primary_method` 由调用方决定，本函数不读 `scenario.require_margin`

`design.md` §5.3 的字段说明是「仅 `scenario.require_margin=true` 时取
`metrics.yaml` 的 `margin_extraction.primary_method`，否则传空值」——这描述的是
**调用方**在调用本函数前应如何计算要传入的值，不是本函数内部的分支逻辑。函数签名
已经把 `margin_primary_method: str | None` 列为独立形参（而不是额外接收
`require_margin: bool` 与 `metrics_config` 后自行判断），本函数的职责只是「按给定
的七个值原样构造字典并取哈希」，信任调用方已经做完 `require_margin` 判断——这是
更简单也更符合本模块「纯键构造器、不碰配置」的边界。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from poweragent.config.hashing import canonical_json

if TYPE_CHECKING:
    from poweragent.store.repo import Candidate, ModelVariant, ScenarioSpec

__all__ = ["simulation_key", "evaluation_key"]


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def simulation_key(
    *,
    model_package_hash: str,
    model_variant: "ModelVariant",
    execution_env_hash: str,
    candidate: "Candidate",
    scenario: "ScenarioSpec",
    margin_primary_method: str | None,
) -> str:
    """`sha256(canonical_json({七个字段}))`，不截断小写十六进制（`design.md` §5.3）。

    七字段闭集：`model_package_hash`、`model_variant`、`execution_env_hash`、
    `candidate_normalized`（= `candidate.parameters_si`）、`scenario_id`
    （= `scenario.scenario_id`）、`scenario_spec_version`（= `scenario.spec_version`）、
    `margin_primary_method`（原样传入，`None` 表示不参与本次采集的裕量方法约束）。

    `margin_primary_method` 是否为 `None` 由调用方按 `scenario.require_margin`
    决定，本函数不读取 `scenario.require_margin`（见模块顶部说明）。

    `model_variant` 取场景行**声明**的变体，不因裕量采集实际发生在另一变体上而
    改写——调用方传入时应使用 `scenario.model_variant`，本函数按传入值原样入键。
    """
    fields = {
        "model_package_hash": model_package_hash,
        "model_variant": model_variant,
        "execution_env_hash": execution_env_hash,
        "candidate_normalized": dict(candidate.parameters_si),
        "scenario_id": scenario.scenario_id,
        "scenario_spec_version": scenario.spec_version,
        "margin_primary_method": margin_primary_method,
    }
    return _sha256_hex(canonical_json(fields))


def evaluation_key(*, simulation_key: str, metrics_hash: str, constraints_hash: str) -> str:
    """`sha256(canonical_json({simulation_key, metrics_hash, constraints_hash}))`，
    不截断小写十六进制（`design.md` §5.3）。

    `metrics_hash` 与 `constraints_hash` 不出现在 `simulation_key()` 的字段集合
    内——两者只在本函数中进入键链，指标/约束重算（两者之一变更）只改变
    `evaluation_key`、不使已缓存的仿真产物（`simulation_key` 命中）失效。
    """
    fields = {
        "simulation_key": simulation_key,
        "metrics_hash": metrics_hash,
        "constraints_hash": constraints_hash,
    }
    return _sha256_hex(canonical_json(fields))
