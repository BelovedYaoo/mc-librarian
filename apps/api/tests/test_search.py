"""混合检索 `POST /v1/search`（issue #6）。

三路通道各自独立测试（直接调通道函数，用 fixture 索引库 + fixture 源码树），
再加融合、`why`、`detail`、`max_tokens`、`path_prefix`、`suggestions` 与三条降级路径。
索引与源码都写在 `tmp_path`，不碰仓库里的真实数据（`mc-librarian-data/`）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.indexing import query
from mc_librarian.indexing.build import build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store
from mc_librarian.retrieval import channels

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"

VERSION = "26.3"
MOB_PATH = "net/minecraft/world/Mob.java"
WORLD_PATH = "net/minecraft/world/World.java"
ENTITY_PATH = "net/minecraft/world/Entity.java"
DATA_PATH = "data/minecraft/tags/mob_category.json"

# `why` 的固定顺序（issue #6 规格 §4）。
WHY_ORDER = ["summary", "symbol", "literal", "reference"]

# 大方法：让 `snippet` 成为响应体的主要体积，才能验证 `locate` 的体量优势。
BIG_METHOD_LINES = 80
BIG_PATH = "net/big/Big.java"


def silent(_level: str, _message: str) -> None:
    pass


def register(settings: Settings, conn: sqlite3.Connection, version: str = VERSION) -> Path:
    """把 fixture 源码树就位、建索引、直接注册版本（无 admin 会话也能用）。"""
    root = source_root(settings.data_dir, version)
    place_source_tree(FIXTURE_SOURCE, root)
    index = index_path(settings.data_dir, version)
    result = build_index(root, index, log=silent)
    store.register_version(
        conn,
        version=version,
        files=result.files,
        tokens=result.tokens,
        obfuscated=False,
        has_summaries=False,
        source_size_bytes=index.stat().st_size,
        db_path=str(index),
    )
    conn.commit()

    return root


@pytest.fixture
def indexed(user_client: TestClient, settings: Settings, conn: sqlite3.Connection) -> Path:
    """数据根里已就位 fixture 源码、已建索引、已注册版本的 26.3。"""
    return register(settings, conn)


@pytest.fixture
def index_conn(settings: Settings, indexed: Path) -> Iterator[sqlite3.Connection]:
    connection = query.open_readonly(index_path(settings.data_dir, VERSION))

    yield connection

    connection.close()


def post_search(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/v1/search", json={"version": VERSION, **body})

    assert response.status_code == 200, response.text

    return cast("dict[str, Any]", response.json())


def build_version(settings: Settings, conn: sqlite3.Connection, files: dict[str, str]) -> Path:
    """把 `{相对路径: 内容}` 写成源码树、建索引、直接注册版本。"""
    root = source_root(settings.data_dir, VERSION)

    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

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


def build_big_version(settings: Settings, conn: sqlite3.Connection) -> Path:
    """一棵只含一个大方法的源码树；用来验证 `locate` 与 `snippet` 的体量比。"""
    lines = ["package net.big;", "", "public class Big {", "    public void huge() {"]

    for step in range(BIG_METHOD_LINES):
        lines.append(
            f"        int value{step:03d} = computeSpawnBudget({step}) + 100000;"
        )

    lines.append("    }")
    lines.append("")
    lines.append("    private int computeSpawnBudget(int seed) { return seed + 1; }")
    lines.append("}")

    return build_version(settings, conn, {BIG_PATH: "\n".join(lines) + "\n"})


# ----------------------------------------------------------------------
# 三路通道（直接调通道函数）
# ----------------------------------------------------------------------


def test_symbol_channel_reuses_four_level_matching(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    candidates = channels.symbol_candidates(
        index_conn, "Mob", kind="any", path_prefix=None
    )

    assert [(item.symbol, item.kind) for item in candidates] == [
        ("net.minecraft.world.Mob", "class")
    ]


def test_literal_channel_reports_file_lines(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    """只出现在 `data/` JSON 里的字面量：没有符号覆盖，退化为 `type=file`。"""
    candidates = channels.literal_candidates(
        index_conn, source_root(settings.data_dir, VERSION), "hostile", path_prefix=None
    )

    assert [(item.path, item.start_line, item.kind, item.symbol) for item in candidates] == [
        (DATA_PATH, 3, "file", None)
    ]


def test_literal_channel_normalizes_to_innermost_symbol(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    """`tick` 的三处字面量分别归到所在符号：Entity#tick / Mob#track / World#spawn。"""
    candidates = channels.literal_candidates(
        index_conn, source_root(settings.data_dir, VERSION), "tick", path_prefix=None
    )

    assert [(item.path, item.symbol) for item in candidates] == [
        (ENTITY_PATH, "net.minecraft.world.Entity#tick"),
        (MOB_PATH, "net.minecraft.world.Mob#track"),
        (WORLD_PATH, "net.minecraft.world.World#spawn"),
    ]


def test_reference_channel_reverses_refs(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    candidates = channels.reference_candidates(index_conn, "tick", path_prefix=None)

    assert [(item.path, item.symbol, item.kind) for item in candidates] == [
        (MOB_PATH, "net.minecraft.world.Mob#track", "method"),
        (WORLD_PATH, "net.minecraft.world.World#spawn", "method"),
    ]


def test_summary_channel_is_empty_without_the_table(
    indexed: Path, index_conn: sqlite3.Connection
) -> None:
    """`summaries` 表由 #7 建；不存在时该路返回空、不报错（规格 §2）。"""
    assert query.has_table(index_conn, "summaries") is False
    assert channels.summary_candidates(index_conn, "刷怪上限", path_prefix=None) == []


# ----------------------------------------------------------------------
# 融合与 `why`
# ----------------------------------------------------------------------


def test_fusion_merges_two_channels_and_puts_the_hit_first(
    user_client: TestClient, indexed: Path
) -> None:
    """`tick` 同时命中 Entity#tick 的 symbol 与 literal 两路，融合后累加并排第一（验收）。"""
    body = post_search(user_client, {"query": "tick"})
    hits = body["hits"]

    assert hits[0]["symbol"] == "net.minecraft.world.Entity#tick"
    assert hits[0]["why"] == ["symbol", "literal"]

    merged = {hit["symbol"]: hit["why"] for hit in hits}
    # literal 与 reference 落在同一个实体（调用点所在方法）上，也会累加。
    assert merged["net.minecraft.world.Mob#track"] == ["literal", "reference"]
    assert merged["net.minecraft.world.World#spawn"] == ["literal", "reference"]


def test_total_counts_candidates_before_fusion(user_client: TestClient, indexed: Path) -> None:
    """`total` 是融合前的候选总数：symbol 1 + literal 3 + reference 2 = 6。"""
    body = post_search(user_client, {"query": "tick"})

    assert body["total"] == 6
    assert len(body["hits"]) == 3


def test_why_is_deduped_and_follows_the_fixed_order(
    user_client: TestClient, indexed: Path
) -> None:
    body = post_search(user_client, {"query": "tick"})

    for hit in body["hits"]:
        why = hit["why"]
        assert why, "why 至少含一个来源"
        assert len(set(why)) == len(why), "why 必须去重"
        # 是固定顺序的子序列。
        assert why == [channel for channel in WHY_ORDER if channel in why]


def test_summary_never_appears_before_issue_seven(user_client: TestClient, indexed: Path) -> None:
    body = post_search(user_client, {"query": "Mob"})

    assert body["hits"]
    assert all("summary" not in hit["why"] for hit in body["hits"])


# ----------------------------------------------------------------------
# detail / max_tokens / path_prefix / suggestions
# ----------------------------------------------------------------------


def test_locate_response_is_a_fraction_of_snippet(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """`locate` 不含 `snippet` / `summary`，体量约为 `snippet` 的五分之一（验收）。"""
    build_big_version(settings, conn)
    full = user_client.post(
        "/v1/search", json={"version": VERSION, "query": "computeSpawnBudget", "limit": 10}
    )
    located = user_client.post(
        "/v1/search",
        json={"version": VERSION, "query": "computeSpawnBudget", "limit": 10, "detail": "locate"},
    )

    assert full.status_code == 200, full.text
    assert located.status_code == 200, located.text

    full_body = full.json()
    located_body = located.json()

    assert full_body["hits"] and located_body["hits"]
    # locate 真的不带片段与摘要。
    assert all("snippet" not in hit for hit in located_body["hits"])
    assert all("summary" not in hit for hit in located_body["hits"])
    assert any(hit.get("snippet") for hit in full_body["hits"])
    # 留余量：断言 < 1/3，实现约为 1/5。
    assert len(located.content) < len(full.content) / 3


def test_max_tokens_drops_after_budget_but_keeps_the_first(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    build_big_version(settings, conn)
    body = post_search(
        user_client, {"query": "computeSpawnBudget", "limit": 10, "max_tokens": 200}
    )
    hits = body["hits"]

    assert len(hits) >= 1, "至少返回第一条，不能出现「有命中却返回空」"
    assert sum(hit.get("tokens") or 0 for hit in hits) <= 200 or len(hits) == 1


def test_path_prefix_filters_candidates_before_fusion(
    user_client: TestClient, indexed: Path
) -> None:
    """设前缀后 `total` 下降但 `hits` 不为空（过滤发生在候选阶段）。"""
    without = post_search(user_client, {"query": "tick"})
    with_prefix = post_search(user_client, {"query": "tick", "path_prefix": WORLD_PATH})

    assert with_prefix["total"] < without["total"]
    assert with_prefix["hits"]
    assert all(hit["path"] == WORLD_PATH for hit in with_prefix["hits"])


def test_suggestions_drop_query_words_and_cap_at_five(
    user_client: TestClient, indexed: Path
) -> None:
    body = post_search(user_client, {"query": "tick"})
    suggestions = body["suggestions"]

    assert "tick" not in suggestions, "query 中已出现的词要排除"
    assert set(suggestions) == {"spawn", "track"}
    assert len(suggestions) <= 5


# ----------------------------------------------------------------------
# 降级与错误
# ----------------------------------------------------------------------


def test_overloaded_methods_stay_separate(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """同名重载方法是不同实体，不能因为 fqn 相同而被并成一条。"""
    build_version(
        settings,
        conn,
        {
            "net/over/Over.java": (
                "package net.over;\n\npublic class Over {\n"
                "    public int pick(int a) { return a; }\n"
                "    public String pick(String a) { return a; }\n"
                "}\n"
            )
        },
    )

    body = post_search(user_client, {"query": "pick"})
    hits = body["hits"]

    assert len(hits) == 2
    assert {tuple(hit["lines"]) for hit in hits} == {(4, 4), (5, 5)}
    assert {hit["symbol"] for hit in hits} == {"net.over.Over#pick"}


def test_summary_channel_contributes_when_the_table_exists(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection, indexed: Path
) -> None:
    """`summaries` 表就位后（#7），摘要通道真正接入融合（本票只留接口）。"""
    index = index_path(settings.data_dir, VERSION)
    writable = sqlite3.connect(index)

    try:
        writable.executescript(
            "CREATE VIRTUAL TABLE summaries USING fts5(fqn, text_zh, keywords, tokens)"
        )
        writable.execute(
            "INSERT INTO summaries (fqn, text_zh, keywords, tokens) VALUES (?, ?, ?, ?)",
            (
                "net.minecraft.world.Mob",
                "自然生成分类：定义怪物数量上限 spawninterval",
                "spawninterval",
                "tokens",
            ),
        )
        writable.commit()
    finally:
        writable.close()

    body = post_search(user_client, {"query": "spawninterval"})

    assert body["hits"]
    assert body["hits"][0]["why"] == ["summary"]
    assert body["hits"][0]["symbol"] == "net.minecraft.world.Mob"
    assert body["hits"][0]["summary"]


def test_empty_result_is_200_with_empty_arrays(
    user_client: TestClient, indexed: Path
) -> None:
    body = post_search(user_client, {"query": "NoSuchSymbolAnywhere1234"})

    assert body["hits"] == []
    assert body["suggestions"] == []
    assert body["total"] == 0


def test_literal_channel_degrades_when_ripgrep_is_missing(
    user_client: TestClient, indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ripgrep 不可用时 literal 路返回空并写 warn，不影响 symbol / reference 两路（规格 §3）。"""
    monkeypatch.setattr(channels, "RG_BINARY", "definitely-not-ripgrep")

    body = post_search(user_client, {"query": "tick"})

    assert body["hits"], "另外两路照常返回"
    assert all("literal" not in hit["why"] for hit in body["hits"])
    assert any("symbol" in hit["why"] for hit in body["hits"])
    assert any("reference" in hit["why"] for hit in body["hits"])


def test_search_requires_login(client: TestClient) -> None:
    response = client.post("/v1/search", json={"query": "tick", "version": VERSION})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_search_404_when_version_unregistered(user_client: TestClient) -> None:
    response = user_client.post("/v1/search", json={"query": "tick", "version": "9.9"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_search_rejects_empty_query(user_client: TestClient, indexed: Path) -> None:
    response = user_client.post("/v1/search", json={"query": "", "version": VERSION})

    assert response.status_code == 422
