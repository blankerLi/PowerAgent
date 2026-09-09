"""poweragent/store/artifacts.py

产物原子落盘：`stage()` / `commit()`（design.md §6.9，`tasks.md` 任务 8.2；
需求 R14.5, R14.6, R14.11）。

## 目录布局

最终路径为 `<base>/<task_id>/<kind>/<name>`，`kind` 对应任务 8.5 定义的七个
子目录之一（`config` `waveforms` `metrics` `plots` `reports` `approvals` `llm`）
中的一个。本模块只按需创建它实际使用的那个 `<task_id>/<kind>/` 目录，不预先
创建全部七个子目录——那是任务 8.5（配置快照与目录初始化）的职责。

## `stage()`：临时文件与最终路径同目录

`stage()` 把临时文件命名为 `<final_dir>/.<name>.tmp`，与最终路径同一目录。
这从构造上保证了 `commit()` 里的原子移动源与目标必然同卷：不存在"跨卷导致
`os.replace` 退化为非原子的复制+删除"的可能性，因此不需要单独的临时目录、
不需要新增声明临时目录的配置项，也不需要在启动期做同卷断言——原子性由
`stage()` 的路径构造保证，不依赖任何运行期校验（design.md §0.5 判据 1）。

`stage()` 只计算并返回临时路径，不写入任何内容；调用方负责把产物字节写到
该路径，随后调用 `commit()`。

## `commit()`：往返校验语义（读法说明）

design.md 把 `commit()` 的校验步骤表述为"写临时文件 → 读回计算 sha256 并与
写入内容的 sha256 比对（往返校验）→ 原子移动 → 事务内提交数据库引用"。这段
表述存在两种可能的实现读法：

1. **调用方持有写入前的哈希，`commit()` 与之比较**（真正意义上的"独立复算
   并比对"）。
2. **`commit()` 自行从磁盘读回临时文件并计算哈希**——这个"读回"动作本身就
   是对写入是否完整、磁盘内容是否可读的往返校验。`ArtifactStore` 并不持有
   调用方内存中的原始字节，因此它能独立完成的往返校验就是"文件被写入后，
   读回同样的字节，用全文档唯一登记的算法（sha256，design.md §5.3.1）重新
   计算一次摘要"。

本实现以解读 2 为**主路径**（`commit(tmp)`，不传 `expected_sha256`）：读取
`tmp` 的当前磁盘内容并计算 sha256，构成"写入 → 读回 → 计算"的往返校验，并与
"产物内容哈希固定为 sha256"的唯一登记保持一致。同时提供可选参数
`expected_sha256`，供已在内存中算过写入前哈希的调用方传入，以实现读法 1 的
真正独立比对；不一致时按与"内容损坏"相同的失败路径处理（不移动、不返回
引用、删除临时文件、抛异常）。

## 引用字符串格式

`store/ddl.sql` 的 `runs.waveform_ref` 与 `runs.observable_ref` 均为 `TEXT` 列，
从命名与用途（供后续按引用读取产物文件）看是路径型字符串，不是内容哈希。
因此 `commit()` 返回**最终路径的字符串形式**作为引用，不是 sha256 摘要。

## 与 `store/repo.py`（任务 8.3）的分工

`ArtifactStore.commit()` 只做文件级的原子提交（校验 → 移动 → 返回引用字符串），
不在事务内写数据库行。"事务内提交数据库引用"是 `store/repo.py` 的职责：调用方
先调 `artifacts.commit()` 取得引用字符串，再在自己的 `Store.tx()` 上下文内把该
引用字符串写入 `runs` / `metric_results` 等表。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ArtifactCommitError(RuntimeError):
    """`commit()` 校验失败时抛出：临时文件长度为 0、不存在，或哈希比对不通过。

    抛出前临时文件已被删除；最终路径与既有数据库行不发生任何变化。
    """


class ArtifactStore:
    """产物原子落盘：`stage()` 计算路径，`commit()` 校验并原子移动。"""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    def stage(self, task_id: str, kind: str, name: str) -> Path:
        """返回临时路径 `<base>/<task_id>/<kind>/.<name>.tmp`，与最终路径同目录。

        确保 `<base>/<task_id>/<kind>/` 目录存在（按需创建，不预先创建其余六个
        子目录）。本方法只计算并返回路径，不写入任何内容——调用方写完产物字节后
        再调用 `commit()`。
        """
        final_dir = self._base_dir / task_id / kind
        final_dir.mkdir(parents=True, exist_ok=True)
        return final_dir / f".{name}.tmp"

    def commit(self, tmp: Path, *, expected_sha256: str | None = None) -> str:
        """校验 `tmp` 后原子移动到最终路径，返回最终路径的字符串引用。

        顺序：校验非空存在 → 读回计算 sha256（往返校验；`expected_sha256` 给出时
        额外与之比对）→ 原子移动 → 返回引用。任一校验环节失败：删除 `tmp`
        （若存在）、不移动、不返回引用、抛出 `ArtifactCommitError`，最终路径与
        既有数据库行不发生任何变化。
        """
        tmp = Path(tmp)

        if not tmp.exists() or tmp.stat().st_size == 0:
            if tmp.exists():
                tmp.unlink()
            raise ArtifactCommitError(f"artifact tmp file missing or empty: {tmp}")

        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()

        if expected_sha256 is not None and digest != expected_sha256:
            tmp.unlink()
            raise ArtifactCommitError(
                f"artifact sha256 mismatch for {tmp}: "
                f"expected {expected_sha256}, got {digest}"
            )

        # tmp 由 stage() 构造为 f".{name}.tmp"，因此去掉首字符（'.'）与末 4 字符
        # （'.tmp'）即还原 name；最终路径与 tmp 同目录。
        final_path = tmp.with_name(tmp.name[1:-4])
        os.replace(tmp, final_path)
        return str(final_path)
