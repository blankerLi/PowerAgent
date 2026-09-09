"""poweragent/config/dual_model_consistency.py

`model.yaml` 的 `dual_model_consistency.checkpoints` 节的独立校验（design.md §4.2，
`tasks.md` 任务 4.1；需求 R4.2, R24.1）。

暂存位置说明：本模块的校验逻辑按设计应归入 `config/schema.py` 的 pydantic v2 模型
（任务 5.1，与本任务 4.1 并行派发）。任务 5.1 落地时，应把
`validate_dual_model_consistency_checkpoints()` 的校验规则改写为
`ModelConfig`（或其 `DualModelConsistency` 子模型）的 pydantic validator，
并删除本模块——本模块只是任务 5.1 落地前的过渡实现，不重复登记校验规则的权威定义。

校验规则（仅当 `model.yaml` 的 `averaged_model_required=true` 时必须调用本函数并
要求通过；`averaged_model_required=false` 时 `dual_model_consistency` 节按模板留空，
不调用本函数）：
- `dual_model_consistency.checkpoints` 至少 1 条。
- 每条 checkpoint 的 `scenario_id` 必须存在于 `task.yaml` 声明的 `scenario_id` 集合中。
- 每条 checkpoint 的 `steady_tol` 与 `transient_tol` 必须为大于 0 的有限数值。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


class DualModelConsistencyConfigError(ValueError):
    """`dual_model_consistency.checkpoints` 不合规时抛出，消息含具体字段路径。"""


def _is_positive_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and value not in (
        float("inf"),
        float("-inf"),
    )


def validate_dual_model_consistency_checkpoints(
    model_config: Mapping[str, Any],
    *,
    task_scenario_ids: Sequence[str],
) -> None:
    """校验 `model_config` 的 `dual_model_consistency.checkpoints` 节。

    `task_scenario_ids` 为 `task.yaml` 中 `scenarios` 显式行声明的 `scenario_id`
    集合（任务 14.1 `controller/scenario.py` 的读取来源相同）。

    不合规时抛出 `DualModelConsistencyConfigError`，消息标识具体字段路径与原因；
    合规时不返回任何值。
    """
    dual_model_consistency = model_config.get("dual_model_consistency") or {}
    checkpoints = dual_model_consistency.get("checkpoints") or []

    if len(checkpoints) < 1:
        raise DualModelConsistencyConfigError(
            "dual_model_consistency.checkpoints: 至少需要 1 条 checkpoint"
        )

    known_scenario_ids = set(task_scenario_ids)

    for index, checkpoint in enumerate(checkpoints):
        path_prefix = f"dual_model_consistency.checkpoints[{index}]"

        scenario_id = checkpoint.get("scenario_id")
        if not scenario_id or scenario_id not in known_scenario_ids:
            raise DualModelConsistencyConfigError(
                f"{path_prefix}.scenario_id: {scenario_id!r} 未在 task.yaml 的 "
                f"scenario_id 集合中声明"
            )

        for field in ("steady_tol", "transient_tol"):
            value = checkpoint.get(field)
            if not _is_positive_finite(value):
                raise DualModelConsistencyConfigError(
                    f"{path_prefix}.{field}: {value!r} 必须为大于 0 的有限数值"
                )
