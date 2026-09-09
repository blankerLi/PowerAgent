"""poweragent/store/layout.py

产物目录布局初始化与配置快照（design.md §2.2「运行期产物」/ §16 T6；`tasks.md`
任务 8.5；需求追溯 R14.8）。

## 本模块的职责边界

任务 8.2（`store/artifacts.py`）的 `ArtifactStore.stage()` 已经能在被调用时按需
创建它当次使用的那一个 `<task_id>/<kind>/` 目录，并把该模块的职责显式限定为
"只按需创建它实际使用的那个目录，不预先创建全部七个子目录"（见
`artifacts.py` 模块 docstring），把"预先创建全部七个子目录"与"配置快照"两件事
留给本任务。本模块正是这两件事的落地：

1. `init_task_artifact_layout()`：在任务开始时一次性把 `config` `waveforms`
   `metrics` `plots` `reports` `approvals` `llm` 七个子目录全部创建好，即使
   `ArtifactStore.stage()` 会在首次使用时按需创建它们。这样做的价值是让任务
   刚创建时人工或工具检视 `artifacts/<task_id>/` 就能看到完整的七个子目录骨架，
   而不是一个随着任务推进逐渐"长出"目录、结构长期不完整的产物树。
2. `snapshot_configs()`：把四个已加载配置文件的原始 YAML 快照 + 各自的
   "快照完整性哈希"落到 `config/` 子目录，供事后核验产物没有被篡改。

## 为何是新模块而不是扩进 `artifacts.py`

`artifacts.py` 的模块 docstring 已经明确把"预先创建全部七个子目录"与"配置快照"
划给"任务 8.5"，且该文件当前的职责是纯粹的"路径构造 + 原子提交"机制
（`stage()` / `commit()`），不含任何"知道有七个子目录、知道配置文件长什么样"
的业务知识。本模块依赖 `config.loader.ConfigBundle`（四个已构造的配置模型）与
`config.hashing.canonical_json`，这些是 `artifacts.py` 故意不感知的更高层概念。
把它们放进新模块，`artifacts.py` 保持对"目录布局""配置内容"零感知，边界更清晰；
`store/layout.py` 反过来只调用 `ArtifactStore` 的公开接口（`stage()` /
`commit()`），不绕过其原子提交机制。

## 七个子目录清单的唯一来源

`SEVEN_SUBDIRS` 是本模块对 design.md §2.2 运行期产物树（`config` `waveforms`
`metrics` `plots` `reports` `approvals` `llm`）与 `tasks.md` 任务 8.5 描述的
唯一落地登记；`ArtifactStore.stage()` 的 `kind` 实参在实践中只应取自这七个
取值之一（`stage()` 本身不做取值校验，仍接受任意 `kind` 字符串——这是任务 8.2
既有行为，本模块不改变它）。

## "四个配置文件快照与各自哈希"的哈希口径澄清（关键设计决策）

design.md 的哈希登记表（§5.1 `tasks` 表列）只定义了 `model_package_hash`
（`sim/hashing.py`，覆盖模型依赖闭包全体文件 + `model.yaml` 规范化内容，范围
远大于"仅 `model.yaml` 自身"）、`metrics_hash`、`constraints_hash`
（两者均为 `config/hashing.py` 对应文件全部内容的 sha256）、`scenario_set_hash`
`execution_env_hash` `calibration_hash`。**这个登记表里不存在一个名为
`task_hash` 的、专门覆盖 `task.yaml` 自身内容的值**——`task.yaml` 的内容目前
不作为任何单独的哈希列出现在任何表或缓存键定义里。

因此，任务 8.5 描述中"四个配置文件快照与各自哈希"里"`task.yaml` 的哈希"没有
现成的登记值可以复用。本模块的选择是：对四个文件**统一**采用同一种"快照
完整性哈希"定义——`sha256(canonical_json(bundle.<对应模型>.model_dump()))`
（`task.yaml` → `bundle.task`，`model.yaml` → `bundle.model`，`metrics.yaml`
→ `bundle.metrics`，`constraints.yaml` → `bundle.constraints`）——而不是对
`model.yaml`/`metrics.yaml`/`constraints.yaml` 三个文件复用
`sim.hashing.model_package_hash()` / `config.hashing.metrics_hash()` /
`config.hashing.constraints_hash()` 这三个已在别处承担"缓存键 / 冻结点"职责
的注册哈希。理由：

- 这四个"快照哈希"的**唯一用途**是让事后核验者能够重算并比对，确认
  `config/` 下的快照文件没有被篡改——这是一个关于"这份快照文件的内容"的
  局部完整性属性，不需要、也不应该与 `simulation_key` / `evaluation_key` /
  `freezes` 表等别处消费的注册哈希共用同一个值。
- `model_package_hash()` 的范围明确**大于** `model.yaml` 自身：它还覆盖模型
  依赖闭包（`.slx` 入口、引用模型、数据字典等）的全部文件内容。如果
  `config/` 下 `model.yaml` 快照旁边的"哈希"文件写的是 `model_package_hash`，
  核验者拿着快照文件本身重算 `sha256(canonical_json(model.yaml 内容))` 会
  发现与登记值不一致——这不是篡改，而是两个哈希的范围原本就不同，容易造成
  误解。四个快照统一使用"仅覆盖该文件自身模型内容"的窄口径哈希，核验语义
  才是自洽的：「这个哈希就是这份快照文件内容的哈希，不多不少」。
- 对 `task.yaml` 而言，即使日后设计决定引入一个正式的 `task_hash` 注册值，
  本模块此刻使用的 ad-hoc 哈希在语义上仍然正确（它就是"这份快照文件内容的
  哈希"），不会与未来的注册值产生冲突——因为它们本来就是两个不同用途的值，
  只是恰好都可能叫"task 的哈希"。

**因此本模块产出的 `snapshot_hashes.json` 中的四个值，不等同于、也不应被
误认为是** `tasks` 表的 `model_package_hash` / `metrics_hash` /
`constraints_hash` 列，或 `store/cache.py` 缓存键计算中使用的同名值。那些
值分别由 `sim/hashing.py`（任务 3.1）与 `config/hashing.py`（任务 5.3）计算，
本模块不重复计算、不覆盖、不替代。

`canonical_json` 复用 `config.hashing.canonical_json`（design.md §5.3.1
全文唯一登记处），本模块不重新实现 JSON 规范化。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from poweragent.config.hashing import canonical_json
from poweragent.config.loader import ConfigBundle
from poweragent.store.artifacts import ArtifactStore

# design.md §2.2 运行期产物树 / tasks.md 任务 8.5 的唯一登记：七个子目录名。
SEVEN_SUBDIRS: tuple[str, ...] = (
    "config",
    "waveforms",
    "metrics",
    "plots",
    "reports",
    "approvals",
    "llm",
)

# config_dir 下四个固定文件名 → ConfigBundle 对应字段名（与
# config.loader._CONFIG_MODELS 的文件名集合一致，顺序固定为四个文件在
# snapshot_hashes.json 中出现的顺序）。
_CONFIG_FILES: tuple[tuple[str, str], ...] = (
    ("task.yaml", "task"),
    ("model.yaml", "model"),
    ("metrics.yaml", "metrics"),
    ("constraints.yaml", "constraints"),
)

# snapshot_hashes.json 的固定文件名，落在 <task_id>/config/ 下，与四个 yaml
# 快照同目录、同一套 stage()/commit() 原子提交机制。
SNAPSHOT_HASHES_FILENAME = "snapshot_hashes.json"


def init_task_artifact_layout(artifact_store: ArtifactStore, task_id: str) -> None:
    """在 `artifacts/<task_id>/` 下一次性创建全部七个子目录。

    `ArtifactStore.stage()` 本身会在被调用时按需创建它当次使用的那一个
    `<task_id>/<kind>/` 目录（任务 8.2），因此本函数不是让目录"存在"的唯一
    途径——它的价值在于任务刚开始时就把完整的七个子目录骨架摆出来，供人工
    或工具立即检视，而不是等各个子目录随着任务推进逐一按需长出。

    幂等：对每个子目录使用 `Path.mkdir(parents=True, exist_ok=True)`，重复
    调用不报错、不产生副作用。
    """
    base = artifact_store._base_dir / task_id  # noqa: SLF001 — 同包内部访问
    for subdir in SEVEN_SUBDIRS:
        (base / subdir).mkdir(parents=True, exist_ok=True)


def snapshot_configs(
    artifact_store: ArtifactStore,
    task_id: str,
    config_dir: Path,
    bundle: ConfigBundle,
) -> dict[str, str]:
    """把四个原始 YAML 配置文件快照 + 各自的快照完整性哈希落到
    `artifacts/<task_id>/config/`。

    四个文件从 `config_dir`（`config.loader.load_all()` 加载时所指向的目录）
    按原始字节读取，经 `ArtifactStore.stage()` + `commit()` 原子提交到
    `<task_id>/config/<filename>`——不绕过该往返校验 + 原子移动机制去做裸
    文件复制，因为本任务的意义之一就是让快照具备与其他产物相同的原子性与
    完整性保证。

    每个文件的哈希取
    `sha256(canonical_json(bundle.<对应模型>.model_dump(mode="json")))`——这是
    覆盖范围恰为"该文件自身内容"的快照完整性哈希，与
    `sim.hashing.model_package_hash()` / `config.hashing.metrics_hash()` /
    `config.hashing.constraints_hash()` 等在别处承担缓存键/冻结点职责的
    注册哈希是两个不同用途的值（范围、消费方均不同，见本模块顶部说明），
    不可混用、不互相替代。

    四个哈希随后写入同目录下的 `snapshot_hashes.json`（同样经
    `stage()`/`commit()` 原子提交），供事后任何人在不重跑
    `config.loader.load_all()` 的前提下，仅凭 `config/` 下的快照文件本身
    重算并比对哈希，核验快照未被篡改。

    返回 `{filename: hex_hash}`，四个键固定为 `task.yaml` / `model.yaml` /
    `metrics.yaml` / `constraints.yaml`。
    """
    config_dir = Path(config_dir)
    hashes: dict[str, str] = {}

    for filename, bundle_field in _CONFIG_FILES:
        raw_bytes = (config_dir / filename).read_bytes()

        tmp = artifact_store.stage(task_id, "config", filename)
        tmp.write_bytes(raw_bytes)
        artifact_store.commit(tmp)

        model = getattr(bundle, bundle_field)
        content_json = canonical_json(model.model_dump(mode="json"))
        hashes[filename] = _sha256_hex(content_json)

    hashes_tmp = artifact_store.stage(task_id, "config", SNAPSHOT_HASHES_FILENAME)
    hashes_tmp.write_text(
        json.dumps(hashes, sort_keys=True, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_store.commit(hashes_tmp)

    return hashes


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
