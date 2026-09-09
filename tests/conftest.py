"""pytest 共享 fixture。"""

from pathlib import Path

import pytest

from poweragent.config.env import load_local_env

# 仓库根目录：tests/conftest.py 的上一级。
REPO_ROOT = Path(__file__).resolve().parents[1]

# 在收集测试前载入本地 .env，使标了 live_llm 的条件测试能读到凭据。没有 .env 时
# 静默跳过——CI 直接把凭据设在环境里，或者根本不跑那些测试。
load_local_env(REPO_ROOT / ".env")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录。配置与模型文件的相对路径均以此为基准解析。"""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    """四个配置文件所在目录。"""
    return repo_root / "configs"
