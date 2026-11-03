"""运行时配置。

数据根由 `MC_LIBRARIAN_DATA_DIR` 指定；未指定时**无条件**落在仓库之外的同级目录
`<repo 的父目录>/mc-librarian-data`，找不到仓库哨兵时退回 `~/mc-librarian-data`，
与 `docs/00-overview.md` §4 的数据布局一致（D9：数据根独立、不入库）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR_ENV: Final = "MC_LIBRARIAN_DATA_DIR"
DATA_DIR_NAME: Final = "mc-librarian-data"

# 会话 cookie 名，与 docs/openapi.yaml 的 `components.securitySchemes.cookieAuth` 一致。
SESSION_COOKIE_NAME: Final = "mc_librarian_session"

# 仓库哨兵：同时存在 `package.json` 与 `apps/api/` 才算仓库根。
# 刻意不用 `.git`（worktree / 源码快照里可能没有）。
_REPO_SENTINELS: Final = (Path("package.json"), Path("apps", "api"))


def find_repo_root(start: Path | None = None) -> Path | None:
    """从 `start`（默认本文件）向上查找仓库根，找不到返回 None。"""
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if all((candidate / sentinel).exists() for sentinel in _REPO_SENTINELS):
            return candidate
    return None


def resolve_data_dir(value: Path, root: Path | None = None) -> Path:
    """规范化数据根：相对路径相对**仓库根**解析，且结果不允许落在仓库内。

    dev 时进程 cwd 是 `apps/api`，直接按 cwd 解析相对路径会静默落进仓库，所以统一锚到仓库根，
    并对「落进仓库」显式报错（D9：数据根独立、不入库）。
    """
    root = root if root is not None else find_repo_root()
    expanded = value.expanduser()

    if not expanded.is_absolute():
        expanded = (root or Path.cwd()) / expanded

    resolved = expanded.resolve()

    if root is not None and resolved.is_relative_to(root):
        raise ValueError(
            f"数据根不能落在仓库内：{resolved}（仓库根 {root}）。"
            f" 相对路径按仓库根解析，请用例如 `../{DATA_DIR_NAME}`，"
            f" 或用 {DATA_DIR_ENV} 指向仓库之外的绝对路径。"
        )

    return resolved


def default_data_dir(start: Path | None = None) -> Path:
    """数据根的默认值，永远在仓库之外。

    仓库哨兵可见时用 `<repo 的父目录>/mc-librarian-data`；
    否则（例如装在 site-packages 里运行）退回 `~/mc-librarian-data`。
    """
    root = find_repo_root(start)
    if root is not None:
        return root.parent / DATA_DIR_NAME
    return Path.home() / DATA_DIR_NAME


class Settings(BaseSettings):
    """进程级配置，全部可通过 `MC_LIBRARIAN_*` 环境变量覆盖。"""

    model_config = SettingsConfigDict(env_prefix="MC_LIBRARIAN_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8787

    # 会话 cookie 的 `Secure` 属性：本地 http 下给 true 会让浏览器直接丢弃 cookie，
    # 所以默认 false（dev）；生产在 HTTPS 边界后面部署时设 `MC_LIBRARIAN_COOKIE_SECURE=true`。
    cookie_secure: bool = False
    # 会话有效期（小时），与 `settings.security.session_ttl_hours` 的默认值保持一致。
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 365)

    @field_validator("data_dir")
    @classmethod
    def _resolve(cls, value: Path) -> Path:
        return resolve_data_dir(value)

    @property
    def repo_root(self) -> Path | None:
        return find_repo_root()

    @property
    def data_dir_from_env(self) -> bool:
        """数据根是不是由 `MC_LIBRARIAN_DATA_DIR` 显式指定。"""
        return DATA_DIR_ENV in os.environ


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例，便于在路由中注入。"""
    return Settings()
