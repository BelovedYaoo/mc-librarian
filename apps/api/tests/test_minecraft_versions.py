"""Mojang 版本清单（release 列表）与 `GET /v1/minecraft-versions`。

覆盖：只返回 `type == "release"`、按发布时间倒序、进程内 TTL 缓存（命中不重拉）、
过期后重拉、网络失败退回缓存、无缓存时抛 `ManifestError`；端点的 admin 权限与错误码。
全部离线：清单由假 `fetch` 提供，时钟可注入。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_librarian import minecraft_versions
from mc_librarian.minecraft_versions import ManifestError, VersionCatalog

MANIFEST: dict[str, Any] = {
    "latest": {"release": "1.21.1"},
    "versions": [
        {
            "id": "1.20.1",
            "type": "release",
            "releaseTime": "2023-06-12T13:25:51+00:00",
        },
        {
            "id": "24w14a",
            "type": "snapshot",
            "releaseTime": "2024-04-03T12:00:00+00:00",
        },
        {
            "id": "1.21.1",
            "type": "release",
            "releaseTime": "2024-08-08T14:00:00+00:00",
        },
        {
            "id": "a1.0",
            "type": "old_alpha",
            "releaseTime": "2010-01-01T00:00:00+00:00",
        },
    ],
}


def fake_fetch(payload: dict[str, Any] = MANIFEST) -> Any:
    def fetch(url: str) -> bytes:
        del url

        return json.dumps(payload).encode("utf-8")

    return fetch


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_catalog_keeps_releases_sorted_by_release_time() -> None:
    catalog = VersionCatalog(fetch=fake_fetch())

    releases = catalog.releases()

    assert [item.id for item in releases] == ["1.21.1", "1.20.1"]
    assert releases[0].released_at.year == 2024


def test_catalog_caches_within_ttl() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)

        return json.dumps(MANIFEST).encode("utf-8")

    catalog = VersionCatalog(fetch=fetch, clock=clock)

    catalog.releases()
    clock.advance(60)
    catalog.releases()

    assert len(calls) == 1, "TTL 内命中缓存，不重复打网络"


def test_catalog_refetches_after_ttl() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)

        return json.dumps(MANIFEST).encode("utf-8")

    catalog = VersionCatalog(fetch=fetch, clock=clock, ttl_s=100)

    catalog.releases()
    clock.advance(101)
    catalog.releases()

    assert len(calls) == 2


def test_catalog_falls_back_to_cache_on_failure() -> None:
    """网络失败时若缓存可用就返回缓存（stale 也比空列表好）。"""
    clock = FakeClock()
    state = {"fail": False}

    def fetch(url: str) -> bytes:
        del url

        if state["fail"]:
            raise OSError("connection refused")

        return json.dumps(MANIFEST).encode("utf-8")

    catalog = VersionCatalog(fetch=fetch, clock=clock, ttl_s=10)

    first = catalog.releases()
    state["fail"] = True
    clock.advance(100)

    assert catalog.releases() == first


def test_catalog_raises_without_cache() -> None:
    def fetch(url: str) -> bytes:
        del url
        raise OSError("connection refused")

    catalog = VersionCatalog(fetch=fetch)

    with pytest.raises(ManifestError):
        catalog.releases()


def test_catalog_rejects_manifest_without_versions() -> None:
    catalog = VersionCatalog(fetch=fake_fetch({"latest": {"release": "1.21.1"}}))

    with pytest.raises(ManifestError, match="versions"):
        catalog.releases()


def _install_catalog(monkeypatch: pytest.MonkeyPatch, fetch: Any) -> None:
    """把路由用的进程内单例换成注入假 fetch 的实例。"""
    monkeypatch.setattr(minecraft_versions, "catalog", VersionCatalog(fetch=fetch))


def test_endpoint_lists_releases(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_catalog(monkeypatch, fake_fetch())

    response = admin_client.get("/v1/minecraft-versions")

    assert response.status_code == 200, response.text

    body = response.json()

    assert [item["id"] for item in body["versions"]] == ["1.21.1", "1.20.1"]
    assert body["versions"][0]["released_at"].startswith("2024-08-08")


def test_endpoint_requires_admin(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_catalog(monkeypatch, fake_fetch())

    assert user_client.get("/v1/minecraft-versions").status_code == 403


def test_endpoint_returns_502_when_manifest_unavailable(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(url: str) -> bytes:
        del url
        raise OSError("connection refused")

    _install_catalog(monkeypatch, fetch)

    response = admin_client.get("/v1/minecraft-versions")

    assert response.status_code == 502, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
