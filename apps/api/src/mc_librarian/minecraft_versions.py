"""Mojang 版本清单的 release 列表（版本页下拉框的数据源）。

只返回 `type == "release"` 的版本，按发布时间倒序；字段 `{id, released_at}`。
**进程内缓存**（TTL 1 小时）：打开注册对话框会反复调这个端点，不能每次都打网络。
网络失败时若缓存可用就返回缓存（stale 也比空列表好），否则抛 `ManifestError`——
调用方（`api/v1/minecraft_versions.py`）把它翻成明确的错误响应，而不是一个空列表。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from mc_librarian.fetch.manifest import MANIFEST_URL, fetch_bytes

logger = logging.getLogger("mc_librarian.minecraft_versions")

# 缓存有效期（秒）：1 小时。
CACHE_TTL_S: Final = 3600.0


class ManifestError(RuntimeError):
    """拉取 / 解析 Mojang 版本清单失败；调用方翻成可操作的错误响应。"""


@dataclass(frozen=True)
class MinecraftRelease:
    """版本清单里的一条 release：版本号 + 发布时间。"""

    id: str
    released_at: datetime


def _parse_releases(raw: bytes) -> tuple[MinecraftRelease, ...]:
    """清单 JSON → release 列表（按发布时间倒序）。"""
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ManifestError(f"版本清单不是合法 JSON：{error}") from error

    versions = loaded.get("versions") if isinstance(loaded, dict) else None

    if not isinstance(versions, list):
        raise ManifestError("版本清单里没有 versions 数组")

    releases: list[MinecraftRelease] = []

    for entry in versions:
        if not isinstance(entry, dict) or entry.get("type") != "release":
            continue

        version_id = entry.get("id")
        released_at = entry.get("releaseTime")

        if not isinstance(version_id, str) or not isinstance(released_at, str):
            continue

        try:
            parsed = datetime.fromisoformat(released_at)
        except ValueError:
            continue

        releases.append(MinecraftRelease(id=version_id, released_at=parsed))

    releases.sort(key=lambda item: item.released_at, reverse=True)

    return tuple(releases)


class VersionCatalog:
    """带 TTL 缓存的版本清单读取；`fetch` / `clock` 可注入，测试不联网。"""

    def __init__(
        self,
        *,
        url: str = MANIFEST_URL,
        fetch: Callable[[str], bytes] = fetch_bytes,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self._url = url
        self._fetch = fetch
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._cache: tuple[float, tuple[MinecraftRelease, ...]] | None = None

    def releases(self) -> tuple[MinecraftRelease, ...]:
        """release 列表；命中缓存直接返回，失败时退回（可能过期的）缓存。"""
        with self._lock:
            cached = self._cache

            if cached is not None and self._clock() - cached[0] < self._ttl_s:
                return cached[1]

            try:
                versions = _parse_releases(self._fetch(self._url))
            except Exception as error:
                if cached is not None:
                    logger.warning("拉取版本清单失败，改用缓存：%s", error)

                    return cached[1]

                raise ManifestError(str(error)) from error

            self._cache = (self._clock(), versions)

            return versions

    def clear(self) -> None:
        """清空缓存（测试用）。"""
        with self._lock:
            self._cache = None


# 进程内单例：路由用它，测试用独立实例（注入假 fetch）。
catalog: Final = VersionCatalog()
