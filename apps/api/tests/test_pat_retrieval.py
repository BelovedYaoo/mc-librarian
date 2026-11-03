"""PAT（`Authorization: Bearer mcl_...`）走真实检索接口的端到端验收（issue #18）。

#10 的验收项「使用令牌调用检索接口会更新 `last_used_at`」在 #5 / #6 落地前只有
`GET /v1/auth/me` 的间接覆盖。这里用**不带任何会话 cookie**的客户端，只靠 Bearer
打 `POST /v1/search` 与 `GET /v1/symbol`，断言 200 且该令牌的 `last_used_at` 被更新；
顺带断言 PAT 的权限与登录会话等价（角色跟人走：`user` 的 PAT 仍是 `user`，
`admin` 的 PAT 能进 `/v1/admin/*`）。

索引与源码都写在 `tmp_path`（`settings.data_dir`），不碰仓库里的真实数据。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import USER_PASSWORD, USER_USERNAME, join_via_invite
from mc_librarian.config import Settings
from mc_librarian.indexing.build import build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"

VERSION = "26.3"


def silent(_level: str, _message: str) -> None:
    """`build_index` 的日志回调；这里不关心输出。"""


def bearer(raw_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_token}"}


def issue_pat(client: TestClient, *, name: str) -> tuple[str, str]:
    """签发一枚令牌，返回（明文、id）；明文只在这次 201 里存在。"""
    response = client.post("/v1/auth/tokens", json={"name": name, "expires_in_days": 30})

    assert response.status_code == 201, response.text

    body = response.json()

    return str(body["token"]), str(body["info"]["id"])


def last_used_at(conn: sqlite3.Connection, token_id: str) -> str | None:
    row = conn.execute("SELECT last_used_at FROM pats WHERE id = ?", (token_id,)).fetchone()

    assert row is not None, "令牌应该还在库里"

    value: str | None = row["last_used_at"]

    return value


@pytest.fixture
def indexed(admin_client: TestClient, settings: Settings, conn: sqlite3.Connection) -> Path:
    """数据根里已就位 fixture 源码、已建索引、已注册版本的 26.3。"""
    root = source_root(settings.data_dir, VERSION)
    place_source_tree(FIXTURE_SOURCE, root)
    index = index_path(settings.data_dir, VERSION)
    result = build_index(root, index, log=silent)
    store.register_version(
        conn,
        version=VERSION,
        files=result.files,
        tokens=result.tokens,
        obfuscated=False,
        has_summaries=False,
        source_size_bytes=index.stat().st_size,
        db_path=str(index),
    )
    conn.commit()

    return root


def test_pat_retrieval_updates_last_used_at(
    admin_client: TestClient, other_client: TestClient, indexed: Path, conn: sqlite3.Connection
) -> None:
    """PAT 调 `POST /v1/search` 与 `GET /v1/symbol`：都 200，且各自令牌的 `last_used_at` 被更新。

    `other_client` 是全新「浏览器」，cookie jar 里没有会话；两个请求只带 Bearer，
    所以命中的一定是 PAT 解析路径（会话路径不写 `pats.last_used_at`）。
    """
    search_token, search_id = issue_pat(admin_client, name="search")
    symbol_token, symbol_id = issue_pat(admin_client, name="symbol")

    assert last_used_at(conn, search_id) is None
    assert last_used_at(conn, symbol_id) is None

    search = other_client.post(
        "/v1/search", json={"version": VERSION, "query": "Mob"}, headers=bearer(search_token)
    )
    symbol = other_client.get(
        "/v1/symbol", params={"version": VERSION, "name": "Mob"}, headers=bearer(symbol_token)
    )

    assert search.status_code == 200, search.text
    assert search.json()["hits"], "fixture 里 `Mob` 应该有命中"
    assert symbol.status_code == 200, symbol.text
    assert [match["fqn"] for match in symbol.json()["matches"]] == ["net.minecraft.world.Mob"]

    assert last_used_at(conn, search_id) is not None
    assert last_used_at(conn, symbol_id) is not None


def test_admin_pat_reaches_admin_endpoints(
    admin_client: TestClient, other_client: TestClient, indexed: Path, conn: sqlite3.Connection
) -> None:
    """角色跟人走：admin 的 PAT 一样能进 `/v1/admin/*`，并同样更新 `last_used_at`。"""
    raw_token, token_id = issue_pat(admin_client, name="admin")

    response = other_client.get("/v1/admin/settings", headers=bearer(raw_token))

    assert response.status_code == 200, response.text
    assert last_used_at(conn, token_id) is not None


def test_user_pat_reaches_user_endpoints_but_not_admin(
    admin_client: TestClient, other_client: TestClient, indexed: Path, conn: sqlite3.Connection
) -> None:
    """user 的 PAT 能调 user 级检索接口，但仍是 user：`/v1/admin/*` 一律 403。"""
    join_via_invite(admin_client, other_client, username=USER_USERNAME, password=USER_PASSWORD)
    raw_token, token_id = issue_pat(other_client, name="member")

    other_client.cookies.clear()  # 只留 Bearer，证明 PAT 能单独使用

    headers = bearer(raw_token)
    search = other_client.post(
        "/v1/search", json={"version": VERSION, "query": "Mob"}, headers=headers
    )
    symbol = other_client.get(
        "/v1/symbol", params={"version": VERSION, "name": "Mob"}, headers=headers
    )
    admin_only = other_client.get("/v1/admin/settings", headers=headers)

    assert search.status_code == 200, search.text
    assert symbol.status_code == 200, symbol.text
    assert admin_only.status_code == 403, admin_only.text
    assert admin_only.headers["content-type"].startswith("application/problem+json")
    assert last_used_at(conn, token_id) is not None
