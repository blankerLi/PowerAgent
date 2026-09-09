"""poweragent/sim/hashing.py

模型依赖闭包解析与哈希（design.md §4.2 / §6.3.3，`tasks.md` T4 / 任务 3.1）。

- `resolve_dependency_closure()`：按 `model.yaml` 的 `model_package` 节列出的
  `.slx` 入口、引用模型、数据字典、MATLAB 函数目录、初始化脚本、MAT 输入与自定义库
  解析出依赖闭包的完整文件清单，不按主观重要性筛选（design.md §4.2 / 需求 R4.1）。
- `model_package_hash()`：对闭包全体**文件内容** + `model.yaml` 规范化内容计算
  sha256，不只对 `entry` 计算（design.md §4.2 / 需求 R4.2）。
- `fast_fingerprint()`：闭包条目的 `(size, mtime_ns)` 序列哈希，供 `sim.simulate()`
  每次调用前的低成本不变性检查；条目顺序与 `model_package_hash` 一致，使两者在
  "仅时间戳变化"与"内容变化"之间可比较、不等价（design.md §6.3.3 / 需求 R4.3）。

软依赖说明：本模块的哈希与规范化序列化复用 `poweragent.config.hashing.canonical_json`
（design.md §5.3.1，全文唯一登记处）。`config/hashing.py` 由任务 5.3 实现，与本任务
（3.1）并行派发。若 5.3 尚未落地，本模块的 import 会失败——这是预期的阶段性状态，
待 5.3 落地后即可正常工作；本模块不重复实现 `canonical_json` 或另立哈希规则。

任务 4.1（`averaged_model_required=true` 时合并闭包哈希覆盖 switching 与 averaged）
的接线已在本模块完整落地，不按变体拆分哈希：`resolve_dependency_closure()` 在该标志
为真时把 averaged 变体的路径追加到 switching 变体之后、返回同一个合并序列；
`model_package_hash()` 不关心闭包由几个变体构成，只对传入的 `closure_paths` 整体
逐一取内容摘要——调用方只需先调 `resolve_dependency_closure()` 取得（已按需合并的）
闭包，再把返回值原样传给 `model_package_hash()`，两者之间不需要任何额外的按变体分支
代码。`dual_model_consistency.checkpoints` 的配置校验落在
`poweragent.config.dual_model_consistency`（与 `config/schema.py` 的 pydantic 模型
并行排期，见该模块顶部说明）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from poweragent.config.hashing import canonical_json

# model.yaml 的 model_package.<variant> 节内，构成依赖闭包的字段名（design.md §4.2 示例）：
#   model_package:
#     switching:
#       entry: models/buck_12ph.slx
#       referenced_models: [models/ref_ctrl.slx]
#       data_dictionaries: [models/buck.sldd]
#       matlab_functions: [matlab/+pa]
#       init_scripts: [models/init_buck.m]
#       mat_inputs: [data/load_profile.mat]
#       custom_libraries: [libs/pwr_lib.slx]
#     averaged: { ... 同构，averaged_model_required=false 时留空 }
#   averaged_model_required: <bool>
_ENTRY_FIELD = "entry"
_LIST_FIELDS = (
    "referenced_models",
    "data_dictionaries",
    "matlab_functions",
    "init_scripts",
    "mat_inputs",
    "custom_libraries",
)
# 该字段声明的是目录（MATLAB 函数目录），闭包成员是该目录下的全部文件而非目录本身，
# 因此新增/删除/重命名目录下文件即改变闭包成员集合（design.md §4.2 / 需求 R4.3）。
_DIRECTORY_FIELD = "matlab_functions"


def _variant_paths(variant_section: Mapping[str, Any], *, base_dir: Path) -> list[Path]:
    """展开单个模型变体（switching 或 averaged）节内声明的全部路径。

    顺序确定性：`entry` 在前，随后按 `_LIST_FIELDS` 的声明顺序逐字段展开，字段内部
    按 YAML 列表中出现的顺序；`matlab_functions` 目录下的文件按相对路径的字典序展开，
    使同一目录内容在不同文件系统遍历顺序下仍得到相同的闭包顺序。
    """
    paths: list[Path] = []

    entry = variant_section.get(_ENTRY_FIELD)
    if entry:
        paths.append(base_dir / entry)

    for field in _LIST_FIELDS:
        for rel in variant_section.get(field) or ():
            candidate = base_dir / rel
            if field == _DIRECTORY_FIELD and candidate.is_dir():
                paths.extend(
                    sorted(
                        (p for p in candidate.rglob("*") if p.is_file()),
                        key=lambda p: p.relative_to(candidate).as_posix(),
                    )
                )
            else:
                paths.append(candidate)

    return paths


def resolve_dependency_closure(
    model_config: Mapping[str, Any],
    *,
    base_dir: Path | str = ".",
) -> tuple[Path, ...]:
    """按 `model.yaml` 的 `model_package` 节解析依赖闭包清单。

    `model_config` 为 `model.yaml` 解析后的顶层映射（含 `model_package` 与
    `averaged_model_required` 两键）。始终解析 `model_package.switching`；
    `averaged_model_required` 为真时额外解析 `model_package.averaged` 并追加到同
    一序列（合并闭包，design.md §4.2）。`model_package_hash()` 只对本函数返回的
    序列整体取内容摘要，不按变体拆分——合并闭包对哈希计算是透明的，调用方无需
    额外接线（任务 4.1）。

    不对闭包条目做任何"重要性"筛选：`model.yaml` 列出的条目全部计入，
    `matlab_functions` 声明的目录展开为该目录下全部文件。返回的路径顺序是
    `model_package_hash()` 与 `fast_fingerprint()` 共用的唯一确定性顺序。
    """
    base = Path(base_dir)
    model_package = model_config.get("model_package") or {}

    paths: list[Path] = list(
        _variant_paths(model_package.get("switching") or {}, base_dir=base)
    )

    if model_config.get("averaged_model_required"):
        paths.extend(_variant_paths(model_package.get("averaged") or {}, base_dir=base))

    return tuple(paths)


def model_package_hash(
    closure_paths: Sequence[Path],
    model_config: Mapping[str, Any],
) -> str:
    """对闭包全体文件内容 + `model.yaml` 规范化内容计算 sha256（design.md §4.2）。

    不只对 `entry` 计算：`closure_paths` 中每个文件的字节内容按给定顺序逐一摘要
    （该顺序应与 `fast_fingerprint()` 使用的顺序一致，通常即 `resolve_dependency_closure()`
    的返回值），随后把 `model_config` 经 `canonical_json` 规范化后的文本一并摘要。
    返回不截断的小写十六进制摘要（design.md §5.3.1）。
    """
    digest = hashlib.sha256()
    for path in closure_paths:
        digest.update(Path(path).read_bytes())
    digest.update(canonical_json(model_config).encode("utf-8"))
    return digest.hexdigest()


def fast_fingerprint(closure_paths: Sequence[Path]) -> str:
    """闭包条目的 `(size, mtime_ns)` 序列哈希，供 `sim.simulate()` 每次调用前的低成本
    不变性检查（design.md §6.3.3）。

    条目顺序须与 `model_package_hash()` 使用的顺序一致（同一个 `closure_paths` 序列），
    使"仅时间戳变化"（本函数结果变化）与"内容或成员集合变化"（`model_package_hash()`
    结果变化）互不等价：闭包文件内容与成员集合不变时，即使时间戳变化，
    `model_package_hash()` 的结果仍保持不变。
    """
    stats = [[Path(p).stat().st_size, Path(p).stat().st_mtime_ns] for p in closure_paths]
    return hashlib.sha256(canonical_json(stats).encode("utf-8")).hexdigest()
