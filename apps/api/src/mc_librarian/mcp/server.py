"""MCP 服务（streamable HTTP）：把检索能力以工具形式暴露给外部 agent。

七个工具全部转调 `mc_librarian.retrieval.service` 与 `mc_librarian.answer`——**不另写检索逻辑**，
否则 MCP 的召回行为会与 `/v1/search` 慢慢漂移（同一份问题在两个入口给出不同结果）。

设计取舍：

- 工具返回**结构化 JSON 文本**，字段名与 `/v1` 契约一致（`hits` / `why` / `suggestions` /
  `symbol` / `lines` / `truncated`）。模型看到的形状与 HTTP 契约同源，文档只需要维护一份。
- **工具内错误回 `{"error": ...}` 而不是抛异常**：版本写错、路径不存在这些是模型能自我纠正的，
  抛异常会变成 MCP 的 `isError` 且信息经过一层包装；只有鉴权失败才在中间件里回 401。
- `versions` 工具是必需的：模型不知道合法的版本串（`26.3` / `neoforge-21.1.265+1.21.1`），
  缺了它每次调用都会猜错。同时把版本清单写进 `instructions` 作为冗余。
- 每个工具调用自成事务：控制面连接与索引连接都在调用内建/关，不跨调用共享。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from mc_librarian import usage
from mc_librarian.answer import AskRun, OpenAiCompatChatClient
from mc_librarian.answer.tools import ToolBox
from mc_librarian.config import (
    LlmConfigError,
    Settings,
    resolve_llm_config,
)
from mc_librarian.db import Database
from mc_librarian.indexing import query
from mc_librarian.jobs import store
from mc_librarian.mcp.auth import Caller, current_caller
from mc_librarian.retrieval import service
from mc_librarian.schemas import SearchDetail, SearchRequest, SourceKind

logger = logging.getLogger(__name__)

SERVER_NAME: Final = "mc-librarian"
SERVER_INSTRUCTIONS: Final = """\
Minecraft 源码知识库（含原版与加载器版本）。用中文提问即可检索。

**先调 `versions` 拿到合法版本串**，再调其它工具。版本是必填参数，写错会返回错误而不是猜。

工具：
- `versions`            已索引的版本（含加载器版本与摘要覆盖）
- `search`              多路融合检索（中文摘要 / 符号 / 字面量 / 引用 / 补丁），主入口
- `symbol`              按类名或方法名找符号，返回签名与成员
- `outline`             文件骨架（类、方法、行号区间）
- `read`                按行读源码（必须给 start/end）
- `references`          反查谁调用了某个符号
- `ask`                 服务端跑完整检索循环，直接返回带行号引用的中文答案（较慢）

每条命中带 `why`（来自哪一路召回）与 `suggestions`（下一步可以搜什么）——
照着 `suggestions` 继续检索通常比换措辞重试更有效。

加载器版本（如 `neoforge-21.1.265+1.21.1`）的 `search` 支持 `source` 过滤：
`patched`（打过补丁的 MC）、`loader`（加载器自身）、`patch`（补丁文件）。
"""

# 工具返回给模型的 JSON 上限：单条命中带 snippet 时可能很长，超了截断并标注。
MAX_TOOL_PAYLOAD_CHARS: Final = 60_000


def _payload(data: Any) -> str:
    """把结果序列化成给模型看的 JSON 文本（中文不转义，模型读起来更省 token）。"""
    text = json.dumps(data, ensure_ascii=False, default=str)

    if len(text) <= MAX_TOOL_PAYLOAD_CHARS:
        return text

    return json.dumps(
        {
            "error": f"结果过大（{len(text)} 字符），已截断。请缩小 limit 或改用 detail=locate。",
            "truncated": True,
        },
        ensure_ascii=False,
    )


def _error(message: str, **extra: Any) -> str:
    return _payload({"error": message, **extra})


def _join(versions: list[str]) -> str:
    """把版本清单拼成一句话；空列表时说清「一个都没有」。"""
    return "、".join(versions) if versions else "（当前没有任何可检索的版本）"


def _caller() -> Caller | None:
    return current_caller.get()


def build_server(database: Database, settings: Settings) -> MCPServer:
    """按一个 app 实例的 `database` / `settings` 构造 MCP server（工具是闭包，不用全局单例）。"""
    server = MCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        # DNS-rebinding 防护默认开启，且默认白名单只有 localhost。生产域名与本地开发都要放行，
        # 否则请求会被 `421 Invalid Host header` 拒掉（实测）。白名单配在 `build_asgi_app` 里——
        # 真正生效的是 `streamable_http_app(transport_security=...)` 那一处，不是构造函数的参数。
        # 鉴权由 `BearerAuthMiddleware` 负责，Host 校验是第二道（防浏览器发起的跨站请求）。
    )

    @contextmanager
    def control() -> Iterator[sqlite3.Connection]:
        with database.connect() as connection:
            yield connection

    def version_states() -> list[dict[str, Any]]:
        """每个版本的元数据 + **是否真的能检索**（`searchable`）。

        「注册了」与「能检索」是两件事：版本行存在但 `db_path` 为空或索引文件被删时，
        `/v1/*` 与 MCP 的工具都会 404。`versions` 工具必须把这条差异说清楚，否则模型会挑一个
        看着在列表里、实际查不到的版本，然后反复失败。
        """
        with control() as connection:
            items = store.list_versions(connection)
            db_paths = store.list_version_db_paths(connection)

        rows: list[dict[str, Any]] = []

        for item in items:
            row: dict[str, Any] = {
                "version": item.version,
                "files": item.files,
                "tokens": item.tokens,
                "loader": item.loader,
                "loader_version": item.loader_version,
                "mc_version": item.mc_version,
                "obfuscated": item.obfuscated,
                "indexed_at": item.indexed_at,
            }
            db_path = db_paths.get(item.version)

            if db_path is None:
                row["searchable"] = False
                row["unavailable_reason"] = "索引文件未登记（版本行存在但 db_path 为空）"
            elif not Path(db_path).is_file():
                row["searchable"] = False
                row["unavailable_reason"] = f"索引文件不存在：{db_path}"
            else:
                row["searchable"] = True
                # 摘要覆盖率从索引库实时读（`versions` 表里的是登记快照，可能早于摘要补齐）。
                try:
                    with query.open_readonly(Path(db_path), check_same_thread=False) as index:
                        row["summary_count"] = index.execute(
                            "SELECT count(*) FROM summaries"
                        ).fetchone()[0]
                        row["class_count"] = index.execute(
                            "SELECT count(*) FROM classes"
                        ).fetchone()[0]
                except sqlite3.Error as error:
                    row["summary_error"] = str(error)

            rows.append(row)

        return rows

    def searchable_versions() -> list[str]:
        return [row["version"] for row in version_states() if row["searchable"]]

    @contextmanager
    def index_of(version: str) -> Iterator[sqlite3.Connection]:
        """按版本打开只读索引库。未注册 / 无索引文件时抛 `LookupError`（调用方转成 error JSON）。

        `LookupError` 的消息里**直接带上可检索版本清单**：只说「先调 versions」会让模型多绕
        一轮，而版本串本身模型是猜不出来的（`neoforge-21.1.265+1.21.1` 这种）。
        """
        available = searchable_versions()

        with control() as connection:
            registered, db_path = store.index_db_path(connection, version)

        if not registered:
            raise LookupError(f"版本 {version} 未注册。可检索的版本：{_join(available)}")

        if db_path is None:
            raise LookupError(f"版本 {version} 尚无索引文件。可检索的版本：{_join(available)}")

        path = Path(db_path)

        if not path.is_file():
            raise LookupError(
                f"版本 {version} 的索引文件不存在：{path}。可检索的版本：{_join(available)}"
            )

        # `check_same_thread=False`：MCPServer 可能把同步工具丢到线程池执行，连接建在事件循环线程。
        connection = query.open_readonly(path, check_same_thread=False)

        try:
            yield connection
        finally:
            connection.close()

    def effective() -> Settings:
        """当前有效配置（数据库覆盖 + 环境兜底）。设置页改完立即生效，不用重启。"""
        from mc_librarian import settings_store

        with control() as connection:
            return settings_store.effective_settings(
                settings, settings_store.read_overrides(connection, settings)
            )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @server.tool(
        description=(
            "列出**可以检索**的版本（其它工具的 version 参数必须取自这里）。"
            "每个版本带 searchable 标记：false 表示注册了但索引不可用，不能拿去检索。"
        )
    )
    def versions() -> str:
        """返回每个版本的元数据：文件数、token、摘要覆盖、加载器标识、是否可检索。"""
        rows = version_states()

        return _payload(
            {
                "versions": rows,
                # 单独给一份「可以直接拿去用」的清单：模型不必自己从 searchable 字段再筛一遍。
                "searchable_versions": [row["version"] for row in rows if row["searchable"]],
            }
        )

    @server.tool(
        description=(
            "多路融合检索（中文摘要 / 符号 / 字面量 / 引用 / 补丁）。"
            "中文提问的主入口；返回带 why 与 suggestions 的命中。"
        )
    )
    def search(
        query_text: str,
        version: str,
        source: str | None = None,
        path_prefix: str | None = None,
        detail: str = "snippet",
        limit: int = 10,
    ) -> str:
        """`source` 只对加载器版本有意义：patched / loader / patch。"""
        try:
            request = SearchRequest(
                query=query_text,
                version=version,
                source=_source(source),
                path_prefix=path_prefix,
                detail=_detail(detail),
                limit=max(1, min(limit, 50)),
            )
        except ValueError as error:
            return _error(f"参数不合法：{error}")

        try:
            with index_of(version) as index:
                response = service.search(index, effective(), request)
        except LookupError as error:
            return _error(str(error))
        except sqlite3.Error as error:
            return _error(f"检索失败：{error}")

        return _payload(response.model_dump(mode="json"))

    @server.tool(description="按类名或方法名查找符号（支持简单名与全限定名，模糊匹配命中后缀）。")
    def symbol(name: str, version: str, fuzzy: bool = True, source: str | None = None) -> str:
        try:
            with index_of(version) as index:
                matches = service.symbol_matches(
                    index, name, fuzzy=fuzzy, source=_source(source)
                )
        except LookupError as error:
            return _error(str(error))
        except sqlite3.Error as error:
            return _error(f"符号查找失败：{error}")

        return _payload(matches.model_dump(mode="json"))

    @server.tool(description="文件骨架：包名、imports、类/方法及其行号区间。")
    def outline(path: str, version: str) -> str:
        try:
            with index_of(version) as index:
                result = service.file_outline(index, effective(), version, path)
        except LookupError as error:
            return _error(str(error))
        except sqlite3.Error as error:
            return _error(f"读取骨架失败：{error}")

        if result is None:
            return _error(f"文件不存在或不在索引里：{path}")

        return _payload(result.model_dump(mode="json"))

    @server.tool(
        description=(
            "按行读源码（1-based 闭区间）。必须给 start/end——整文件读取会把上下文撑爆。"
            "超出 token 预算时返回 truncated=true。"
        )
    )
    def read(path: str, version: str, start: int, end: int, max_tokens: int = 6000) -> str:
        if start < 1 or end < start:
            return _error("行号不合法：要求 1 <= start <= end")

        try:
            with index_of(version):
                text = service.read_source(effective(), version, path)
        except LookupError as error:
            return _error(str(error))

        if text is None:
            return _error(f"文件不存在或不在源码根内：{path}")

        lines = text.splitlines()
        result = service.slice_lines(
            lines, path, version, start, min(end, len(lines)), max(1, min(max_tokens, 32_000))
        )

        return _payload(result.model_dump(mode="json"))

    @server.tool(description="反查引用：谁调用了这个方法/字段，以及所在的封闭方法。")
    def references(symbol_name: str, version: str, limit: int = 50) -> str:
        try:
            with index_of(version) as index:
                result = service.reference_list(
                    index, version, symbol_name, limit=max(1, min(limit, 200))
                )
        except LookupError as error:
            return _error(str(error))
        except sqlite3.Error as error:
            return _error(f"反查引用失败：{error}")

        return _payload(result.model_dump(mode="json"))

    @server.tool(
        description=(
            "服务端跑完整检索循环，直接返回带行号引用的中文答案。比逐步检索慢（30-70 秒），"
            "适合「直接给我结论」；需要自己控制检索过程时用 search + read。"
        )
    )
    async def ask(question: str, version: str) -> str:
        caller = _caller()

        if caller is None:  # 中间件保证非空，这里是防御性分支
            return _error("未认证。")

        config_now = effective()

        if not config_now.ask_enabled:
            return _error("问答功能已在设置页关闭。请改用 search / symbol / read。")

        try:
            config = resolve_llm_config(config_now)
        except LlmConfigError as error:
            return _error(f"模型未配置：{error}")

        try:
            with index_of(version) as index:
                run = _new_run(config, index, config_now, question, version)
                await _drain(run)
        except LookupError as error:
            return _error(str(error))

        _record_usage(database, caller, config.model, run, version)

        return _payload(
            {
                "answer": run.answer,
                "reason": run.reason,
                "citations": [c.model_dump(mode="json") for c in run.citations],
                "usage": run.usage.model_dump(mode="json"),
            }
        )

    return server


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------


def _source(value: str | None) -> SourceKind | None:
    """`source` 的字符串 → 契约里的取值域；None/空串 = 不过滤。"""
    if value is None or value == "":
        return None

    if value not in ("patched", "loader", "patch"):
        raise ValueError(f"source 只能是 patched / loader / patch，收到 {value!r}")

    return value  # type: ignore[return-value]


def _detail(value: str) -> SearchDetail:
    if value not in ("locate", "snippet"):
        raise ValueError(f"detail 只能是 locate / snippet，收到 {value!r}")

    return value  # type: ignore[return-value]


def _new_run(
    config: Any, index: sqlite3.Connection, settings: Settings, question: str, version: str
) -> AskRun:
    """构造一次问答循环。与 `/v1/ask` 用同一套 `AskRun` + `ToolBox`，只是不落会话历史。"""
    return AskRun(
        client=OpenAiCompatChatClient(config),
        toolbox=ToolBox(index, settings, version),
        question=question,
        version=version,
        max_steps=settings.ask_max_steps,
        max_answer_tokens=settings.ask_max_answer_tokens,
        max_context_tokens=settings.ask_max_context_tokens,
        timeout_s=float(settings.ask_timeout_s),
        llm_timeout_s=settings.llm_timeout_s,
        # MCP 的 ask 不建历史会话：它是一次性工具调用，没有「会话」概念（历史只在网页端）。
        conversation_id=None,
    )


async def _drain(run: AskRun) -> None:
    """把事件流跑到底。答案与引用都挂在 `run` 上，事件本身不需要。"""
    async for _event in run.stream():
        continue


def _record_usage(
    database: Database, caller: Caller, model: str, run: AskRun, version: str
) -> None:
    """落一条用量（与 `/v1/ask` 同口径，`kind="mcp_ask"`）。失败只记日志。"""
    try:
        with database.connect() as connection:
            usage.record(
                connection,
                user_id=caller.user_id,
                kind="mcp_ask",
                model=model,
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                latency_ms=run.latency_ms,
                version=version,
            )
    except sqlite3.Error:
        logger.exception("MCP ask 写用量失败")


def build_asgi_app(server: MCPServer) -> Any:
    """构造 MCP 的 ASGI 子 app：内部路径 `/`（外层 mount 到 `/mcp`）。"""
    return server.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[
                "mc-librarian.belovedyaoo.top",
                "127.0.0.1:*",
                "localhost:*",
                # Starlette 的 TestClient 用这个 Host，pytest 要能跑。
                "testserver",
            ],
            allowed_origins=[
                "https://mc-librarian.belovedyaoo.top",
                "http://127.0.0.1:*",
                "http://localhost:*",
            ],
        ),
    )


class NoTrailingSlashMiddleware:
    """让 `/mcp` 直接落到子 app，不发 307。

    `app.mount("/mcp", sub)` 对 `/mcp`（无尾斜杠）会回 307 → `/mcp/`。浏览器与多数客户端会跟随，
    但 **POST 上跟随重定向不可靠**（部分客户端不重放 body），MCP 的 initialize 恰恰是 POST。
    实测：加这个改写后 `/mcp` 直接 200，客户端不必处理重定向。

    只对 `path == "/mcp"` 动手，其它路径（`/v1/*`）原样透传。
    """

    def __init__(self, app: Any, prefix: str = "/mcp") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == self.prefix:
            scope = dict(scope, path=self.prefix + "/", raw_path=(self.prefix + "/").encode())

        await self.app(scope, receive, send)


__all__ = [
    "NoTrailingSlashMiddleware",
    "build_asgi_app",
    "build_server",
]
