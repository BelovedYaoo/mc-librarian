"""混合检索 `POST /v1/search`（issue #6）。

三路通道各自独立测试（直接调通道函数，用 fixture 索引库 + fixture 源码树），
再加融合、`why`、`detail`、`max_tokens`、`path_prefix`、`suggestions` 与降级路径。
索引与源码都写在 `tmp_path`，不碰仓库里的真实数据（`mc-librarian-data/`）。

`literal` 通道要跑真实的 `rg` 子进程（规格 §3 定死用 ripgrep，不做内置实现），所以依赖它的
用例在 `rg` 不在 PATH 上时 `pytest.skip`：否则没装 ripgrep 的机器上 `bun run check` 直接红。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.indexing import query, summarize
from mc_librarian.indexing.build import (
    SourceSpec,
    build_index,
    build_index_from_sources,
    place_source_tree,
)
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

requires_ripgrep = pytest.mark.skipif(
    shutil.which(channels.RG_BINARY) is None,
    reason="literal 通道要跑 ripgrep 子进程：把 rg 放进 PATH 后再跑这条用例",
)


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


# `#7` 的摘要表：普通表 `summaries` + FTS5 `summaries_fts`（存 jieba 预分词后的 text_zh）。
SUMMARY_ROWS = (
    (
        "net.minecraft.world.Mob",
        "刷怪上限：定义怪物/动物的最大数量上限与每区块生成预算\n"
        "机制：按生物群系权重挑选候选，受 mob cap 与玩家距离约束\n"
        "关键成员：maxInstancesPerChunk, spawnInterval",
    ),
    (
        "net.minecraft.world.Entity",
        "实体刻：每刻推进实体状态并触发 tick 回调\n"
        "机制：服务端/客户端各自按 tick 调度\n"
        "关键成员：tick, position",
    ),
)


def write_summaries(
    settings: Settings, rows: tuple[tuple[str, str], ...] = SUMMARY_ROWS
) -> None:
    """用 `#7` 的真实写入路径给索引库灌摘要（普通表 + jieba 预分词的 FTS5）。"""
    path = index_path(settings.data_dir, VERSION)
    readable = sqlite3.connect(path)

    try:
        ids = {str(row[0]): int(row[1]) for row in readable.execute("SELECT fqn, id FROM classes")}
    finally:
        readable.close()

    summarize.write_summaries(
        path,
        [
            summarize.SummaryRow(
                class_id=ids[fqn],
                fqn=fqn,
                text_zh=text,
                content_hash=summarize.compute_content_hash(fqn, None, (), (), ()),
            )
            for fqn, text in rows
        ],
    )


def write_statements(settings: Settings, statements: tuple[str, ...]) -> None:
    """在索引库里直接执行 DDL（构造「表存在但不是 FTS5」这类异常状态）。"""
    writable = sqlite3.connect(index_path(settings.data_dir, VERSION))

    try:
        for statement in statements:
            writable.execute(statement)

        writable.commit()
    finally:
        writable.close()


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


@requires_ripgrep
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


@requires_ripgrep
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
    """`summaries_fts` 由 #7 建；不存在时该路返回空、不报错（规格 §2）。"""
    assert query.has_table(index_conn, "summaries_fts") is False
    assert channels.summary_candidates(index_conn, "刷怪上限", path_prefix=None) == []


# ----------------------------------------------------------------------
# 融合与 `why`
# ----------------------------------------------------------------------


@requires_ripgrep
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


@requires_ripgrep
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


def test_summary_absent_without_summaries_tables(user_client: TestClient, indexed: Path) -> None:
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


def test_locate_tokens_count_only_what_locate_returns(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """`locate` 的 `tokens` 按实际返回内容计（不含 `snippet`/`summary`），不拿片段成本充数。"""
    build_big_version(settings, conn)
    body = {"query": "computeSpawnBudget", "limit": 10}
    full = post_search(user_client, body)
    located = post_search(user_client, {**body, "detail": "locate"})

    full_tokens = sum(hit["tokens"] for hit in full["hits"])
    locate_tokens = sum(hit["tokens"] for hit in located["hits"])

    # 同一个 query、同一批命中：locate 的 token 量级是 snippet 的十几分之一（见 `docs/01-api.md`）。
    assert locate_tokens * 5 <= full_tokens
    assert all(hit["tokens"] < 100 for hit in located["hits"])


def test_locate_budget_fits_more_hits_than_snippet(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """同一个 `max_tokens` 下 `locate` 装得下更多条目：预算按实际返回内容算，不被片段成本吃掉。"""
    build_big_version(settings, conn)
    body = {"query": "computeSpawnBudget", "limit": 10, "max_tokens": 200}
    full = post_search(user_client, body)
    located = post_search(user_client, {**body, "detail": "locate"})

    # `huge` 的片段自己就超预算，只因为「至少返回第一条」才在 snippet 下留下。
    assert len(full["hits"]) == 1
    assert len(located["hits"]) > len(full["hits"])
    assert sum(hit["tokens"] for hit in located["hits"]) <= 200


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


@requires_ripgrep
def test_path_prefix_filters_candidates_before_fusion(
    user_client: TestClient, indexed: Path
) -> None:
    """设前缀后 `total` 下降但 `hits` 不为空（过滤发生在候选阶段）。"""
    without = post_search(user_client, {"query": "tick"})
    with_prefix = post_search(user_client, {"query": "tick", "path_prefix": WORLD_PATH})

    assert with_prefix["total"] < without["total"]
    assert with_prefix["hits"]
    assert all(hit["path"] == WORLD_PATH for hit in with_prefix["hits"])


@requires_ripgrep
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
    """同名重载方法是不同实体：既不被并成一条，也不撞 `id`（前端列表靠它做 key）。"""
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
    assert len({hit["id"] for hit in hits}) == 2, "重载方法的 id 必须带行区间才不重复"


def test_summary_channel_hits_two_char_chinese_word(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    """两字词「刷怪」能命中摘要索引（验收）：不预分词的话 `unicode61` 永远切不出这个词。"""
    write_summaries(
        settings,
        (
            (
                "net.minecraft.world.Mob",
                "刷怪上限：每区块生成预算\n机制：mob cap\n关键成员：spawnInterval",
            ),
        ),
    )

    candidates = channels.summary_candidates(index_conn, "刷怪", path_prefix=None)

    assert [(item.symbol, item.kind, item.path) for item in candidates] == [
        ("net.minecraft.world.Mob", "class", MOB_PATH)
    ]
    assert candidates[0].summary


def test_summary_channel_ignores_chinese_punctuation(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    """query 带中文标点仍能命中：标点碎片被丢掉、词加引号，不被 FTS 语法误解析（规格 §6）。"""
    write_summaries(
        settings,
        (
            (
                "net.minecraft.world.Mob",
                "刷怪上限：每区块生成预算\n机制：mob cap\n关键成员：spawnInterval",
            ),
        ),
    )

    candidates = channels.summary_candidates(index_conn, "刷怪，上限", path_prefix=None)

    assert [item.symbol for item in candidates] == ["net.minecraft.world.Mob"]


def test_summary_channel_returns_empty_for_punctuation_only_query(
    settings: Settings, indexed: Path, index_conn: sqlite3.Connection
) -> None:
    """纯标点 query 没有可匹配的词：该路直接返回空，不去执行注定报错的 SQL。"""
    write_summaries(settings)

    assert channels.summary_candidates(index_conn, "，。！", path_prefix=None) == []


def test_summary_channel_degrades_when_not_fts5(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection, indexed: Path
) -> None:
    """`summaries_fts` 存在但不是 FTS5（建错 / 被外部改过）时该路返回空，不 500（规格 §8）。"""
    write_statements(
        settings,
        (
            "CREATE TABLE summaries (id INTEGER PRIMARY KEY, class_id INTEGER, fqn TEXT,"
            " text_zh TEXT, content_hash TEXT)",
            "CREATE TABLE summaries_fts (fqn TEXT, text_zh TEXT)",
        ),
    )

    body = post_search(user_client, {"query": "刷怪上限"})

    assert all("summary" not in hit["why"] for hit in body["hits"])


def test_summary_hit_reports_summary_in_why(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection, indexed: Path
) -> None:
    """摘要通道命中时 `why` 含 `summary`（验收，规格 §7）。"""
    write_summaries(settings)

    body = post_search(user_client, {"query": "刷怪上限"})

    assert body["hits"]
    assert body["hits"][0]["why"] == ["summary"]
    assert body["hits"][0]["symbol"] == "net.minecraft.world.Mob"
    assert body["hits"][0]["summary"]


@requires_ripgrep
def test_one_query_hits_summary_symbol_and_literal_together(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection, indexed: Path
) -> None:
    """同一个 query 能同时返回摘要、符号、字面量三路的命中（验收，承接 #6）。"""
    write_summaries(settings)

    body = post_search(user_client, {"query": "tick"})
    recalled = {channel for hit in body["hits"] for channel in hit["why"]}

    assert {"summary", "symbol", "literal"} <= recalled


def test_summary_channel_degrades_on_fts_syntax_in_query(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection, indexed: Path
) -> None:
    """query 含 FTS 语法（`"`、`*`、`-`……）时摘要路返回空、其余路照常，整体 200。

    这些字符在 FTS5 的 `MATCH` 里是语法错误（`unterminated string` 等），不是「查询不合法」：
    规格 §8 要求任何一路为空都不算错误。
    """
    write_summaries(settings)

    for raw_query in ('"', "*", "-"):
        response = user_client.post(
            "/v1/search", json={"version": VERSION, "query": raw_query}
        )

        assert response.status_code == 200, f"query={raw_query!r}：{response.text}"
        assert all("summary" not in hit["why"] for hit in response.json()["hits"])


@requires_ripgrep
def test_literal_channel_skips_a_query_without_identifiers(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection, indexed: Path
) -> None:
    """query 里一个标识符 token 都没有（纯中文 / 纯标点）时，`literal` 路不起子进程、返回空。

    旧行为把整句原样当 ripgrep 正则：中文查询在源码里恒不命中，还白耗一次 5 秒超时的子进程
    （issue #23）。标点同理——`"` 在 fixture 的 `data/*.json` 里遍地都是，那种「命中」只是噪声。
    """
    write_summaries(settings)

    for raw_query in ('"', "刷怪上限是怎么算的"):
        body = post_search(user_client, {"query": raw_query})

        assert all("literal" not in hit["why"] for hit in body["hits"]), raw_query


def test_literal_pattern_extracts_only_identifiers() -> None:
    """`literal_pattern` 只抽「像标识符的 token」：驼峰、点号路径、下划线都算，长度 ≥ 3。"""
    assert channels.literal_pattern("刷怪上限是怎么算的") is None
    assert channels.literal_pattern("，。 、") is None
    assert channels.literal_pattern("MobCategory") == "MobCategory"
    # `.`/`$` 在正则里有特殊含义，这里要的是字面量，所以逐个转义。
    assert channels.literal_pattern("Mob.spawnInterval") == "Mob\\.spawnInterval"
    assert channels.literal_pattern("刷怪上限 MobCategory 怎么算") == "MobCategory"
    # 多个 token 去重、按出现顺序用 `|` 连接；短于 3 的丢掉。
    assert channels.literal_pattern("tick tick Mob ok") == "tick|Mob"


def test_literal_candidates_do_not_spawn_ripgrep_for_chinese_queries(
    settings: Settings,
    index_conn: sqlite3.Connection,
    indexed: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯中文查询不起子进程（issue #23）：`subprocess.run` 被换成「调用即失败」。"""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("纯中文查询不该起 ripgrep 子进程")

    monkeypatch.setattr(channels.subprocess, "run", explode)

    candidates = channels.literal_candidates(
        index_conn,
        source_root(settings.data_dir, VERSION),
        "刷怪上限是怎么算的",
        path_prefix=None,
    )

    assert candidates == []


def test_literal_candidates_send_only_the_english_tokens_to_ripgrep(
    settings: Settings,
    index_conn: sqlite3.Connection,
    indexed: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中英混合查询只把英文部分送进 rg（issue #23）：整句中文不再被当正则。"""
    captured: list[list[str]] = []

    class Completed:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(args: list[str], **_kwargs: object) -> Completed:
        captured.append(list(args))

        return Completed()

    monkeypatch.setattr(channels.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(channels.subprocess, "run", fake_run)

    channels.literal_candidates(
        index_conn,
        source_root(settings.data_dir, VERSION),
        "刷怪上限 MobCategory 怎么算",
        path_prefix=None,
    )

    assert len(captured) == 1
    # `_ripgrep` 的参数末尾是 `-- <pattern> .`。
    assert captured[0][-2] == "MobCategory"


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


# ----------------------------------------------------------------------
# 多来源检索：source 过滤 + patch 通道（issue #24 规格 §5）
# ----------------------------------------------------------------------

LOADER_VERSION = "neoforge-21.1.265+1.21.1"
PATCH_PATH = "net/minecraft/world/Mob.java.patch"
PATCH_TARGET = "net/minecraft/world/Mob.java"
PATCH_FILE = """\
--- a/net/minecraft/world/Mob.java
+++ b/net/minecraft/world/Mob.java
@@ -10,3 +_,6 @@
     public void tick() {
+        this.checkDespawn();
+        this.customServerAiStep();
     }
"""


def build_multi_source(settings: Settings, conn: sqlite3.Connection) -> Path:
    """patched + loader + patch 三类来源各一份，建索引并注册成加载器版本。"""
    patched = settings.data_dir / "sources" / "patched"
    loader = settings.data_dir / "sources" / "loader"
    patch_dir = settings.data_dir / "sources" / "patches"

    for relative, content in {
        "net/minecraft/world/Mob.java": (
            "package net.minecraft.world;\n\npublic class Mob {\n    public void tick() {}\n}\n"
        ),
        "net/minecraft/world/World.java": (
            "package net.minecraft.world;\n\npublic class World {\n    public void tick() {}\n}\n"
        ),
    }.items():
        path = patched / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    loader_file = loader / "net" / "neoforged" / "EventBus.java"
    loader_file.parent.mkdir(parents=True, exist_ok=True)
    loader_file.write_text(
        "package net.neoforged;\n\npublic class EventBus {\n"
        "    public void tick() {}\n\n    public void post() {}\n}\n",
        encoding="utf-8",
    )

    patch_file = patch_dir / PATCH_PATH
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(PATCH_FILE, encoding="utf-8")

    index = index_path(settings.data_dir, LOADER_VERSION)
    result = build_index_from_sources(
        [
            SourceSpec(kind="patched", root=patched),
            SourceSpec(kind="loader", root=loader),
            SourceSpec(kind="patch", root=patch_dir),
        ],
        index,
        log=silent,
    )
    # 与 `parse` 阶段同口径：Java 源码就位到 `src/<version>/`（literal 通道据此扫源码）。
    destination = source_root(settings.data_dir, LOADER_VERSION)
    place_source_tree(patched, destination)
    place_source_tree(loader, destination)
    store.register_version(
        conn,
        version=LOADER_VERSION,
        files=result.files,
        tokens=result.tokens,
        obfuscated=False,
        has_summaries=False,
        source_size_bytes=0,
        db_path=str(index),
    )
    conn.commit()

    return index


@pytest.fixture
def multi_index(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> Path:
    return build_multi_source(settings, conn)


def search_multi(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/v1/search", json={"version": LOADER_VERSION, **body})

    assert response.status_code == 200, response.text

    return cast("dict[str, Any]", response.json())


def test_patch_channel_returns_hunks_with_target_and_new_lines(
    settings: Settings, multi_index: Path
) -> None:
    """patch 通道查 `patches_fts`，命中给补丁路径 + target + 新行号区间（规格 §5）。"""
    connection = query.open_readonly(multi_index)

    try:
        candidates = channels.patch_candidates(
            connection, "checkDespawn", path_prefix=None, source=None
        )
    finally:
        connection.close()

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.channel == "patch"
    assert candidate.kind == "patch"
    assert candidate.path == PATCH_PATH
    assert candidate.target == PATCH_TARGET
    assert (candidate.start_line, candidate.end_line) == (10, 15)
    assert candidate.patch_context is not None and "checkDespawn" in candidate.patch_context


def test_patch_channel_respects_source_filter(settings: Settings, multi_index: Path) -> None:
    connection = query.open_readonly(multi_index)

    try:
        assert channels.patch_candidates(
            connection, "checkDespawn", path_prefix=None, source="patched"
        ) == []
        assert channels.patch_candidates(
            connection, "checkDespawn", path_prefix=None, source="patch"
        )
    finally:
        connection.close()


def test_search_source_filter_separates_patched_and_loader(
    client: TestClient, multi_index: Path
) -> None:
    """`source` 过滤把两类来源分开：同一个 `tick` 查询按来源各回各的类。"""
    def symbols(body: dict[str, Any]) -> list[str]:
        return [
            str(hit["symbol"])
            for hit in search_multi(client, body)["hits"]
            if hit.get("symbol")
        ]

    everything = symbols({"query": "tick", "detail": "locate", "limit": 20})
    patched = symbols(
        {"query": "tick", "detail": "locate", "limit": 20, "source": "patched"}
    )
    loader = symbols(
        {"query": "tick", "detail": "locate", "limit": 20, "source": "loader"}
    )

    assert any(symbol.startswith("net.minecraft.world.Mob") for symbol in everything)
    assert any(symbol.startswith("net.neoforged.EventBus") for symbol in everything)
    assert all(
        not symbol.startswith("net.neoforged.") for symbol in patched
    ), "patched 过滤不该回加载器的类"
    assert all(
        not symbol.startswith("net.minecraft.") for symbol in loader
    ), "loader 过滤不该回 MC 的类"


def test_search_hits_patch_hunk_with_chinese_summary(
    client: TestClient, settings: Settings, multi_index: Path
) -> None:
    """中文提问命中补丁 hunk：`why` 含 `patch`，`type=patch`，带 `target` 与 hunk 正文。"""
    summarize.write_summaries(
        multi_index,
        [
            summarize.SummaryRow(
                class_id=None,
                fqn=PATCH_PATH,
                text_zh=(
                    "职责：改掉 Mob 的刷怪与掉落钩子\n"
                    "机制：在 tick 里插入自定义 AI，受区块加载状态约束\n"
                    "关键成员：Mob, checkDespawn"
                ),
                content_hash="h",
                source="patch",
            )
        ],
    )
    summarize.refresh_patch_fts(multi_index)

    response = search_multi(client, {"query": "刷怪 掉落 钩子", "detail": "snippet"})
    patches = [hit for hit in response["hits"] if hit["type"] == "patch"]

    assert patches, response
    hit = patches[0]
    assert "patch" in hit["why"]
    assert hit["path"] == PATCH_PATH
    assert hit["target"] == PATCH_TARGET
    assert hit["lines"] == [10, 15]
    assert hit["snippet"] is not None and "checkDespawn" in hit["snippet"]


def test_patch_hit_without_summaries_still_matches_identifiers(
    client: TestClient, multi_index: Path
) -> None:
    """没跑过摘要时补丁通道仍能被标识符命中（`patches_fts` 先只有 diff 正文）。"""
    response = search_multi(client, {"query": "checkDespawn", "detail": "locate"})

    assert any(hit["type"] == "patch" and "patch" in hit["why"] for hit in response["hits"])
