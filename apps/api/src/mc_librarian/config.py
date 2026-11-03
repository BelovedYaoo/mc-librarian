"""运行时配置。

数据根由 `MC_LIBRARIAN_DATA_DIR` 指定；未指定时落在仓库**之外**的同级目录
`<repo 的父目录>/mc-librarian-data`，与 `docs/00-overview.md` §4 的数据布局一致。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR_ENV: Final = "MC_LIBRARIAN_DATA_DIR"
DATA_DIR_NAME: Final = "mc-librarian-data"

# 仓库根标志：契约文件 + git 目录同时存在。
_REPO_MARKERS: Final = (Path("docs", "openapi.yaml"), Path(".git"))


def find_repo_root(start: Path | None = None) -> Path | None:
    """从 `start`（默认本文件）向上查找仓库根，找不到返回 None。"""
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if all((candidate / marker).exists() for marker in _REPO_MARKERS):
            return candidate
    return None


def default_data_dir() -> Path:
    """仓库以外的数据根：`<repo 的父目录>/mc-librarian-data`。"""
    root = find_repo_root()
    if root is not None:
        return root.parent / DATA_DIR_NAME
    return Path.cwd() / DATA_DIR_NAME


class Settings(BaseSettings):
    """进程级配置，全部可通过 `MC_LIBRARIAN_*` 环境变量覆盖。"""

    model_config = SettingsConfigDict(env_prefix="MC_LIBRARIAN_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8787

    @field_validator("data_dir")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def repo_root(self) -> Path | None:
        return find_repo_root()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例，便于在路由中注入。"""
    return Settings()
