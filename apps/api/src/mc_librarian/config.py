"""运行时配置。

数据根由 `MC_LIBRARIAN_DATA_DIR` 指定；未指定时**无条件**落在仓库之外的同级目录
`<repo 的父目录>/mc-librarian-data`，找不到仓库哨兵时退回 `~/mc-librarian-data`，
与 `docs/00-overview.md` §4 的数据布局一致（D9：数据根独立、不入库）。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR_ENV: Final = "MC_LIBRARIAN_DATA_DIR"
DATA_DIR_NAME: Final = "mc-librarian-data"

# 运行环境。`production` 下会给会话 cookie 加硬性护栏（见 `Settings._guard_production`）。
ENV_DEV: Final = "dev"
ENV_PRODUCTION: Final = "production"

# 会话 cookie 名，与 docs/openapi.yaml 的 `components.securitySchemes.cookieAuth` 一致。
SESSION_COOKIE_NAME: Final = "mc_librarian_session"

# 仓库哨兵：同时存在 `package.json` 与 `apps/api/` 才算仓库根。
# 刻意不用 `.git`（worktree / 源码快照里可能没有）。
_REPO_SENTINELS: Final = (Path("package.json"), Path("apps", "api"))

# `-Xmx` 的值：数字 + 单位，避免把任意字符串拼进 java 命令行。
DECOMPILE_MEMORY_PATTERN: Final = re.compile(r"^\d+[kKmMgG]$")


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

    # `MC_LIBRARIAN_ENV`：dev / production。只有 production 会启用 cookie 硬性护栏。
    env: Literal["dev", "production"] = "dev"
    # 会话 cookie 的 `Secure` 属性：本地 http 下给 true 会让浏览器直接丢弃 cookie，
    # 所以默认 false（dev）；生产在 HTTPS 边界后面部署时设 `MC_LIBRARIAN_COOKIE_SECURE=true`。
    cookie_secure: bool = False
    # 会话有效期（小时），与 `settings.security.session_ttl_hours` 的默认值保持一致。
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 365)
    # 一次性邀请链接的有效期（小时），与 `settings.security.invite_ttl_hours` 一致。
    invite_ttl_hours: int = Field(default=48, ge=1, le=24 * 365)

    # 下载 / 反编译工具链（D24：镜像内只要 JRE + Vineflower + SpecialSource，不装 Gradle）。
    # 两个 jar 路径默认空；只有真正跑 download/decompile 阶段才要求配置，parse-only 任务不受影响。
    java: str = "java"
    vineflower_jar: Path | None = None
    specialsource_jar: Path | None = None
    decompile_memory: str = "4G"

    # 索引构建总闸门（`MC_LIBRARIAN_INDEX_ENABLED`）：关掉后 `POST /v1/admin/jobs` 返回 403。
    # 完整五段（下载 + 反编译 + 全树解析）跑在服务进程内，反编译一个版本要 3–4 GiB；
    # 生产节点内存不够，所以在节点上设 false，索引在本地工作站产出后用
    # `POST /v1/admin/versions` 注册。默认 true，保证本地开箱可用。
    index_enabled: bool = True

    @field_validator("decompile_memory")
    @classmethod
    def _check_decompile_memory(cls, value: str) -> str:
        if not DECOMPILE_MEMORY_PATTERN.match(value):
            raise ValueError(f"decompile_memory 必须是形如 4G / 512M 的值，收到：{value}")

        return value

    @field_validator("data_dir")
    @classmethod
    def _resolve(cls, value: Path) -> Path:
        return resolve_data_dir(value)

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        """生产护栏：公网部署忘了开 `Secure`，会话 cookie 会明文跑在 HTTP 上。

        dev（默认）下不动，因为本地是 http，`Secure` 会让浏览器直接丢弃 cookie。
        """
        if self.env == ENV_PRODUCTION and not self.cookie_secure:
            raise ValueError(
                "生产环境必须给会话 cookie 加 Secure：请设置 MC_LIBRARIAN_COOKIE_SECURE=true。"
                f"（当前 MC_LIBRARIAN_ENV={self.env}、cookie_secure=false；"
                f" 仅本地调试时才把 MC_LIBRARIAN_ENV 设回 {ENV_DEV}）"
            )

        return self

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
