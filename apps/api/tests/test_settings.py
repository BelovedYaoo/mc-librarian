"""实例设置与密钥（issue #11）：迁移、部分更新、密钥往返、立即生效、审计。

逐条对应验收标准：保存后立即生效（不需要重启）/ 读取只回 `api_key_set` /
主密钥缺失时启动失败 / 关闭问答后 `/v1/ask` 回 429 且检索不受影响 / 设置变更写审计。

数据根一律用 `tmp_path`（`settings` fixture）；密钥在库里必须是密文，断言直接读控制面库。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, TEST_SECRET, FakeChat, tool_call
from mc_librarian import secretbox, settings_store
from mc_librarian.config import Settings, resolve_alerts_config, resolve_llm_config
from mc_librarian.db import Database
from mc_librarian.indexing.build import build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store
from mc_librarian.main import create_app
from mc_librarian.schemas import SettingsUpdate
from mc_librarian.secretbox import SecretError

API_KEY = "sk-live-0123456789abcdef"
WEBHOOK_URL = "https://hooks.example.invalid/services/abc/def"

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"
VERSION = "26.3"


def login_admin(client: TestClient) -> None:
    created = client.post(
        "/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )

    assert created.status_code == 201, created.text


def settings_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """控制面库里的 `settings` 行，按 key 索引。"""
    return {
        str(row["key"]): row
        for row in conn.execute("SELECT key, value_json, encrypted FROM settings")
    }


def put(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.put("/v1/admin/settings", json=payload)

    assert response.status_code == 200, response.text

    body: dict[str, Any] = response.json()

    return body


def tools_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", message["tool_calls"])


def parse_sse(text: str) -> list[dict[str, Any]]:
    """SSE 文本 → 事件 payload 列表（与 `test_ask.py` 同一口径）。"""
    events: list[dict[str, Any]] = []

    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue

        data = next(
            line[len("data: ") :] for line in block.splitlines() if line.startswith("data: ")
        )
        events.append(json.loads(data))

    return events


def register_fixture_index(settings: Settings, conn: sqlite3.Connection) -> None:
    """就位 fixture 源码 → 建索引 → 注册版本（`/v1/ask` 需要可检索的版本）。"""
    root = source_root(settings.data_dir, VERSION)
    place_source_tree(FIXTURE_SOURCE, root)
    index = index_path(settings.data_dir, VERSION)
    result = build_index(root, index, log=lambda _level, _message: None)

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


# ----------------------------------------------------------------------
# 主密钥
# ----------------------------------------------------------------------


def test_missing_secret_fails_startup(tmp_path: Path) -> None:
    """主密钥缺失 → 服务启动失败并给出明确错误（验收第 3 条），不自动生成。"""
    settings = Settings(data_dir=tmp_path / "data", secret=None)

    with (
        pytest.raises(SecretError, match="MC_LIBRARIAN_SECRET"),
        TestClient(create_app(settings)),
    ):
        pass


def test_short_secret_fails_startup(tmp_path: Path) -> None:
    """长度不足同样启动失败：太短的主密钥可暴力枚举。"""
    settings = Settings(data_dir=tmp_path / "data", secret="short")

    with pytest.raises(SecretError, match="至少"), TestClient(create_app(settings)):
        pass


def test_secret_box_roundtrip_rejects_other_key() -> None:
    """密文往返：换了主密钥就解不开（明确报错，不静默返回空串）。"""
    token = secretbox.encrypt(TEST_SECRET, API_KEY)

    assert token != API_KEY
    assert secretbox.decrypt(TEST_SECRET, token) == API_KEY

    with pytest.raises(SecretError):
        secretbox.decrypt("another-secret-0123456789", token)


# ----------------------------------------------------------------------
# 读取：配置派生 + 数据库覆盖
# ----------------------------------------------------------------------


def test_get_returns_derived_values_without_overrides(
    admin_client: TestClient, settings: Settings
) -> None:
    """库里没有覆盖时全部从环境配置派生；未配置的可选字段整字段省略（不填假默认值）。"""
    body = admin_client.get("/v1/admin/settings").json()

    assert set(body) == {"llm", "ask", "index", "security", "alerts"}
    assert body["llm"]["base_url"] == settings.llm_base_url
    assert body["llm"]["model"] == settings.llm_model
    assert body["llm"]["api_key_set"] is True
    assert body["ask"] == {
        "max_steps": settings.ask_max_steps,
        "max_answer_tokens": settings.ask_max_answer_tokens,
        "max_context_tokens": settings.ask_max_context_tokens,
        "timeout_s": settings.ask_timeout_s,
        "enabled": True,
    }
    assert body["alerts"] == {"webhook_url_set": False}, "未配置的阈值不回 0，整字段省略"


def test_get_omits_unset_llm_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 一项都没配时 `llm` 块里没有 `base_url`/`model`：省略比假值诚实。"""
    for name in ("MC_LIBRARIAN_LLM_BASE_URL", "MC_LIBRARIAN_LLM_MODEL", "MC_LIBRARIAN_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with TestClient(create_app(settings)) as client:
        login_admin(client)
        body = client.get("/v1/admin/settings").json()

    assert body["llm"] == {"temperature": 0.0, "timeout_s": 120.0, "api_key_set": False}


def test_override_wins_over_environment(
    admin_client: TestClient, settings: Settings
) -> None:
    """库里有覆盖用库里的；清掉覆盖就回退环境配置（不是留一个空值）。"""
    put(admin_client, {"llm": {"base_url": "http://db-override.invalid/v1"}})

    assert (
        admin_client.get("/v1/admin/settings").json()["llm"]["base_url"]
        == "http://db-override.invalid/v1"
    )

    put(admin_client, {"llm": {"base_url": None}})

    assert admin_client.get("/v1/admin/settings").json()["llm"]["base_url"] == settings.llm_base_url


# ----------------------------------------------------------------------
# 部分更新
# ----------------------------------------------------------------------


def test_partial_update_keeps_other_values(
    admin_client: TestClient, settings: Settings
) -> None:
    """只改请求里给的字段，其余保持原值——前端可以只提交一个开关。"""
    put(admin_client, {"ask": {"timeout_s": 30}})

    body = admin_client.get("/v1/admin/settings").json()

    assert body["ask"]["timeout_s"] == 30
    assert body["ask"]["max_steps"] == settings.ask_max_steps
    assert body["ask"]["enabled"] is True
    assert body["index"]["java_xmx"] == settings.decompile_memory


def test_partial_update_spans_blocks(admin_client: TestClient) -> None:
    """一次 PUT 可以同时改多个块，块内也只改给出的字段。"""
    body = put(
        admin_client,
        {
            "ask": {"enabled": False, "max_steps": 3},
            "index": {"java_xmx": "2G", "summary_concurrency": 8},
            "security": {"session_ttl_hours": 12},
            "alerts": {"daily_token_threshold": 1_000_000},
        },
    )

    assert body["ask"]["enabled"] is False
    assert body["ask"]["max_steps"] == 3
    assert body["index"]["java_xmx"] == "2G"
    assert body["index"]["summary_concurrency"] == 8
    assert body["security"]["session_ttl_hours"] == 12
    assert body["alerts"]["daily_token_threshold"] == 1_000_000
    # 没给的字段保持派生值；未配置的可选字段直接省略（不填假默认值）。
    assert body["ask"]["timeout_s"] == 120
    assert body["index"]["vineflower_jar"] == ""


def test_empty_body_changes_nothing(admin_client: TestClient, settings: Settings) -> None:
    """空对象是合法的部分更新（不改任何字段），但 PUT 本身仍然记账。"""
    body = put(admin_client, {})

    assert body["ask"]["timeout_s"] == settings.ask_timeout_s


def test_null_block_is_rejected(admin_client: TestClient) -> None:
    """整块传 `null` 没有语义（部分更新用「不出现」表达）：422，不静默当成没改。"""
    assert admin_client.put("/v1/admin/settings", json={"llm": None}).status_code == 422


def test_index_enabled_is_writable(admin_client: TestClient) -> None:
    """`index.enabled` 现在可从设置页关掉（改完立即作用于 `POST /v1/admin/jobs`）。"""
    body = put(admin_client, {"index": {"enabled": False}})

    assert body["index"]["enabled"] is False

    assert admin_client.get("/v1/admin/settings").json()["index"]["enabled"] is False


def test_extracted_environment_settings_are_page_configurable(
    admin_client: TestClient, settings: Settings
) -> None:
    """6 项原仅环境变量的配置已提取到设置页：GET 里有，PUT 后 GET 读回新值。"""
    body = admin_client.get("/v1/admin/settings").json()

    assert body["index"]["data_dir"] == str(settings.data_dir), "注册对话框要显示数据根绝对路径"

    for block, field in (
        ("llm", "timeout_s"),
        ("ask", "max_context_tokens"),
        ("index", "enabled"),
        ("index", "java"),
        ("index", "vineflower_jar"),
        ("index", "specialsource_jar"),
    ):
        assert field in body[block], f"{block}.{field} 应出现在 GET 响应里"

    put(
        admin_client,
        {
            "llm": {"timeout_s": 30.5},
            "ask": {"max_context_tokens": 12345},
            "index": {
                "enabled": False,
                "java": "/opt/jdk/bin/java",
                "vineflower_jar": "/opt/vineflower.jar",
                "specialsource_jar": "/opt/specialsource.jar",
            },
        },
    )

    updated = admin_client.get("/v1/admin/settings").json()

    assert updated["llm"]["timeout_s"] == 30.5
    assert updated["ask"]["max_context_tokens"] == 12345
    assert updated["index"]["enabled"] is False
    assert updated["index"]["java"] == "/opt/jdk/bin/java"
    # 路径类字段后端转 `Path`：Windows 上分隔符会被规范化，按 `str(Path(...))` 比。
    assert updated["index"]["vineflower_jar"] == str(Path("/opt/vineflower.jar"))
    assert updated["index"]["specialsource_jar"] == str(Path("/opt/specialsource.jar"))

    # 空字符串 = 清除覆盖：路径字段重新省略（回退环境变量）。
    cleared = put(
        admin_client,
        {"index": {"vineflower_jar": "", "specialsource_jar": ""}},
    )

    assert cleared["index"]["vineflower_jar"] == ""
    assert cleared["index"]["specialsource_jar"] == ""


def test_unknown_field_is_rejected(admin_client: TestClient) -> None:
    """拼错的字段名 422，而不是静默什么都不做。"""
    response = admin_client.put("/v1/admin/settings", json={"llm": {"base_uri": "http://x"}})

    assert response.status_code == 422, response.text


def test_invalid_value_is_rejected(admin_client: TestClient) -> None:
    """取值域由契约定：`java_xmx` 必须形如 4G / 512M。"""
    response = admin_client.put("/v1/admin/settings", json={"index": {"java_xmx": "4GB"}})

    assert response.status_code == 422, response.text


def test_settings_require_admin(user_client: TestClient) -> None:
    """非 admin 一律 403（前端隐藏入口只是体验）。"""
    assert user_client.get("/v1/admin/settings").status_code == 403

    denied = user_client.put("/v1/admin/settings", json={"ask": {"enabled": False}})

    assert denied.status_code == 403


# ----------------------------------------------------------------------
# 密钥只写不读
# ----------------------------------------------------------------------


def test_api_key_roundtrip_is_encrypted_in_db(
    admin_client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    """写入后响应里只有 `api_key_set`，库里是密文（不是明文），且读取路径能解出来。"""
    body = put(admin_client, {"llm": {"api_key": API_KEY}})

    assert body["llm"]["api_key_set"] is True
    assert "api_key" not in body["llm"], "任何情况下都不回明文或密文"

    row = settings_rows(conn)["llm.api_key"]

    assert row["encrypted"] == 1
    assert API_KEY not in str(row["value_json"]), "库里不能是明文"

    effective = settings_store.effective_settings(
        settings, settings_store.read_overrides(conn, settings)
    )

    assert effective.llm_api_key == API_KEY
    assert resolve_llm_config(effective).api_key == API_KEY


def test_empty_string_clears_the_api_key(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    """传空字符串 = 清除该密钥：覆盖行被删掉（库里的值不再存在）。"""
    put(admin_client, {"llm": {"api_key": API_KEY}})
    body = put(admin_client, {"llm": {"api_key": ""}})

    assert "llm.api_key" not in settings_rows(conn)
    # conftest 的 settings 配了环境变量密钥，所以清除覆盖后回退成「已设置」。
    assert body["llm"]["api_key_set"] is True


def test_clearing_api_key_falls_back_to_environment(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """环境变量没配密钥时，清除覆盖后 `api_key_set` 就是 false。"""
    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with TestClient(create_app(settings)) as client:
        login_admin(client)
        assert client.get("/v1/admin/settings").json()["llm"]["api_key_set"] is False
        assert put(client, {"llm": {"api_key": API_KEY}})["llm"]["api_key_set"] is True
        assert put(client, {"llm": {"api_key": ""}})["llm"]["api_key_set"] is False


def test_webhook_url_is_write_only_and_encrypted(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    """`alerts.webhook_url` 同样只写不读，且库里是密文（URL 里带 token，算密钥）。"""
    body = put(admin_client, {"alerts": {"webhook_url": WEBHOOK_URL}})

    assert body["alerts"]["webhook_url_set"] is True
    assert "webhook_url" not in body["alerts"]

    row = settings_rows(conn)["alerts.webhook_url"]

    assert row["encrypted"] == 1
    assert WEBHOOK_URL not in str(row["value_json"])


def test_resolve_alerts_config_reads_database_overrides(
    admin_client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    """`resolve_alerts_config(settings, conn)` 是 `#12` 的入口：带数据库覆盖，未配置的阈值是 0。"""
    empty = resolve_alerts_config(settings, conn)

    assert empty.webhook_url is None
    assert empty.daily_token_threshold == 0
    assert empty == resolve_alerts_config(settings), "库里没有覆盖时，带 conn 与不带 conn 等价"

    put(
        admin_client,
        {"alerts": {"webhook_url": WEBHOOK_URL, "daily_token_threshold": 500_000}},
    )

    configured = resolve_alerts_config(settings, conn)

    assert configured.webhook_url == WEBHOOK_URL
    assert configured.daily_token_threshold == 500_000


# ----------------------------------------------------------------------
# 立即生效（不需要重启）
# ----------------------------------------------------------------------


def test_disabling_ask_takes_effect_immediately(admin_client: TestClient) -> None:
    """`PUT` 关掉问答后，**同一个进程**里 `/v1/ask` 立刻 429（验收第 1、4 条）。"""
    asked = {"question": "刷怪上限在哪？", "version": "1.20.1"}

    assert admin_client.post("/v1/ask", json=asked).status_code != 429

    put(admin_client, {"ask": {"enabled": False}})

    response = admin_client.post("/v1/ask", json=asked)

    assert response.status_code == 429, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "暂停" in response.json()["detail"]

    # 检索不受影响：问答开关只挡 `/v1/ask`（验收第 4 条）。
    search = admin_client.post("/v1/search", json={"query": "x", "version": "1.20.1"})

    assert search.status_code != 429


def test_max_concurrent_asks_change_takes_effect_immediately(
    admin_client: TestClient, settings: Settings
) -> None:
    """并发上限同样实时读：改成 1 后第二个问答立刻 429（而不是等重启）。"""
    put(admin_client, {"security": {"max_concurrent_asks": 1}})

    limiter = admin_client.app.state.ask_limiter  # type: ignore[attr-defined]

    assert limiter.try_acquire(1) is True

    try:
        response = admin_client.post("/v1/ask", json={"question": "x", "version": "1.20.1"})

        assert response.status_code == 429, response.text
        assert "并发" in response.json()["detail"]
    finally:
        limiter.release()


def test_llm_config_change_takes_effect_immediately(
    admin_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """模型地址/密钥改完立刻用于下一次请求（读的是数据库覆盖，不是启动时快照）。"""
    put(
        admin_client,
        {"llm": {"base_url": "http://db-llm.invalid/v1", "model": "db-model", "api_key": API_KEY}},
    )

    effective = settings_store.effective_settings(
        settings, settings_store.read_overrides(conn, settings)
    )
    config = resolve_llm_config(effective)

    assert config.base_url == "http://db-llm.invalid/v1"
    assert config.model == "db-model"
    assert config.api_key == API_KEY


def test_ask_request_uses_server_side_defaults(
    admin_client: TestClient, settings: Settings, conn: sqlite3.Connection, fake_chat: FakeChat
) -> None:
    """`AskRequest` 省略这两个字段时用设置页的值（不是硬编码的 6 / 8000）。"""
    register_fixture_index(settings, conn)
    put(admin_client, {"ask": {"max_steps": 2, "max_answer_tokens": 1500}})
    fake_chat.always_tools = tools_of(tool_call("search", query="Mob", detail="locate"))

    response = admin_client.post("/v1/ask", json={"question": "Mob", "version": VERSION})

    assert response.status_code == 200, response.text

    events = parse_sse(response.text)
    executed = [event for event in events if event["type"] == "step" and "ok" in event]

    assert len(executed) == 2, "跑满 2 步就强制收尾（用硬编码的 6 会是 6 步）"
    assert events[-1]["reason"] == "max_steps"
    # 两次带工具的调用 + 一次强制收尾，`max_answer_tokens` 每次都取设置页的值。
    assert [call["max_tokens"] for call in fake_chat.calls] == [1500, 1500, 1500]


# ----------------------------------------------------------------------
# 审计
# ----------------------------------------------------------------------


def test_update_is_audited_without_values(admin_client: TestClient) -> None:
    """审计只记「改了哪些键」，不记值——密钥更不能进审计（验收第 5 条）。"""
    put(admin_client, {"llm": {"api_key": API_KEY, "model": "db-model"}, "ask": {"enabled": False}})

    entries = admin_client.get("/v1/admin/audit").json()["entries"]
    updated = [entry for entry in entries if entry["action"] == "settings.updated"]

    assert updated, "PUT 必须写一条 settings.updated"
    entry = updated[0]

    assert entry["actor"] == ADMIN_USERNAME
    assert sorted(entry["detail"]["keys"]) == ["ask.enabled", "llm.api_key", "llm.model"]
    assert API_KEY not in json.dumps(entry, ensure_ascii=False), "审计里不能出现密钥"


def test_noop_update_is_audited_with_empty_keys(admin_client: TestClient) -> None:
    """重复写同一个值不算改动：审计记空键列表，不编「改了」的假象，也不记值。"""
    put(admin_client, {"ask": {"timeout_s": 45}})
    put(admin_client, {"ask": {"timeout_s": 45}})

    entries = admin_client.get("/v1/admin/audit").json()["entries"]
    updated = [entry for entry in entries if entry["action"] == "settings.updated"]

    assert updated[0]["detail"] == {"keys": []}
    assert updated[1]["detail"] == {"keys": ["ask.timeout_s"]}


def test_update_records_actor_in_database(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    """`settings.updated_by` 记的是改它的管理员。"""
    put(admin_client, {"ask": {"timeout_s": 45}})

    row = settings_rows(conn)["ask.timeout_s"]
    actor = conn.execute(
        "SELECT username FROM users WHERE id = (SELECT updated_by FROM settings WHERE key = ?)",
        ("ask.timeout_s",),
    ).fetchone()

    assert actor is not None
    assert actor["username"] == ADMIN_USERNAME
    assert json.loads(str(row["value_json"])) == 45


# ----------------------------------------------------------------------
# 存储层
# ----------------------------------------------------------------------


def test_overrides_are_field_keyed(tmp_path: Path) -> None:
    """`read_overrides` 返回的是 `Settings` 字段名（直接喂 `effective_settings`）。"""
    database = Database.for_data_dir(tmp_path / "data")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with database.connect() as conn:
        assert settings_store.read_overrides(conn, settings) == {}

        conn.execute(
            "INSERT INTO settings (key, value_json, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            (settings_store.KEY_ASK_TIMEOUT_S, "15", "2026-01-01T00:00:00Z"),
        )
        overrides = settings_store.read_overrides(conn, settings)

    assert overrides == {"ask_timeout_s": 15}


def test_unknown_keys_in_database_are_skipped(tmp_path: Path) -> None:
    """库里可能有旧版本写入、当前已不支持的键：跳过它，不影响其它设置生效。"""
    database = Database.for_data_dir(tmp_path / "data")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with database.connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value_json, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("legacy.removed", "true", "2026-01-01T00:00:00Z"),
        )

        assert settings_store.read_overrides(conn, settings) == {}


def test_corrupt_override_is_reported_not_ignored(tmp_path: Path) -> None:
    """手工改坏的值在读取时明确报错，不会带着坏值一路跑到 java 命令行。"""
    database = Database.for_data_dir(tmp_path / "data")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with database.connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value_json, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            (settings_store.KEY_INDEX_JAVA_XMX, json.dumps("4GB"), "2026-01-01T00:00:00Z"),
        )

        with pytest.raises(Exception, match="decompile_memory"):
            settings_store.effective_settings(
                settings, settings_store.read_overrides(conn, settings)
            )


def test_every_spec_maps_to_real_fields() -> None:
    """`BLOCK_SPECS` 与两侧一一对应：写错字段名会在运行时 KeyError，这里先挡住。

    三个方向都要对得上：更新模型的块名/字段名、`config.Settings` 的字段名、契约键。
    """
    payload = SettingsUpdate()

    for block_name, specs in settings_store.BLOCK_SPECS.items():
        block = getattr(payload, block_name)

        assert set(specs) <= set(type(block).model_fields), (
            f"{block_name} 的字段名与更新模型不一致"
        )

        for spec in specs.values():
            assert spec.field in Settings.model_fields, f"{spec.key} → Settings.{spec.field} 不存在"
            assert spec.key.startswith(f"{block_name}."), f"{spec.key} 与块 {block_name} 不匹配"

    keys = [spec.key for specs in settings_store.BLOCK_SPECS.values() for spec in specs.values()]

    assert len(set(keys)) == len(settings_store.SPECS_BY_KEY)


def test_settings_table_is_created_by_migration(tmp_path: Path) -> None:
    """迁移建表（`docs/00-overview.md` §5.1）：key / value_json / encrypted + 记账列。"""
    database = Database.for_data_dir(tmp_path / "data")
    version = database.initialize()

    assert version >= 7

    with database.connect() as conn:
        columns = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA table_info(settings)")
        }

    assert set(columns) == {"key", "value_json", "encrypted", "updated_at", "updated_by"}
    assert columns["key"]["pk"] == 1
    assert columns["value_json"]["notnull"] == 1
    assert columns["encrypted"]["dflt_value"] == "0"
