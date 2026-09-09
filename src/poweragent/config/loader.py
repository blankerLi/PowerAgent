"""poweragent/config/loader.py

四个配置文件（`task.yaml` / `model.yaml` / `metrics.yaml` / `constraints.yaml`）的
加载入口 `load_all()` 与不合规报告（design.md §4.2 / §6.2.2 附近的 "config" 模块
职责；tasks.md 任务 5.2；需求追溯 R3.2, R2.4）。

本模块的范围
------------
只做两件事：

1. **占位符预检（pre-flight）**：在把原始 YAML 数据喂给 `config/schema.py` 的
   pydantic 模型之前，逐字段递归扫描全部字段（不限于「必填」），命中字面量字符串
   `"<...>"` 即记一条 `config_incomplete: <文件名>.<字段路径>` 问题，且**不再继续
   构造该文件对应的 pydantic 模型**（schema.py 模块docstring 与 R2.4 的约定）。
   这一步先于 pydantic 构造，是为了避免占位符触发一个令人困惑的 pydantic 类型
   校验错误（例如 `"<...>"` 塞进 `int` 字段会报 `int_parsing` 而不是
   `config_incomplete`）。
2. **四类不合规的逐条报告**：结构（`structural`）、字段类型（`field_type`）、
   `unit` 与声明不一致（`unit_mismatch`）、数值越界（`out_of_range`）。四类均
   逐条给出「文件名 + 嵌套键序列」的完整字段路径与类别，**不返回配置模型**（R3.2）。

必填字段留空的落点（`config_incomplete` 与 pydantic 错误的边界）
------------------------------------------------------------------
「必填字段为空 / `null` / 仍为 `<...>` 占位形式」三种情形统一落 `config_incomplete`
类别，不落 `field_type`：

- `"<...>"` 占位形式：由上述预检直接捕获，不进入 pydantic 构造。
- 必填字段完全缺失或显式为 `null`：pydantic 报 `missing` 错误，或对不接受 `None`
  的字段报「输入为 `None`」的类型错误（如 `string_type` 且 `input is None`）——
  本模块把这两类 pydantic 错误重新归类为 `config_incomplete`（见
  `_classify_pydantic_error`），而不是当作普通的字段类型错误。
- 必填字符串被留空（`""`）：pydantic 报 `string_too_short`（`min_length=1`）且
  `input == ""`，同样归为 `config_incomplete`。

**允许为 `null` 且不判为 `config_incomplete` 的字段**（如
`model.yaml` 的 `baseline.measured_evidence_ref`）在 schema.py 中已声明为
`X | None = None`，pydantic 对显式 `null` 不会报错，因此不会产生任何问题——这与
schema.py 的既有约定一致，本模块不需要为它们特殊处理。

本模块不做的事（按任务边界划给其他任务）
----------------------------------------
- `ticks` 的展开与档位字面量规范化 —— 归任务 5.9。
- `canonical_json` / 各类哈希 —— 归任务 5.3（`config/hashing.py`）。
- **不为任何缺失或不合规字段填默认值**：全部问题原样报告，不做值替换或推断
  （R2.4、R3.2 的显式要求）。

跨文件一致性校验（任务 5.8）的调用时机
----------------------------------------
`divergence_guard` vs `hard_constraints`、`ticks` 等比、`tick_match_rel_tol` 与
`tick_ratio` 的关系三项断言实现在 `config/schema.py` 的
`check_cross_file_consistency()`（任务 5.8）。跨文件断言只有在四份数据同时在手
才可能进行，因此 `load_all()` 在四个配置文件**全部**成功构造为 pydantic 模型
之后调用该函数一次；若该函数抛出 `CrossFileConsistencyError`，`load_all()`
把它转译为一条 `structural` 类别的 `ConfigIssue` 并抛出 `ConfigLoadError`
（与单文件不合规时的报告路径一致，仍不返回任何配置模型）。

API 设计
--------
```
load_all(config_dir: Path) -> ConfigBundle
```

- 四个文件全部合规：返回 `ConfigBundle`（四个已构造的 pydantic 模型实例：
  `task` / `model` / `metrics` / `constraints`）。
- 任一文件存在任何问题（含 `config_incomplete`）：抛出 `ConfigLoadError`，其
  `.issues` 属性为 `tuple[ConfigIssue, ...]`，逐条给出文件名、完整字段路径、
  类别与可读消息。**只要有一条问题，四个模型都不构造、不返回**——即使其余三个
  文件本身是合规的（R3.2「不返回配置模型」的字面要求）。

选用「抛异常」而非「返回 `(bundle, issues)` 元组」的理由：R3.2 的措辞是
「THE `config.loader` SHALL **抛出**校验错误」，异常携带 `.issues` 同时满足
「逐条列出」的要求；调用方（`controller.preflight`，任务 14.2）可以直接
`try: load_all(...) except ConfigLoadError as exc: ...` 后把 `exc.issues` 转译
为 `PreflightError` 消息，不需要在每次调用后手动判断成功/失败。

四个文件名固定为 `task.yaml`、`model.yaml`、`metrics.yaml`、`constraints.yaml`
（`configs/task_template.yaml` 是模板文件本身的固定名字，供工程师复制为
`task.yaml` 使用；本模块不处理模板到任务文件的复制，只按上述四个固定文件名从
`config_dir` 读取）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import yaml
from pydantic import BaseModel, ValidationError

from poweragent.config.schema import (
    ConstraintsConfig,
    CrossFileConsistencyError,
    MetricsConfig,
    ModelConfig,
    TaskConfig,
    check_cross_file_consistency,
)

# 占位符字面量：模板文件中待 Owner 在 M0 填写的字段一律取该字符串（configs/
# task_template.yaml 等四个模板文件的实际用法）。
PLACEHOLDER = "<...>"

IssueCategory = Literal[
    "structural", "field_type", "unit_mismatch", "out_of_range", "config_incomplete"
]

# 四个配置文件的固定文件名与对应 pydantic 模型（design.md §4；schema.py 任务 5.1）。
_CONFIG_MODELS: Mapping[str, type[BaseModel]] = {
    "task.yaml": TaskConfig,
    "model.yaml": ModelConfig,
    "metrics.yaml": MetricsConfig,
    "constraints.yaml": ConstraintsConfig,
}

# pydantic 内置的数值闭区间约束错误类型（Field(ge=/gt=/le=/lt=) 触发），归为
# out_of_range；不含 "missing" 等与「留空」相关的类型（那些归 config_incomplete，
# 见 _classify_pydantic_error）。
_OUT_OF_RANGE_ERROR_TYPES = frozenset(
    {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "finite_number",
    }
)

# 「输入为 None 但字段类型不接受 None」时 pydantic 报出的错误类型集合；配合
# `input is None` 一起判定为「必填字段被显式置空」，归 config_incomplete。
_NONE_INPUT_ERROR_TYPES = frozenset(
    {
        "string_type",
        "int_type",
        "float_type",
        "bool_type",
        "dict_type",
        "list_type",
        "literal_error",
        "value_error",
        "date_type",
        "tuple_type",
    }
)


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """单条不合规问题：文件名 + 完整字段路径（嵌套键序列）+ 类别 + 可读消息。"""

    file: str
    field_path: str
    category: IssueCategory
    message: str


@dataclass(frozen=True, slots=True)
class ConfigBundle:
    """四个配置文件全部合规时的返回值：四个已构造的 pydantic 模型实例。"""

    task: TaskConfig
    model: ModelConfig
    metrics: MetricsConfig
    constraints: ConstraintsConfig


class ConfigLoadError(Exception):
    """`load_all()` 在任一配置文件存在不合规问题时抛出（R3.2, R2.4）。

    `issues` 携带全部不合规问题的完整列表（可能跨多个文件）。抛出本异常时，
    四个配置模型均不构造、不返回。
    """

    def __init__(self, issues: Sequence[ConfigIssue]) -> None:
        self.issues: tuple[ConfigIssue, ...] = tuple(issues)
        summary = "; ".join(issue.message for issue in self.issues)
        super().__init__(f"配置加载失败，共 {len(self.issues)} 项不合规: {summary}")


# ---------------------------------------------------------------------------
# 原始 YAML 加载与结构性检查
# ---------------------------------------------------------------------------


def _load_raw_yaml(path: Path, file: str) -> tuple[Any, list[ConfigIssue]]:
    """读取并解析单个 YAML 文件；结构性问题（不存在 / 解析失败 / 顶层非映射）
    直接返回，不再继续。"""

    if not path.is_file():
        return None, [
            ConfigIssue(
                file=file,
                field_path="<root>",
                category="structural",
                message=f"{file}: 文件不存在（期望路径: {path}）",
            )
        ]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [
            ConfigIssue(
                file=file,
                field_path="<root>",
                category="structural",
                message=f"{file}: 读取失败: {exc}",
            )
        ]

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [
            ConfigIssue(
                file=file,
                field_path="<root>",
                category="structural",
                message=f"{file}: YAML 解析失败: {exc}",
            )
        ]

    if data is None:
        return None, [
            ConfigIssue(
                file=file,
                field_path="<root>",
                category="structural",
                message=f"{file}: 文件内容为空",
            )
        ]

    if not isinstance(data, Mapping):
        return None, [
            ConfigIssue(
                file=file,
                field_path="<root>",
                category="structural",
                message=(
                    f"{file}: 顶层结构必须是映射（mapping），"
                    f"实际为 {type(data).__name__}"
                ),
            )
        ]

    return data, []


# ---------------------------------------------------------------------------
# 占位符预检
# ---------------------------------------------------------------------------


def _join_path(path: Sequence[str]) -> str:
    """把路径段序列拼成人类可读的点号路径；整数索引段（形如 "[0]"）直接贴在
    前一段之后，不额外加点。"""

    joined = ""
    for segment in path:
        if segment.startswith("["):
            joined += segment
        elif joined:
            joined += f".{segment}"
        else:
            joined = segment
    return joined


def _scan_placeholders(file: str, node: Any, path: list[str]) -> list[ConfigIssue]:
    """递归扫描原始 YAML 数据，命中字面量字符串 "<...>" 即记一条
    config_incomplete 问题。不区分该字段是否为 schema 意义上的「必填」——
    占位符本身就是「待 Owner 填写」的显式标记，出现即需报告（见模块 docstring）。
    """

    issues: list[ConfigIssue] = []

    if isinstance(node, str):
        if node == PLACEHOLDER:
            field_path = _join_path(path) or "<root>"
            issues.append(
                ConfigIssue(
                    file=file,
                    field_path=field_path,
                    category="config_incomplete",
                    message=f"config_incomplete: {file}.{field_path}",
                )
            )
        return issues

    if isinstance(node, Mapping):
        for key, value in node.items():
            issues.extend(_scan_placeholders(file, value, [*path, str(key)]))
        return issues

    if isinstance(node, list):
        for index, value in enumerate(node):
            issues.extend(_scan_placeholders(file, value, [*path, f"[{index}]"]))
        return issues

    return issues


# ---------------------------------------------------------------------------
# pydantic 校验错误 → 不合规类别 + 字段路径
# ---------------------------------------------------------------------------


def _format_field_path(loc: Sequence[Any]) -> str:
    parts: list[str] = []
    for segment in loc:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            parts.append(str(segment))
    return _join_path(parts)


def _classify_pydantic_error(error: Mapping[str, Any]) -> IssueCategory:
    """把 pydantic v2 的单条 ValidationError 错误归入四类之一（不含
    config_incomplete 的占位符情形，那部分已在预检阶段挡住；但必填字段的
    「留空/null」仍会在这里出现，归 config_incomplete，见模块 docstring）。"""

    loc = error["loc"]
    error_type = error["type"]
    last_segment = str(loc[-1]) if loc else ""
    input_value = error.get("input")

    # 必填字段完全缺失。
    if error_type == "missing":
        return "config_incomplete"

    # 字段被显式置空（null）而类型不允许 None：视为「填了 null」的留空情形。
    if input_value is None and error_type in _NONE_INPUT_ERROR_TYPES:
        return "config_incomplete"

    # 必填字符串被留空（""）。
    if error_type == "string_too_short" and input_value == "":
        return "config_incomplete"

    # unit 字段的 Literal 校验失败：声明的单位字符串与 schema 不逐字符相同。
    if last_segment == "unit" and error_type in {"literal_error", "enum", "string_type"}:
        return "unit_mismatch"

    # pydantic 内置数值闭区间约束。
    if error_type in _OUT_OF_RANGE_ERROR_TYPES:
        return "out_of_range"

    # 自定义 model_validator 抛出的 ValueError：按消息关键字进一步归类。
    if error_type == "value_error":
        message = str(error.get("msg", ""))
        if any(kw in message for kw in ("不得大于", "必须严格小于", "上限", "下限", "越界")):
            return "out_of_range"
        if "unit" in message.lower() or "单位" in message:
            return "unit_mismatch"
        return "field_type"

    return "field_type"


# ---------------------------------------------------------------------------
# 单文件加载
# ---------------------------------------------------------------------------


def _load_one(
    path: Path, file: str, model_cls: type[BaseModel]
) -> tuple[BaseModel | None, list[ConfigIssue]]:
    data, structural_issues = _load_raw_yaml(path, file)
    if structural_issues:
        return None, structural_issues

    placeholder_issues = _scan_placeholders(file, data, [])
    if placeholder_issues:
        # 占位符预检失败：不再继续构造该文件的 pydantic 模型（R2.4）。
        return None, placeholder_issues

    try:
        model = model_cls.model_validate(data)
    except ValidationError as exc:
        issues: list[ConfigIssue] = []
        for error in exc.errors():
            category = _classify_pydantic_error(error)
            field_path = _format_field_path(error["loc"]) or "<root>"
            issues.append(
                ConfigIssue(
                    file=file,
                    field_path=field_path,
                    category=category,
                    message=f"{category}: {file}.{field_path}: {error['msg']}",
                )
            )
        return None, issues

    return model, []


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_all(config_dir: Path | str) -> ConfigBundle:
    """加载 `config_dir` 下的四个固定命名配置文件（`task.yaml` / `model.yaml` /
    `metrics.yaml` / `constraints.yaml`）。

    全部合规时返回 `ConfigBundle`；任一文件存在任何问题（结构 / 字段类型 /
    unit 不一致 / 数值越界 / 必填留空或占位符）时抛出 `ConfigLoadError`，
    `.issues` 携带全部问题，四个模型均不返回。

    不为任何缺失或不合规字段填默认值。
    """

    config_dir = Path(config_dir)
    all_issues: list[ConfigIssue] = []
    models: dict[str, BaseModel] = {}

    for file, model_cls in _CONFIG_MODELS.items():
        model, issues = _load_one(config_dir / file, file, model_cls)
        if issues:
            all_issues.extend(issues)
        else:
            models[file] = model  # type: ignore[assignment]

    if all_issues:
        raise ConfigLoadError(all_issues)

    bundle = ConfigBundle(
        task=models["task.yaml"],  # type: ignore[arg-type]
        model=models["model.yaml"],  # type: ignore[arg-type]
        metrics=models["metrics.yaml"],  # type: ignore[arg-type]
        constraints=models["constraints.yaml"],  # type: ignore[arg-type]
    )

    # 跨文件一致性断言（任务 5.8）：四个模型全部构造成功后才可能进行。
    # 失败时转译为一条 structural 问题并抛出 ConfigLoadError，与单文件不合规时
    # 的报告路径一致，不返回任何配置模型。
    try:
        check_cross_file_consistency(
            bundle.task, bundle.model, bundle.metrics, bundle.constraints
        )
    except CrossFileConsistencyError as exc:
        raise ConfigLoadError(
            [
                ConfigIssue(
                    file="<cross-file>",
                    field_path="<cross-file>",
                    category="structural",
                    message=str(exc),
                )
            ]
        ) from exc

    return bundle
