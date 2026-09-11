"""poweragent/sim/hashing.py

模型依赖闭包解析与哈希（design.md §4.2 / §6.3.3，`tasks.md` T4 / 任务 3.1）。

- `resolve_dependency_closure()`：按 `model.yaml` 的 `model_package` 节列出的
  `.slx` 入口、引用模型、数据字典、MATLAB 函数目录、初始化脚本、MAT 输入与自定义库
  解析出依赖闭包的完整文件清单，不按主观重要性筛选（design.md §4.2 / 需求 R4.1）。
- `model_package_hash()`：对闭包全体**文件内容** + `model.yaml` 规范化内容计算
  sha256，不只对 `entry` 计算（design.md §4.2 / 需求 R4.2）。`.slx` 成员取规范化
  字节而非原始字节——`.slx` 的原始字节对"用同一份输入重复生成"不确定，直接哈希会
  让门禁测的是"文件有没有被重新写过"而不是"模型是否真的变了"，见
  `_canonical_slx_bytes()` 的 docstring。
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
import re
import zipfile
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
# MATLAB 后端的入口（`.slx`），可选字段——见 `config/schema.py` 的
# `ModelVariantPackage` docstring 对两个入口字段的说明。它与 `entry` 一样是"被仿真
# 的对象"本身，因此同样计入闭包：`.slx` 由 `entry` 派生只是当前的工作方式，不是
# 文件系统强制的约束，不计入它则手工改动 `.slx` 不会使旧结论失效。
_SLX_ENTRY_FIELD = "slx_entry"
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


class SlxNotAnOpcPackageError(ValueError):
    """`.slx` 不是有效的 OPC(zip) 包时抛出。

    不静默回退到原始字节哈希：那会让"哈希对重复生成不确定"这个问题悄悄回来，
    而它的后果是门禁失去区分能力（见 `_canonical_slx_bytes()` 的说明）。
    """


# `.slx` 是 OPC(zip) 容器。其中一部分条目与模型定义无关，且**每次保存都会变**，
# 因此不计入模型包哈希。这份清单按"是不是被仿真的对象的一部分"划线，不是按
# "能不能让哈希稳定"划线：
#
#   metadata/*                       文件级属性：创建/修改时间、作者、文档 UUID、
#                                    Simulink 版本、缩略图 PNG。缩略图是从模型
#                                    **渲染**出来的产物而非模型定义——模型真变了，
#                                    systems/*.xml 必然先变；而 PNG 编码器的输出
#                                    不保证跨版本/跨平台确定。Simulink 版本本身
#                                    属于执行环境，已由 execution_env_hash 的
#                                    matlab_release 字段覆盖。
#   simulink/ScheduleCore.xml        Schedule Editor（任务调度编辑器）的状态。
#   simulink/ScheduleEditor.xml      本项目不使用该编辑器；这两个条目的内容是一张
#                                    纯 UUID 引用图，且实测其元素顺序在重复生成时
#                                    会在两种排列间交替。
#   simulink/windowsInfo.xml         模型窗口的位置与大小，纯界面状态。
#
# **新增的未知条目默认计入**（本清单是排除清单而非白名单）。这个方向是有意选的：
# Simulink 版本升级引入新条目时，行为是"可能误报"而不是"可能漏报"——误报会立刻
# 被人发现并来查这份清单，漏报不会。
_SLX_EXCLUDED_NAMES = frozenset({
    "simulink/ScheduleCore.xml",
    "simulink/ScheduleEditor.xml",
    "simulink/windowsInfo.xml",
})
_SLX_EXCLUDED_PREFIXES = ("metadata/",)

# 计入的条目里仍散布着与模型定义无关、每次保存都重新生成的标识：
#   simulink/blockdiagram.xml    ModelUUID 与每个块的 Field Name="UUID"
#   simulink/modelDictionary.xml System / Interface 的 uuid 属性
# 以及（若未来某个计入条目出现时间戳）ISO8601 时间戳。两者都替换为固定占位符。
# 占位符不含 `<` `>` `&`，替换后的内容仍是合法 XML，便于人工排查时直接打开看。
_UUID_PATTERN = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO8601_PATTERN = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")


def _canonical_slx_bytes(path: Path) -> bytes:
    """`.slx` 的规范化字节表示：只含模型定义，剔除每次保存都变的标识。

    ## 为什么不能直接对 `.slx` 取字节哈希

    实测（R2024a）：用**完全相同**的 spec 连续生成 4 份 `buck4ph_averaged.slx`，
    4 份的 sha256 各不相同，文件大小也在 98700~98704 字节之间抖动。差异散布在
    6 个 OPC 条目里，全部是与电路无关的东西——`dcterms:created` /
    `dcterms:modified` 时间戳、文档 UUID、`ModelUUID`、每个块的 `Field Name="UUID"`、
    以及 Schedule Editor 状态里的一张 UUID 引用图。

    这让 `model_package_hash` 测的不再是"模型是否真的变了"，而是"这个二进制文件
    最近有没有被重新写过"。后果不只是误报：门禁一旦频繁误报，人就会习惯性地
    "重跑一下记录"或"再审批一次"，那时真正的模型变更也会被同样地放过去。
    `configs/model.yaml` 的注释里已经把这条道理写在闭包边界的裁定上——
    「若把开发中的求解器代码计入闭包，每改一行都要重新走冻结审批，冻结机制会因为
    过度敏感而被绕过——那才是真正的失效」。字节哈希在这里犯的是同一个错误。

    ## 规范化后仍然敏感

    模型定义（块、参数、连线、求解器配置）落在 `simulink/systems/*.xml`、
    `simulink/blockdiagram.xml`、`simulink/configSet0.xml`、
    `simulink/graphicalInterface.xml` 等条目里，这些条目在重复生成时**本来就是
    确定的**——非确定性完全集中在被排除的那几个。实测规范化后：4 份产物哈希一致，
    而对 `system_root.xml` 里的增益值、块名、信号名，以及 `configSet0.xml` 里的
    求解器类型所做的改动全部被检出。`tests/unit/test_slx_hashing.py` 用构造的
    OPC 包守护这两条性质，不需要 MATLAB。

    ## 与两级不变性检查的关系

    `fast_fingerprint()` 用 `(size, mtime_ns)`，重新生成 `.slx` 必然改变它——这是
    对的，也是两级设计的意图：指纹不一致会升级为全量 `model_package_hash` 重算，
    而重算结果一致时 `sim/simulate.py` 的 `_check_model_not_mutated()` 判为
    "仅时间戳被触碰"的假警报并继续执行。修好本函数之后，"重新生成模型但一个参数
    都没改"落在这条假警报路径上；修好之前它落在 `model_mutated_during_optimize`
    的停止路径上。
    """
    try:
        with zipfile.ZipFile(path) as package:
            parts: list[bytes] = []
            # 按条目名排序：OPC 容器内的物理存储顺序不承载语义（实测重复生成时
            # 顺序稳定，但不依赖这一点）。
            for name in sorted(package.namelist()):
                if name in _SLX_EXCLUDED_NAMES or name.startswith(
                    _SLX_EXCLUDED_PREFIXES
                ):
                    continue
                content = package.read(name)
                content = _UUID_PATTERN.sub(b"UUID_NORMALIZED", content)
                content = _ISO8601_PATTERN.sub(b"TIMESTAMP_NORMALIZED", content)
                # 条目名与内容一并计入，且用 NUL 分隔：只哈希内容的话，把一个条目
                # 改名（模型结构确实变了）不会改变摘要。
                parts.append(name.encode("utf-8") + b"\x00" + content)
    except zipfile.BadZipFile as exc:
        raise SlxNotAnOpcPackageError(
            f"{path} 不是有效的 OPC(zip) 包，无法计算规范化哈希: {exc}"
        ) from exc

    return b"\x00".join(parts)


def _canonical_member_bytes(path: Path) -> bytes:
    """闭包成员用于哈希的字节表示。

    `.slx` 走 OPC 规范化（见 `_canonical_slx_bytes()`）；其余成员——`.yaml` 拓扑
    定义、`.m` 函数、`.mat` 输入——都是文本或已经确定的二进制，原样取字节。
    """
    if path.suffix.lower() == ".slx":
        return _canonical_slx_bytes(path)
    return path.read_bytes()


def _variant_paths(variant_section: Mapping[str, Any], *, base_dir: Path) -> list[Path]:
    """展开单个模型变体（switching 或 averaged）节内声明的全部路径。

    顺序确定性：`entry` 在前、`slx_entry`（存在时）紧随其后，随后按 `_LIST_FIELDS`
    的声明顺序逐字段展开，字段内部按 YAML 列表中出现的顺序；`matlab_functions`
    目录下的文件按相对路径的字典序展开，使同一目录内容在不同文件系统遍历顺序下仍
    得到相同的闭包顺序。

    `slx_entry` 缺失（未使用 MATLAB 后端的部署）时该位置不占任何条目，闭包序列与
    该字段引入之前逐条相同——因此只使用 Python 后端的环境不会因为这个字段的存在
    而得到不同的 `model_package_hash`。
    """
    paths: list[Path] = []

    entry = variant_section.get(_ENTRY_FIELD)
    if entry:
        paths.append(base_dir / entry)

    slx_entry = variant_section.get(_SLX_ENTRY_FIELD)
    if slx_entry:
        paths.append(base_dir / slx_entry)

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

    不只对 `entry` 计算：`closure_paths` 中每个文件按给定顺序逐一摘要（该顺序应与
    `fast_fingerprint()` 使用的顺序一致，通常即 `resolve_dependency_closure()` 的
    返回值），随后把 `model_config` 经 `canonical_json` 规范化后的文本一并摘要。
    返回不截断的小写十六进制摘要（design.md §5.3.1）。

    **`.slx` 成员取规范化字节而非原始字节**（`_canonical_member_bytes()`）。原因是
    `.slx` 的原始字节对"用同一份输入重复生成"不确定——实测同一 spec 连续生成 4 次
    得到 4 个不同的 sha256，差异全在文件元数据、文档 UUID、块 UUID 与编辑器状态里。
    直接哈希原始字节会让这个门禁测的是"文件最近有没有被重新写过"而不是"模型是否
    真的变了"，完整论证见 `_canonical_slx_bytes()` 的 docstring。其余成员原样取字节。
    """
    digest = hashlib.sha256()
    for path in closure_paths:
        digest.update(_canonical_member_bytes(Path(path)))
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
