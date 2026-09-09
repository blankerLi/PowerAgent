"""poweragent/store/db.py

SQLite 连接助手 + `tx()` 事务上下文管理器 + 从 `ddl.sql` 初始化数据库。

- 连接助手在每次打开连接后设置 `PRAGMA journal_mode=WAL` 与 `PRAGMA foreign_keys=ON`，
  并回读两个 PRAGMA 以确认生效（`journal_mode` 应为 `wal`、`foreign_keys` 应为 `1`），
  不一致时抛异常。`foreign_keys` 是逐连接生效的 PRAGMA，SQLite 不会把它持久化到文件中，
  因此每次打开连接都必须重新设置。
- `tx()` 以 `BEGIN IMMEDIATE` 开启事务：正常退出提交；异常回滚并原样传播（不吞异常、
  不改写异常类型）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DDL_PATH = Path(__file__).with_name("ddl.sql")


class PragmaVerificationError(RuntimeError):
    """`PRAGMA journal_mode` 或 `PRAGMA foreign_keys` 回读值与期望不符时抛出。"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开到 `db_path` 的 SQLite 连接，设置并回读 `journal_mode` 与 `foreign_keys`。

    回读值不为 `wal` / `1` 时抛出 `PragmaVerificationError`。
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        conn.close()
        raise PragmaVerificationError(
            f"PRAGMA journal_mode readback expected 'wal', got {journal_mode!r}"
        )

    foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    if int(foreign_keys) != 1:
        conn.close()
        raise PragmaVerificationError(
            f"PRAGMA foreign_keys readback expected 1, got {foreign_keys!r}"
        )

    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """以 `BEGIN IMMEDIATE` 开启事务；正常退出提交，异常回滚并原样传播。"""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """从 `ddl.sql` 在 `db_path` 处初始化一个全新的数据库，返回其连接。

    数据库文件不应预先存在非空内容；本函数不做既有表的迁移。
    """
    conn = connect(db_path)
    ddl = DDL_PATH.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()
    return conn
