"""poweragent/config/ticks.py

`design_space.variables.<name>.ticks` 的展开与档位字面量规范化
（`tasks.md` 任务 5.9；需求追溯 R3.16）。

本模块的范围
------------
任务 5.1（`config/schema.py`）已定义 `ticks` 字段的两种形状
（显式列表 `list[float]` / 隐式展开式 `TicksCount`），任务 5.8（同文件的
`_check_ticks_geometric_and_tolerance`）已校验其等比性与 `tick_match_rel_tol`
的容差关系——但两者都明确不做展开本身。本模块补上这一步：把 `ticks`
（无论哪种形状）展开为具体的档位值列表，并把每个档位值经
`config.hashing.format_tick_literal()`（`.12g`，任务 5.3 登记的唯一档位字面量
格式化规则）规范化为十进制文本。

前置条件（本模块不重复校验）
----------------------------
本模块假定传入的 `DesignVariableSpec` / `ConstraintsConfig` 已通过
`config.schema` 的 pydantic 校验与 `check_cross_file_consistency()`
（任务 5.8）——即 `ticks` 显式列表已确认等比、`TicksCount.count >= 2`、
`domain` 闭区间合法。`expand_ticks()` 不重新执行这些检查，只做展开与格式化，
避免与任务 5.8 的校验职责重复。

为什么返回字符串而不是浮点数
----------------------------
R3.16 的核心要求是「写入 prompt 的档位字面量与校验时的档位值是同一组十进制
文本」：同一个浮点数在两处分别调用 `format(v, ".12g")` 数值上当然相等，但
只有让两处调用方共享**同一次**格式化产出的字符串，才能从代码结构上杜绝
「两处各自格式化、理论等价但未被同一函数保证」的隐患。因此本模块直接返回
`tuple[str, ...]`，`agent.prompt.build_prompt`（任务 23.x）与 `agent.validate`
（任务 24.x）都应直接使用这些字符串本身，不应再对浮点数重新调用
`format_tick_literal()`。
"""

from __future__ import annotations

from .hashing import format_tick_literal
from .schema import ConstraintsConfig, DesignVariables, DesignVariableSpec, TicksCount

__all__ = ["expand_ticks", "expand_all_ticks"]


def expand_ticks(spec: DesignVariableSpec) -> tuple[str, ...]:
    """展开单个设计变量的 `ticks` 为 `.12g` 规范化的十进制文本元组。

    - `spec.ticks` 为显式列表时：按给定顺序逐项经 `format_tick_literal()`
      规范化后返回，不重新排序、不重新校验等比性（已由任务 5.8 的
      `_check_ticks_geometric_and_tolerance` 在配置加载期完成）。
    - `spec.ticks` 为 `TicksCount(count, spacing='log')` 时：以 `spec.domain`
      两端为端点，展开为 `count` 个按对数等比排列的档位（两端点均计入这
      `count` 个值，即标准的几何/对数等比序列
      `domain[0] * (domain[1] / domain[0]) ** (i / (count - 1))`，
      `i` 取 `0..count-1`），按升序返回，每项经 `format_tick_literal()`
      规范化。

    前置条件：`spec` 须已通过 `config.schema` 的 pydantic 校验（`domain`
    严格递增、`ticks` 已确认等比等），本函数不重新校验。
    """
    ticks = spec.ticks

    if isinstance(ticks, TicksCount):
        low, high = spec.domain
        count = ticks.count
        ratio = high / low
        values = (low * ratio ** (i / (count - 1)) for i in range(count))
        return tuple(format_tick_literal(v) for v in values)

    return tuple(format_tick_literal(v) for v in ticks)


def expand_all_ticks(constraints_cfg: ConstraintsConfig) -> dict[str, tuple[str, ...]]:
    """展开 `constraints_cfg.design_space.variables` 中全部设计变量
    （`rcomp` / `ccomp`）的 `ticks`，返回 `{变量名: 档位文本元组}`。

    供 `agent.prompt.build_prompt` / `agent.validate` 等下游调用方一次性取得
    全部设计变量的档位文本，不需要各自遍历 `DesignVariables.model_fields`。
    """
    variables = constraints_cfg.design_space.variables
    return {
        name: expand_ticks(getattr(variables, name))
        for name in DesignVariables.model_fields
    }
