"""pytest 共享 fixture。"""

from pathlib import Path

import pytest

# 仓库根目录：tests/conftest.py 的上一级。
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录。配置与模型文件的相对路径均以此为基准解析。"""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    """四个配置文件所在目录。"""
    return repo_root / "configs"
