"""poweragent/config/hashing.py

`design.md` §5.3.1「`canonical_json` 与哈希规则（全文唯一登记处）」的唯一实现
（`tasks.md` T3 / 任务 5.3）。本模块是全文对哈希与浮点格式化规则的唯一登记处，
其他模块（`sim/hashing.py`、`store/cache.py`、`agent/prompt.py`、`agent/validate.py`、
`retrieval/ingest.py`、`report/render.py` 等）只引用本模块的函数，不重复定义规则。

规则总览（design.md §5.3.1）：

- `canonical_json`：`sort_keys=True`、`separators=(",",":")`、`ensure_ascii=False`，
  浮点统一 `format(v, ".12g")`。
- 全部哈希算法固定为 **sha256**，返回不截断的小写十六进制（`candidate_id` 例外，
  显式截断前 16 字符）。
- 三种浮点格式化各有唯一用途，不可混用：档位字面量 `.12g`、原因串距离数值 `.4f`、
  报告数值 `.4g`。

实现策略说明：`json.dumps` 的浮点序列化由 C 编码器直接调用 `float.__repr__`，
不经过可自定义的钩子（`default` 只在遇到*不可*原生序列化的类型时触发，浮点数天生
可序列化，因此该钩子对浮点数无效）。为了让浮点数按 `.12g` 精确落地为 JSON 数值
（而不是被引号包裹的字符串），本模块不依赖 `json.dumps` 处理浮点，而是自行实现一个
最小的递归序列化器：字符串走 `json.dumps` 借其转义规则，容器（dict/list）与标量
（bool/int/float/None）由本模块直接拼接文本。这样每一个字节的输出都在本模块控制之
下，"sort_keys、最小分隔符、ensure_ascii=False、`.12g` 浮点" 四条规则同时精确成立，
不存在需要与 `json.dumps` 的内部浮点路径博弈的空间。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping as _MappingABC
from collections.abc import Sequence as _SequenceABC
from typing import Any, Mapping, Sequence

__all__ = [
    "canonical_json",
    "metrics_hash",
    "constraints_hash",
    "scenario_set_hash",
    "candidate_id",
    "context_hash",
    "normalize_evidence_text",
    "evidence_text_hash",
    "normalize_prompt_text",
    "format_tick_literal",
    "format_distance",
    "format_report_number",
    "result_hash",
]


# --------------------------------------------------------------------------
# canonical_json：自行控制每个字节的最小递归序列化器
# --------------------------------------------------------------------------

def _encode(obj: Any) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        # 必须先判 bool：bool 是 int 的子类，先判 int 会把 True/False 错序列化为 1/0
        return "true" if obj else "false"
    if isinstance(obj, float):
        return format(obj, ".12g")
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, str):
        # 复用 json.dumps 的字符串转义与 ensure_ascii=False 语义，不重新实现转义规则
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, _MappingABC):
        for k in obj.keys():
            if not isinstance(k, str):
                raise TypeError(
                    f"canonical_json: mapping key must be str, got {type(k)!r}"
                )
        items = sorted(obj.items(), key=lambda kv: kv[0])
        body = ",".join(f"{_encode(k)}:{_encode(v)}" for k, v in items)
        return "{" + body + "}"
    if isinstance(obj, _SequenceABC) and not isinstance(obj, (str, bytes, bytearray)):
        return "[" + ",".join(_encode(v) for v in obj) + "]"
    raise TypeError(f"canonical_json: unsupported type {type(obj)!r}")


def canonical_json(obj: Any) -> str:
    """`design.md` §5.3.1`：`sort_keys=True`、`separators=(",",":")`、
    `ensure_ascii=False`，浮点统一 `format(v, ".12g")`。

    `obj` 须为 JSON 兼容结构（`Mapping`、`Sequence`（`str`/`bytes` 除外）、
    `str`、`bool`、`int`、`float`、`None` 的任意嵌套）。字典键须为 `str`。
    """
    return _encode(obj)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 配置与场景集哈希（design.md §5.3.1 表格 + §5.3 上文）
# --------------------------------------------------------------------------

def metrics_hash(metrics_config: Mapping[str, Any]) -> str:
    """`metrics.yaml` 全部内容的 sha256（不截断小写十六进制）。"""
    return _sha256_hex(canonical_json(metrics_config))


def constraints_hash(constraints_config: Mapping[str, Any]) -> str:
    """`constraints.yaml` 全部内容的 sha256（不截断小写十六进制）。"""
    return _sha256_hex(canonical_json(constraints_config))


def scenario_set_hash(scenario_rows: Sequence[Mapping[str, Any]]) -> str:
    """写入 `scenario_set` 表的全部冻结场景行的 sha256（不截断小写十六进制）。

    `scenario_rows` 为按 `task.yaml` 行序排列的场景行序列（`task_id` / `scenario_id`
    / `tier` / `model_variant` / `require_margin` / `spec_json` / `spec_version`，
    对应 `store/ddl.sql` 的 `scenario_set` 表列）。
    """
    return _sha256_hex(canonical_json(list(scenario_rows)))


# --------------------------------------------------------------------------
# candidate_id / context_hash（design.md §5.3.1 表格）
# --------------------------------------------------------------------------

def candidate_id(parameters_si: Mapping[str, float]) -> str:
    """`sha256(canonical_json(parameters_si))[:16]`。

    登记表中唯一被截断的哈希，截断为 16 个十六进制字符。
    """
    return _sha256_hex(canonical_json(parameters_si))[:16]


def context_hash(state: Any) -> str:
    """`sha256(canonical_json(state))`（不截断小写十六进制）。"""
    return _sha256_hex(canonical_json(state))


# --------------------------------------------------------------------------
# evidence.text_hash 与文本规范化（design.md §5.3.1 表格）
# --------------------------------------------------------------------------

def normalize_evidence_text(text: str) -> str:
    """`evidence.text_hash` 所用的规范化：行尾统一 `\\n` + 去每行尾随空白 +
    连续空行折叠为一。

    供 `retrieval/ingest.py` 等其他模块在计算/比较 `text_hash` 前调用同一份
    规范化逻辑，不在别处重复实现。
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]

    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        blank = line == ""
        if blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = blank

    return "\n".join(collapsed)


def evidence_text_hash(text: str) -> str:
    """`sha256(normalized_text.encode("utf-8"))`（不截断小写十六进制）。"""
    return _sha256_hex(normalize_evidence_text(text))


# --------------------------------------------------------------------------
# prompt_text 换行规范化（design.md §5.3.1 表格）——注意比 evidence 规范化更窄：
# 只统一换行，不去尾随空白、不折叠空行。
# --------------------------------------------------------------------------

def normalize_prompt_text(text: str) -> str:
    """`prompt_text` 渲染后的换行规范化：统一为 `\\n`
    （`replace("\\r\\n","\\n").replace("\\r","\\n")`），保证跨平台 `prompt_hash` 一致。

    与 `normalize_evidence_text` 不同：不去除尾随空白、不折叠连续空行。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------
# 三种浮点格式化——各自唯一用途，不可混用（design.md §5.3.1 表格）
# --------------------------------------------------------------------------

def format_tick_literal(v: float) -> str:
    """档位/tick 字面量格式化：`format(v, ".12g")`。

    唯一用途：写入 prompt 的档位字面量与 `agent.validate` 的档位命中判定必须
    使用同一组十进制文本。不得用于距离数值或报告数值。
    """
    return format(v, ".12g")


def format_distance(d: float) -> str:
    """距离数值格式化：`format(d, ".4f")`。

    唯一用途：原因串 `low_novelty:<距离>` 与 `local_refine` 的近邻标注中的
    距离数值。不得用于档位字面量或报告数值。
    """
    return format(d, ".4f")


def format_report_number(v: float) -> str:
    """报告数值格式化：`format(v, ".4g")`。

    唯一用途：渲染报告中出现的数值，含占位段子集判定时两侧比较所用的字符串。
    不得用于档位字面量或原因串中的距离数值。
    """
    return format(v, ".4g")


# --------------------------------------------------------------------------
# result_hash（design.md §5.3.1「result_hash 的重算范围」）
# --------------------------------------------------------------------------

_RESULT_HASH_KEYS: tuple[str, ...] = (
    "candidate_id",
    "parameters_si",
    "worst_case",
    "per_scenario",
    "constraints",
    "model_package_hash",
    "constraints_hash",
    "metrics_hash",
    "scenario_set_hash",
)


def result_hash(payload: Mapping[str, Any]) -> str:
    """重算范围恰为 `{candidate_id, parameters_si, worst_case, per_scenario
    [按 (scenario_id, metric_id) 排序], constraints[按 scenario_id 排序],
    model_package_hash, constraints_hash, metrics_hash, scenario_set_hash}`。

    `runs.elapsed_ms` / `cache_hit` / `budget_units` 等过程量不在范围内
    （重跑同一候选不应作废审批）。

    这 9 个键**必须**全部存在，缺失任一个即抛 `KeyError`（暴露调用方缺字段的
    错误，而不是悄悄漏算）。若 `payload` 中还带有这 9 个键之外的其他键
    （例如误传入的 `elapsed_ms`），本函数**忽略**它们而不参与哈希——这是刻意
    的防御性选择：既保证 9 个必需键齐备，又保证过程量即便被误传入也不会污染
    `result_hash`。

    `per_scenario` 与 `constraints` 由本函数**内部排序**（不要求调用方预先排序），
    这样调用方不需要记住排序规则，排序本身也不会因遗忘而产生不稳定的哈希。
    """
    missing = [key for key in _RESULT_HASH_KEYS if key not in payload]
    if missing:
        raise KeyError(
            f"result_hash: missing required key(s): {', '.join(missing)}"
        )

    per_scenario = sorted(
        payload["per_scenario"],
        key=lambda row: (row["scenario_id"], row["metric_id"]),
    )
    constraints = sorted(
        payload["constraints"],
        key=lambda row: row["scenario_id"],
    )

    scoped = {
        "candidate_id": payload["candidate_id"],
        "parameters_si": payload["parameters_si"],
        "worst_case": payload["worst_case"],
        "per_scenario": per_scenario,
        "constraints": constraints,
        "model_package_hash": payload["model_package_hash"],
        "constraints_hash": payload["constraints_hash"],
        "metrics_hash": payload["metrics_hash"],
        "scenario_set_hash": payload["scenario_set_hash"],
    }
    return _sha256_hex(canonical_json(scoped))
