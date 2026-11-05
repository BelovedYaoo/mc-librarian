"""MCP 端点（`/mcp`）：鉴权、工具清单、路径与工具调用。

不联网、不起真端口：`TestClient` 会驱动完整的 ASGI 栈，包括 MCP 的 streamable HTTP 传输
（`initialize` → `notifications/initialized` → `tools/list` / `tools/call`）。

**为什么不用官方 MCP 客户端 SDK 写这些用例**：SDK 的客户端要真 socket + 真事件循环，
在 pytest 里会把测试变成「先起一个服务器」。这里手写 JSON-RPC 报文更直接，也顺带把
「客户端必须回传 `mcp-session-id`」这条协议要求显式钉在测试里。
"""

from __future__ import annotations

import json
import shutil
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.indexing.build import build_index
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store

# MCP 要求客户端声明能接受 SSE（服务端回的是 `text/event-stream`）。
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}


JAVA_SOURCE = """package net.minecraft.world.entity;

public class MobCategory {
   private int maxInstancesPerChunk;

   public int getMaxInstancesPerChunk() {
      return this.maxInstancesPerChunk;
   }
}
"""


def _silent(_level: str, _message: str) -> None:
    pass


def _register_index(settings: Settings, conn: Any, version: str) -> None:
    """建一份最小索引并注册版本——注册端点要求索引文件真实存在。

    只放一个类：这些用例验的是 MCP 的**协议层**（鉴权 / 工具清单 / error JSON），
    检索质量由 `test_search.py` 那一套覆盖，不必在这里重复铺 fixture。
    """
    root = source_root(settings.data_dir, version)
    target = root / "net/minecraft/world/entity/MobCategory.java"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(JAVA_SOURCE, encoding="utf-8")
    index = index_path(settings.data_dir, version)
    result = build_index(root, index, log=_silent)
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


def _issue_pat(admin_client: TestClient) -> str:
    """建一个 PAT。PAT 是 MCP 唯一的凭据（没有 session cookie 这条路）。"""
    response = admin_client.post("/v1/auth/tokens", json={"name": "mcp", "expires_in_days": 1})

    assert response.status_code == 201, response.text

    return str(response.json()["token"])


def _sse_json(response: Any) -> dict[str, Any]:
    """从 SSE 响应体里取出 JSON-RPC 消息。

    服务端回的是 `event: message\\r\\ndata: {...}`；一次响应可能有多帧，这里取第一条带 `result`
    或 `error` 的。纯 JSON 响应（`json_response=True` 时）也兼容。
    """
    text = response.text

    if response.headers.get("content-type", "").startswith("application/json"):
        return dict(json.loads(text))

    for block in text.replace("\r\n", "\n").split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])

                if "result" in payload or "error" in payload:
                    return dict(payload)

    raise AssertionError(f"响应里没有 JSON-RPC 消息：{text[:400]}")


def _open_session(client: TestClient, pat: str) -> tuple[str, dict[str, str]]:
    """走完 `initialize`，返回 `(session_id, 带会话头的后续请求头)`。"""
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {pat}"}
    response = client.post("/mcp", json=INITIALIZE, headers=headers)

    assert response.status_code == 200, response.text

    session_id = response.headers.get("mcp-session-id")

    assert session_id, "initialize 的响应必须带 mcp-session-id"

    # 协议要求：initialize 之后客户端发 `notifications/initialized`（通知无 id）。
    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**headers, "mcp-session-id": session_id},
    )

    assert initialized.status_code == 202, initialized.text

    return session_id, {**headers, "mcp-session-id": session_id}


def _call(
    client: TestClient, headers: dict[str, str], method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 99, "method": method}

    if params is not None:
        body["params"] = params

    response = client.post("/mcp", json=body, headers=headers)

    assert response.status_code == 200, response.text

    return _sse_json(response)


def test_requires_bearer_token(client: TestClient) -> None:
    """没有凭据一律 401，且不区分「令牌不存在」与「令牌已吊销」。"""
    response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_rejects_unknown_token(client: TestClient) -> None:
    response = client.post(
        "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer mcl_bogus"}
    )

    assert response.status_code == 401


def test_rejects_session_cookie(admin_client: TestClient) -> None:
    """MCP 只认 PAT：浏览器会话 cookie 不能拿来调它（它不是网页端的一部分）。"""
    response = admin_client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401


def test_no_trailing_slash_redirect(admin_client: TestClient) -> None:
    """`/mcp` 与 `/mcp/` 都必须直接 200——POST 跟随 307 重定向不可靠。"""
    pat = _issue_pat(admin_client)
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {pat}"}

    for path in ("/mcp", "/mcp/"):
        response = admin_client.post(path, json=INITIALIZE, headers=headers)

        assert response.status_code == 200, f"{path} → {response.status_code}"


def test_lists_seven_tools(admin_client: TestClient) -> None:
    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(admin_client, headers, "tools/list")

    names = {tool["name"] for tool in result["result"]["tools"]}

    assert names == {"versions", "search", "symbol", "outline", "read", "references", "ask"}


def test_versions_tool_reports_registered_versions(
    admin_client: TestClient, settings: Settings, conn: Any
) -> None:
    """`versions` 是模型唯一能知道合法版本串的入口，必须能列出已注册版本。"""
    _register_index(settings, conn, "1.21.1")

    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(admin_client, headers, "tools/call", {"name": "versions", "arguments": {}})
    payload = json.loads(result["result"]["content"][0]["text"])

    assert [item["version"] for item in payload["versions"]] == ["1.21.1"]
    # 加载器元数据三列在契约里有；原版版本应为 None（不是空字符串）。
    assert payload["versions"][0]["loader"] is None
    # 「注册了」不等于「能检索」：这份索引真实存在，所以要标 searchable 并进顶层清单。
    assert payload["versions"][0]["searchable"] is True
    assert payload["searchable_versions"] == ["1.21.1"]
    # 这份 fixture 只写了源码树 + 建索引，两者都在 → 可检索也可读。
    assert payload["versions"][0]["sources_available"] is True
    assert payload["readable_versions"] == ["1.21.1"]


def test_versions_marks_registered_without_index_as_unsearchable(
    admin_client: TestClient, conn: Any
) -> None:
    """版本行存在但 `db_path` 为空时，`versions` 必须标 `searchable=false`。

    否则模型会挑一个「在列表里但查不到」的版本，然后来回失败——这正是这个工具要防的。
    """
    store.register_version(
        conn,
        version="9.9.9",
        files=0,
        tokens=0,
        obfuscated=False,
        has_summaries=False,
        source_size_bytes=0,
        db_path=None,
    )
    conn.commit()

    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(admin_client, headers, "tools/call", {"name": "versions", "arguments": {}})
    payload = json.loads(result["result"]["content"][0]["text"])

    entry = next(item for item in payload["versions"] if item["version"] == "9.9.9")

    assert entry["searchable"] is False
    assert "db_path" in entry["unavailable_reason"]
    assert "9.9.9" not in payload["searchable_versions"]


def test_versions_reports_missing_source_tree(
    admin_client: TestClient, settings: Settings, conn: Any
) -> None:
    """索引传了、源码树没传时：`searchable=true` 但 `sources_available=false`。

    这是最容易白跑一轮的配置错：`search` 能用，`read`/`outline` 必然失败。提前标出来。
    """
    _register_index(settings, conn, "1.21.1")
    shutil.rmtree(source_root(settings.data_dir, "1.21.1"))

    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(admin_client, headers, "tools/call", {"name": "versions", "arguments": {}})
    payload = json.loads(result["result"]["content"][0]["text"])

    entry = payload["versions"][0]

    assert entry["searchable"] is True
    assert entry["sources_available"] is False
    assert payload["searchable_versions"] == ["1.21.1"]
    assert payload["readable_versions"] == []


def test_search_error_lists_searchable_versions(
    admin_client: TestClient, settings: Settings, conn: Any
) -> None:
    """版本写错时错误信息里**直接给可用清单**，模型不必再多调一次 `versions`。"""
    _register_index(settings, conn, "1.21.1")

    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(
        admin_client,
        headers,
        "tools/call",
        {"name": "search", "arguments": {"query_text": "刷怪", "version": "9.9.9"}},
    )
    payload = json.loads(result["result"]["content"][0]["text"])

    assert "error" in payload
    assert "1.21.1" in payload["error"]


def test_search_tool_returns_error_for_unknown_version(admin_client: TestClient) -> None:
    """版本不存在时回**可读的 error JSON**，不是异常——模型要能据此自我纠正。"""
    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(
        admin_client,
        headers,
        "tools/call",
        {"name": "search", "arguments": {"query_text": "刷怪", "version": "9.9.9"}},
    )
    payload = json.loads(result["result"]["content"][0]["text"])

    assert "error" in payload
    assert "9.9.9" in payload["error"]


def test_invalid_source_is_reported_as_error(admin_client: TestClient) -> None:
    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(
        admin_client,
        headers,
        "tools/call",
        {
            "name": "search",
            "arguments": {"query_text": "刷怪", "version": "1.21.1", "source": "bogus"},
        },
    )
    payload = json.loads(result["result"]["content"][0]["text"])

    assert "error" in payload


@pytest.mark.parametrize(("start", "end"), [(0, 10), (10, 5)])
def test_read_tool_rejects_bad_line_range(
    admin_client: TestClient, start: int, end: int
) -> None:
    """`read` 必须显式给行号区间；非法区间回 error 而不是读整个文件。"""
    pat = _issue_pat(admin_client)
    _session, headers = _open_session(admin_client, pat)
    result = _call(
        admin_client,
        headers,
        "tools/call",
        {
            "name": "read",
            "arguments": {"path": "a.java", "version": "1.21.1", "start": start, "end": end},
        },
    )
    payload = json.loads(result["result"]["content"][0]["text"])

    assert "error" in payload


def test_mcp_path_does_not_break_http_api(admin_client: TestClient) -> None:
    """挂 MCP 时用的全局中间件不能把 `/v1/*` 拦掉。"""
    assert admin_client.get("/v1/health").status_code == 200
    assert admin_client.get("/v1/versions").status_code == 200


