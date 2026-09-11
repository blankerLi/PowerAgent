"""`.slx` 规范化哈希：对重复生成稳定，对模型定义改动敏感。

## 这一层守护的是什么

`model_package_hash()` 的用途是门禁——"模型文件变了，旧的仿真结论就不能再用"。
它此前直接对 `.slx` 取原始字节哈希，而 `.slx` 的原始字节对"用同一份输入重复生成"
**不确定**：实测（R2024a）同一 spec 连续生成 4 份 `buck4ph_averaged.slx`，4 份的
sha256 各不相同、文件大小在 98700~98704 字节之间抖动，差异全部落在

    metadata/coreProperties.xml             dcterms:created / dcterms:modified
    metadata/mwcorePropertiesExtension.xml  文档 uuid
    simulink/blockdiagram.xml               ModelUUID + 每个块的 Field Name="UUID"
    simulink/modelDictionary.xml            System / Interface 的 uuid
    simulink/ScheduleCore.xml               UUID 引用图
    simulink/ScheduleEditor.xml             UUID 引用图（元素顺序还会在两种排列间交替）

这些与电路模型有没有变没有任何关系。后果不只是误报：门禁一旦频繁误报，人就会
习惯性地"重跑一下记录"，那时真正的模型变更也会被同样地放过去——过度敏感的门禁
等于没有门禁。

## 为什么用构造的 OPC 包而不是真实 `.slx`

这两条性质必须在**没有 MATLAB** 的环境里被守护，否则 CI 看不住它。`.slx` 是标准
OPC(zip) 容器，用 `zipfile` 就能构造出条目结构与真实产物一致的包——真实产物的
条目清单见下方 `_MINIMAL_SLX_ENTRIES` 的注释，取自实测。

对真实 `.slx` 的验证由 `tests/matlab/` 那一层承担（那里能真的重复生成）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from poweragent.sim.hashing import (
    SlxNotAnOpcPackageError,
    _canonical_slx_bytes,
    model_package_hash,
)

pytestmark = pytest.mark.unit


# 条目清单与内容形状取自实测的 buck4ph_averaged.slx（R2024a 生成）：
# 21 个条目，其中 metadata/thumbnail.png 是唯一的非 XML 条目。这里只保留与本测试
# 相关的条目，内容做了裁剪但保持了 UUID / 时间戳出现的位置。
_MINIMAL_SLX_ENTRIES: dict[str, bytes] = {
    # --- 模型定义：必须计入 ---
    "simulink/systems/system_root.xml": (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<System>\n'
        b'  <Block BlockType="Gain" Name="gm" SID="3">\n'
        b'    <P Name="Gain">0.0002</P>\n'
        b'  </Block>\n'
        b'  <Block BlockType="TransportDelay" Name="delay_td" SID="7">\n'
        b'    <P Name="DelayTime">2.5e-07</P>\n'
        b'  </Block>\n'
        b'  <Line><P Name="Src">3#out:1</P><P Name="Dst">7#in:1</P></Line>\n'
        b'</System>\n'
    ),
    "simulink/systems/system_10.xml": (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<System Name="compensator">\n'
        b'  <Block BlockType="Constant" Name="Rcomp" SID="10:1">\n'
        b'    <P Name="Value">12000</P>\n'
        b'  </Block>\n'
        b'  <Block BlockType="Constant" Name="Ccomp" SID="10:2">\n'
        b'    <P Name="Value">2.2000000000000001e-09</P>\n'
        b'  </Block>\n'
        b'</System>\n'
    ),
    "simulink/configSet0.xml": (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<ConfigSet>\n'
        b'  <P Name="Solver">ode23tb</P>\n'
        b'  <P Name="MaxStep">2e-07</P>\n'
        b'  <P Name="StopTime">0.001</P>\n'
        b'</ConfigSet>\n'
    ),
    "simulink/graphicalInterface.xml": (
        b'<?xml version="1.0"?><GraphicalInterface><NumRootInports>0'
        b'</NumRootInports></GraphicalInterface>\n'
    ),
    "[Content_Types].xml": b'<?xml version="1.0"?><Types/>\n',
    # --- 计入，但内部含每次都变的 UUID ---
    "simulink/blockdiagram.xml": (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<ModelInformation>\n'
        b'  <P Name="ModelUUID">a28fd677-c800-4c33-bc4e-09f8c3c3aab2</P>\n'
        b'  <P Name="SolverName">ode23tb</P>\n'
        b'  <Object>\n'
        b'    <Field Name="UUID" Class="char">cb744297-b7da-45f5-8721-bfa4aed3247b</Field>\n'
        b'  </Object>\n'
        b'</ModelInformation>\n'
    ),
    "simulink/modelDictionary.xml": (
        b'<?xml version="1.0"?>\n'
        b'<Dictionary>\n'
        b'  <System type="System" uuid="8a5372fd-e160-46ff-97b8-09e5c2698a40">\n'
        b'    <Interface type="Interface" uuid="4296ba33-49ee-4f47-bd31-04c53f2d4680"/>\n'
        b'  </System>\n'
        b'</Dictionary>\n'
    ),
    # --- 文件元数据与编辑器状态：必须排除 ---
    "metadata/coreProperties.xml": (
        b'<?xml version="1.0"?>\n'
        b'<coreProperties>\n'
        b'  <dcterms:created>2026-09-10T13:09:39Z</dcterms:created>\n'
        b'  <dcterms:modified>2026-09-10T13:09:50Z</dcterms:modified>\n'
        b'  <dc:creator>someone</dc:creator>\n'
        b'</coreProperties>\n'
    ),
    "metadata/mwcorePropertiesExtension.xml": (
        b'<?xml version="1.0"?><ext><uuid>0a1fd2c9-e7d9-4eb9-b54b-8d3b89a77803'
        b'</uuid></ext>\n'
    ),
    "metadata/thumbnail.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    "simulink/ScheduleCore.xml": (
        b'<?xml version="1.0"?>\n'
        b'<sltp_core>\n'
        b'  <Context uuid="132e8281-5e8a-428a-9dd1-354db9b45281"/>\n'
        b'</sltp_core>\n'
    ),
    "simulink/ScheduleEditor.xml": (
        b'<?xml version="1.0"?>\n'
        b'<sltp_editor>\n'
        b'  <Diagram uuid="9cda1831-9a06-4b23-ba04-9f73efeceffc"/>\n'
        b'  <Context uuid="9c1313ff-6a3a-44cf-a8b3-92c24ae3531c"/>\n'
        b'</sltp_editor>\n'
    ),
    "simulink/windowsInfo.xml": (
        b'<?xml version="1.0"?><WindowsInfo><Position>[10 10 800 600]</Position>'
        b'</WindowsInfo>\n'
    ),
}


def _write_slx(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, data in entries.items():
            package.writestr(name, data)
    return path


def _mutated(entries: dict[str, bytes], name: str, old: bytes, new: bytes) -> dict[str, bytes]:
    assert old in entries[name], f"{name} 中没有 {old!r}，测试资产与断言不匹配"
    return {**entries, name: entries[name].replace(old, new, 1)}


@pytest.fixture()
def baseline_slx(tmp_path: Path) -> Path:
    return _write_slx(tmp_path / "baseline.slx", _MINIMAL_SLX_ENTRIES)


def _digest(path: Path) -> bytes:
    return _canonical_slx_bytes(path)


# --------------------------------------------------------------------------
# 稳定性：与模型定义无关的部分改变时，摘要不变
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "old", "new", "what"),
    [
        (
            "metadata/coreProperties.xml",
            b"2026-09-10T13:09:39Z",
            b"2027-01-02T03:04:05Z",
            "创建时间戳",
        ),
        (
            "metadata/mwcorePropertiesExtension.xml",
            b"0a1fd2c9-e7d9-4eb9-b54b-8d3b89a77803",
            b"a672b015-0a95-45fb-b2a8-395f8172ff2f",
            "文档 UUID",
        ),
        (
            "simulink/ScheduleCore.xml",
            b"132e8281-5e8a-428a-9dd1-354db9b45281",
            b"2bb04499-111b-46f8-af4a-f295181efb67",
            "Schedule Editor 状态里的 UUID",
        ),
        (
            "simulink/windowsInfo.xml",
            b"[10 10 800 600]",
            b"[0 0 1920 1080]",
            "模型窗口位置",
        ),
        (
            "simulink/blockdiagram.xml",
            b"a28fd677-c800-4c33-bc4e-09f8c3c3aab2",
            b"db28523f-da31-4b2d-9c77-7695d163e2ba",
            "ModelUUID（在计入的条目里）",
        ),
        (
            "simulink/modelDictionary.xml",
            b"8a5372fd-e160-46ff-97b8-09e5c2698a40",
            b"a5ce6264-d408-4e76-937d-25b365217da5",
            "System uuid（在计入的条目里）",
        ),
    ],
)
def test_non_semantic_changes_do_not_change_digest(
    tmp_path: Path, baseline_slx: Path, entry: str, old: bytes, new: bytes, what: str
) -> None:
    """这些改动每次重新生成模型都会发生，与电路无关，不应触发门禁。"""
    other = _write_slx(
        tmp_path / "other.slx", _mutated(_MINIMAL_SLX_ENTRIES, entry, old, new)
    )
    assert _digest(baseline_slx) == _digest(other), f"{what} 改变时摘要不应变化"


def test_thumbnail_is_excluded(tmp_path: Path, baseline_slx: Path) -> None:
    """缩略图是从模型渲染出来的产物，不是模型定义。

    模型真的变了，`systems/*.xml` 必然先变；而 PNG 编码器的输出不保证跨版本、
    跨平台确定。把渲染产物计入门禁只会引入与电路无关的假警报。
    """
    entries = {**_MINIMAL_SLX_ENTRIES, "metadata/thumbnail.png": b"\x89PNG\r\n\x1a\n" + b"\xff" * 64}
    other = _write_slx(tmp_path / "other.slx", entries)
    assert _digest(baseline_slx) == _digest(other)


def test_entry_order_in_the_package_does_not_matter(
    tmp_path: Path, baseline_slx: Path
) -> None:
    """OPC 容器内条目的物理存储顺序不承载语义。"""
    reversed_entries = dict(reversed(list(_MINIMAL_SLX_ENTRIES.items())))
    other = _write_slx(tmp_path / "reordered.slx", reversed_entries)
    assert _digest(baseline_slx) == _digest(other)


# --------------------------------------------------------------------------
# 敏感性：模型定义变了，摘要必须变
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "old", "new", "what"),
    [
        ("simulink/systems/system_root.xml", b"0.0002", b"0.0003", "gm 增益值"),
        ("simulink/systems/system_root.xml", b"2.5e-07", b"5e-07", "采样保持延迟"),
        ("simulink/systems/system_root.xml", b"delay_td", b"delay_tdX", "块名"),
        ("simulink/systems/system_root.xml", b"7#in:1", b"7#in:2", "连线目标端口"),
        ("simulink/systems/system_10.xml", b"12000", b"12001", "Rcomp 的注入默认值"),
        ("simulink/systems/system_10.xml", b"Rcomp", b"RcompX", "注入点的块名"),
        ("simulink/configSet0.xml", b"ode23tb", b"ode45", "求解器类型"),
        ("simulink/configSet0.xml", b"2e-07", b"2e-08", "求解器最大步长"),
        ("simulink/blockdiagram.xml", b"ode23tb", b"ode45", "模型级求解器名"),
    ],
)
def test_semantic_changes_change_digest(
    tmp_path: Path, baseline_slx: Path, entry: str, old: bytes, new: bytes, what: str
) -> None:
    """规范化不能把这些改动一起抹掉——那才是门禁真正要抓的东西。"""
    other = _write_slx(
        tmp_path / "other.slx", _mutated(_MINIMAL_SLX_ENTRIES, entry, old, new)
    )
    assert _digest(baseline_slx) != _digest(other), f"{what} 改变时摘要必须变化"


def test_adding_or_removing_an_entry_changes_digest(
    tmp_path: Path, baseline_slx: Path
) -> None:
    """闭包成员集合的变化必须被检出（新增子系统、删掉配置集等）。"""
    added = _write_slx(
        tmp_path / "added.slx",
        {**_MINIMAL_SLX_ENTRIES, "simulink/systems/system_20.xml": b"<System/>"},
    )
    assert _digest(baseline_slx) != _digest(added)

    without = dict(_MINIMAL_SLX_ENTRIES)
    del without["simulink/systems/system_10.xml"]
    removed = _write_slx(tmp_path / "removed.slx", without)
    assert _digest(baseline_slx) != _digest(removed)


def test_renaming_an_entry_changes_digest(tmp_path: Path, baseline_slx: Path) -> None:
    """条目名与内容一并计入：改名（内容不动）也是结构变化。

    只哈希内容的话，把 `system_10.xml` 改名为 `system_11.xml` 不会改变摘要，而
    OPC 关系文件里对它的引用已经不同了。
    """
    renamed = {
        ("simulink/systems/system_11.xml" if k == "simulink/systems/system_10.xml" else k): v
        for k, v in _MINIMAL_SLX_ENTRIES.items()
    }
    other = _write_slx(tmp_path / "renamed.slx", renamed)
    assert _digest(baseline_slx) != _digest(other)


# --------------------------------------------------------------------------
# 与 model_package_hash 的接线
# --------------------------------------------------------------------------


def test_model_package_hash_uses_canonical_slx_bytes(tmp_path: Path) -> None:
    """`model_package_hash()` 对 `.slx` 走规范化，对其他成员走原始字节。"""
    model_config = {"model_package": {"switching": {"entry": "m.slx"}}}

    a = _write_slx(tmp_path / "a.slx", _MINIMAL_SLX_ENTRIES)
    b = _write_slx(
        tmp_path / "b.slx",
        _mutated(
            _MINIMAL_SLX_ENTRIES,
            "metadata/coreProperties.xml",
            b"2026-09-10T13:09:39Z",
            b"2027-01-02T03:04:05Z",
        ),
    )
    assert model_package_hash([a], model_config) == model_package_hash([b], model_config)

    c = _write_slx(
        tmp_path / "c.slx",
        _mutated(_MINIMAL_SLX_ENTRIES, "simulink/configSet0.xml", b"ode23tb", b"ode45"),
    )
    assert model_package_hash([a], model_config) != model_package_hash([c], model_config)

    # 非 .slx 成员仍按原始字节：一个字符的改动必须改变摘要。
    yaml_a = tmp_path / "spec.yaml"
    yaml_a.write_text("cout_f: 6.0e-3\n", encoding="utf-8")
    before = model_package_hash([yaml_a], model_config)
    yaml_a.write_text("cout_f: 6.0e-4\n", encoding="utf-8")
    assert before != model_package_hash([yaml_a], model_config)


def test_non_opc_slx_is_rejected_not_silently_hashed(tmp_path: Path) -> None:
    """损坏或不是 zip 的 `.slx` 显式失败，不静默回退到原始字节哈希。

    回退会让"哈希对重复生成不确定"这个问题悄悄回来，而它的表现是门禁看起来在工作、
    实际已经失去区分能力。宁可在这里明确失败。
    """
    broken = tmp_path / "broken.slx"
    broken.write_bytes(b"this is not a zip archive")

    with pytest.raises(SlxNotAnOpcPackageError):
        _canonical_slx_bytes(broken)

    with pytest.raises(SlxNotAnOpcPackageError):
        model_package_hash([broken], {})
