"""Mojang 版本清单：把版本号解析成 client jar 与官方 mappings 的下载地址。

清单入口 `version_manifest_v2.json` 每个版本只有元数据地址；真正的 `downloads` 在版本
自己的 JSON 里。**是否需要 remap 直接由「有没有 `client_mappings`」决定**，不比较版本号：
26.x 起 Mojang 不再混淆、不再提供 mappings（1.14.4 之前的版本同样没有）。

`HttpManifestResolver` 的 `fetch` 可注入，测试用本地构造的 JSON，不联网。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.request import urlopen

from mc_librarian.fetch.errors import FetchError

MANIFEST_URL: Final = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_TIMEOUT_S: Final = 60.0


@dataclass(frozen=True)
class VersionDownloads:
    """一个版本要下载的两个地址；`client_mappings_url` 为空表示无需 remap。"""

    version: str
    client_url: str
    client_mappings_url: str | None

    @property
    def needs_remap(self) -> bool:
        return self.client_mappings_url is not None


class ManifestResolver(Protocol):
    def resolve(self, version: str) -> VersionDownloads: ...


def fetch_bytes(url: str) -> bytes:
    """下载一个小文件到内存（清单 / 版本元数据）。"""
    with urlopen(url, timeout=MANIFEST_TIMEOUT_S) as response:
        return bytes(response.read())


class HttpManifestResolver:
    """真实实现：HTTP 拉清单与版本元数据。"""

    def __init__(
        self,
        *,
        manifest_url: str = MANIFEST_URL,
        fetch: Callable[[str], bytes] = fetch_bytes,
    ) -> None:
        self._manifest_url = manifest_url
        self._fetch = fetch

    def resolve(self, version: str) -> VersionDownloads:
        manifest = _load_json(self._fetch(self._manifest_url), self._manifest_url)
        entry = _find_version(manifest, version)
        meta_url = entry.get("url")

        if not isinstance(meta_url, str) or not meta_url:
            raise FetchError(f"版本清单里 {version} 缺少元数据地址")

        meta = _load_json(self._fetch(meta_url), meta_url)
        downloads = meta.get("downloads")

        if not isinstance(downloads, dict):
            raise FetchError(f"版本 {version} 的元数据里没有 downloads")

        client = downloads.get("client")
        client_url = client.get("url") if isinstance(client, dict) else None

        if not isinstance(client_url, str) or not client_url:
            raise FetchError(f"版本 {version} 的元数据里没有 downloads.client.url")

        mappings = downloads.get("client_mappings")
        mappings_url = mappings.get("url") if isinstance(mappings, dict) else None

        return VersionDownloads(
            version=version,
            client_url=client_url,
            client_mappings_url=mappings_url if isinstance(mappings_url, str) else None,
        )


def _load_json(raw: bytes, source: str) -> dict[str, Any]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise FetchError(f"解析 JSON 失败：{source}（{error}）") from error

    if not isinstance(loaded, dict):
        raise FetchError(f"JSON 顶层不是对象：{source}")

    return loaded


def _find_version(manifest: dict[str, Any], version: str) -> dict[str, Any]:
    versions = manifest.get("versions")

    if not isinstance(versions, list):
        raise FetchError("版本清单里没有 versions 数组")

    for entry in versions:
        if isinstance(entry, dict) and entry.get("id") == version:
            return entry

    raise FetchError(f"版本清单里找不到版本：{version}")
