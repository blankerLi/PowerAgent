"""把本地 `.env` 载入环境变量。

为什么需要这一步
----------------
`design.md` §14 要求凭据只从环境变量读取，不写入四个 YAML、数据库或 `artifacts/`。
但在本地开发时把 Key 常驻在 shell 会话里不方便（换终端就没了、写进 profile 又等于
永久暴露给所有进程），惯例做法是放一个被 gitignore 的 `.env`。

`.env` 不属于被禁止的那几个位置，它就是"环境变量的本地来源"。本模块只做搬运：
读文件、写进 `os.environ`，之后所有代码仍然只认环境变量，凭据的读取路径没有变多。

为什么不引入 python-dotenv
--------------------------
需要的语法只有 `KEY=VALUE`、注释行和空行三种。为此加一个依赖不划算——依赖不是免费
的，它要进锁文件、要跟着升级、在 CI 里要装。二十行代码能覆盖的场景不值得。

已存在的环境变量优先
--------------------
`os.environ` 里已有的值不被文件覆盖。这是 dotenv 生态的通行语义，也符合直觉：
显式在命令行里设的值应当压过文件里的默认值，否则临时切换一个模型跑对比实验就得
去改文件。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_local_env", "DEFAULT_ENV_FILENAME"]

DEFAULT_ENV_FILENAME = ".env"


def load_local_env(path: str | Path = DEFAULT_ENV_FILENAME) -> dict[str, str]:
    """把 `path` 指向的 `.env` 载入 `os.environ`，返回本次实际写入的键值。

    文件不存在时静默返回空字典——没有 `.env` 是完全正常的状态（凭据也可以直接设在
    环境里，CI 就是这样）。

    解析规则刻意保持最小：忽略空行与 `#` 开头的注释行；`KEY=VALUE` 按第一个等号
    切分；去掉 VALUE 两端空白与一层配对的引号。不支持变量插值、多行值、`export`
    前缀——这些语法在本项目的 `.env` 里都不需要，支持它们只会带来"文件里写了但没
    生效"这类难查的问题。

    返回值只含**本次写入**的键：已经存在于环境中的键不被覆盖，也不出现在返回值里，
    因此调用方可以据此判断"这次到底从文件里补了什么"。
    """
    env_path = Path(path)
    if not env_path.is_file():
        return {}

    applied: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if not key or not value:
            continue
        if key in os.environ:
            continue

        os.environ[key] = value
        applied[key] = value

    return applied
