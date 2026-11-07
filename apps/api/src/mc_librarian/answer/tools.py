"""`/v1/ask` 的 5 个检索工具（issue #8 规格 §1）。

工具集固定为 `search` / `symbol` / `references` / `outline` / `read`，参数与已有 HTTP 端点同义。
**不另写一套检索逻辑**：每个工具都调用 `mc_librarian.retrieval` 服务层的同一个函数，
HTTP 端点与这里共用一份实现。`version` 由请求给出，模型改不了——工具参数里没有它。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from mc_librarian.config import Settings
from mc_librarian.indexing import query
from mc_librarian.indexing.text import split_lines
from mc_librarian.retrieval import (
    RetrievalNotFound,
    file_outline,
    read_patch,
    read_source,
    reference_list,
    search_smart,
    slice_lines,
    symbol_matches,
)
from mc_librarian.schemas import (
    Citation,
    FileSlice,
    Hit,
    Outline,
    ReferenceResponse,
    SearchDetail,
    SearchRequest,
    SearchResponse,
    SymbolMatches,
)

# 工具参数里没给时的默认值，与 HTTP 端点同口径。
DEFAULT_LIMIT: Final = 10
DEFAULT_MAX_TOKENS: Final = 4000
# 一次工具结果最多给模型多少字符（在上下文预算之前先挡一道，避免单个结果过大）。
RESULT_MAX_CHARS: Final = 20000

# `references` 的 `limit` 上限（与 `GET /v1/references` 的 `le=200` 一致）。
REFERENCES_MAX_LIMIT: Final = 200

SEARCH_TOOL: Final = "search"
SYMBOL_TOOL: Final = "symbol"
REFERENCES_TOOL: Final = "references"
OUTLINE_TOOL: Final = "outline"
READ_TOOL: Final = "read"

TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {SEARCH_TOOL, SYMBOL_TOOL, REFERENCES_TOOL, OUTLINE_TOOL, READ_TOOL}
)

# 给模型的工具声明（OpenAI function calling 格式）。
TOOL_SCHEMAS: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "function": {
            "name": SEARCH_TOOL,
            "description": (
                "四路召回（中文摘要 / 符号 / 字面量 / 引用）在服务端用 RRF 融合。"
                "先用它定位相关类与方法；detail=locate 只返回路径与行区间（省 token），"
                "detail=snippet 额外带源码片段与中文摘要。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "中文或英文查询"},
                    "path_prefix": {
                        "type": "string",
                        "description": "只在该路径前缀下召回，如 net/minecraft/world",
                    },
                    "detail": {"type": "string", "enum": ["locate", "snippet"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "max_tokens": {"type": "integer", "minimum": 200, "maximum": 32000},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SYMBOL_TOOL,
            "description": "按类 / 方法 / 字段名查符号，返回全限定名、签名、路径与行区间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "简单名或全限定名"},
                    "fuzzy": {"type": "boolean", "description": "false 只做精确与简单名匹配"},
                    "kind": {"type": "string", "enum": ["any", "class", "method", "field"]},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": REFERENCES_TOOL,
            "description": (
                "反查某个方法 / 字段被谁调用，返回调用点路径与行号（召回优先，允许误报）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "方法名或 Class#method"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": OUTLINE_TOOL,
            "description": "看一个文件的骨架：包名、imports、类与方法签名和行号，不含方法体。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对该版本源码根的路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": READ_TOOL,
            "description": "精读源码切片，`start`/`end` 必填（1-based 闭区间）；不提供整文件读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                    "max_tokens": {"type": "integer", "minimum": 100, "maximum": 32000},
                },
                "required": ["path", "start", "end"],
            },
        },
    },
]


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用的结果：给模型的文本、给前端的 `hits` 计数、带出的引用位置。"""

    ok: bool
    text: str
    hits: int = 0
    citations: tuple[Citation, ...] = field(default=())


class ToolBox:
    """在某个版本的索引 + 源码上执行工具。`execute` 是阻塞的，调用方用 `asyncio.to_thread`。

    **每次工具调用自己开一个只读连接、用完立刻关**，不在实例上持有连接。

    为什么不共享：并发工具会共用一个连接，而 CPython 的 per-connection 语句缓存在跨线程时
    无锁共享——实测 400 次里 19 次 `bad parameter or other API misuse`、2000 次里出现
    静默读错行（`#27`）。

    为什么不池化/线程本地：实测开+读+关只要 0.17ms（80 次并发检索，每次调用 27.0s vs 共享
    99.9s）。而 `asyncio.to_thread` 的线程是**跨请求复用**的，线程本地连接会按「线程 × 版本」
    累积且永不关闭（5 版本 × 32 线程 × 默认 2MB 页缓存 ≈ 300MB），与 2C/2G 的目标冲突。
    每次调用开一个：没有生命周期状态、不可能泄漏，且保住了 3.7× 的并发收益。
    """

    def __init__(self, index_path: Path, settings: Settings, version: str) -> None:
        self._index_path = index_path
        self._settings = settings
        self._version = version

    @contextmanager
    def _index(self) -> Iterator[sqlite3.Connection]:
        """本次工具调用专用的只读连接（作用域就是一次调用）。"""
        connection = query.open_readonly(self._index_path)

        try:
            yield connection
        finally:
            connection.close()

    def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """执行一次工具调用；参数不合法 / 目标不存在都返回 `ok=False`，不抛异常。"""
        try:
            return self._dispatch(name, args)
        except RetrievalNotFound as error:
            return ToolOutcome(ok=False, text=f"失败：{error}")
        except (TypeError, ValueError) as error:
            return ToolOutcome(ok=False, text=f"参数不合法：{error}")
        except sqlite3.Error as error:
            return ToolOutcome(ok=False, text=f"索引查询失败：{error}")

    def _dispatch(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        if name == SEARCH_TOOL:
            return self._search(args)

        if name == SYMBOL_TOOL:
            return self._symbol(args)

        if name == REFERENCES_TOOL:
            return self._references(args)

        if name == OUTLINE_TOOL:
            return self._outline(args)

        if name == READ_TOOL:
            return self._read(args)

        return ToolOutcome(ok=False, text=f"未知工具：{name}")

    def _search(self, args: dict[str, Any]) -> ToolOutcome:
        request = SearchRequest(
            query=_require_str(args, "query"),
            version=self._version,
            path_prefix=_optional_str(args, "path_prefix"),
            detail=_detail(args),
            limit=_bounded_int(args, "limit", DEFAULT_LIMIT, 1, 50),
            max_tokens=_bounded_int(args, "max_tokens", DEFAULT_MAX_TOKENS, 200, 32000),
        )
        # 问答循环里**总是走智能检索**：模型自己写的查询未必是最优召回词，多花两次 LLM 调用
        # 换召回（实测纯字面 43% → 智能 93%）。纯标识符查询内部会直通字面，不会白花。
        with self._index() as connection:
            response = search_smart(connection, self._settings, request)

        return ToolOutcome(
            ok=True,
            text=_format_search(response),
            hits=len(response.hits),
            citations=tuple(_hit_citation(hit) for hit in response.hits),
        )

    def _symbol(self, args: dict[str, Any]) -> ToolOutcome:
        with self._index() as connection:
            matches = symbol_matches(
                connection,
                _require_str(args, "name"),
                fuzzy=bool(args.get("fuzzy", True)),
                kind=str(args.get("kind") or "any"),
            )

        return ToolOutcome(
            ok=True,
            text=_format_symbols(matches),
            hits=len(matches.matches),
            citations=tuple(
                Citation(path=item.path, lines=item.lines, symbol=item.fqn)
                for item in matches.matches
            ),
        )

    def _references(self, args: dict[str, Any]) -> ToolOutcome:
        with self._index() as connection:
            response = reference_list(
                connection,
                self._version,
                _require_str(args, "symbol"),
                limit=_bounded_int(args, "limit", 50, 1, REFERENCES_MAX_LIMIT),
            )

        return ToolOutcome(
            ok=True,
            text=_format_references(response),
            hits=len(response.references),
            citations=tuple(
                Citation(path=item.path, lines=item.lines, symbol=item.enclosing)
                for item in response.references
            ),
        )

    def _outline(self, args: dict[str, Any]) -> ToolOutcome:
        with self._index() as connection:
            outline = file_outline(
                connection, self._settings, self._version, _require_str(args, "path")
            )

        return ToolOutcome(
            ok=True,
            text=_format_outline(outline),
            hits=len(outline.classes),
            citations=tuple(
                Citation(path=outline.path, lines=item.lines or [], symbol=item.fqn)
                for item in outline.classes
                if item.lines and len(item.lines) == 2
            ),
        )

    def _read(self, args: dict[str, Any]) -> ToolOutcome:
        path = _require_str(args, "path")
        text = read_source(self._settings, self._version, path)

        # 补丁文件的正文在索引库里（`patches` 表的 hunk），不在源码树上。
        # 模型常从检索命中的 `path`（`xxx.java.patch`）直接来读，这里接住，不让它白跑一轮。
        if text is None:
            # 只有补丁回退才需要索引库：普通源码文件不白开连接。
            with self._index() as connection:
                text = read_patch(connection, path)

        if text is None:
            raise RetrievalNotFound(f"源码树里没有这个文件：{path}")

        lines = split_lines(text)
        total = len(lines) or 1
        start = _clamped_int(args, "start", 1, 1, total)
        # `end` **夹到文件末尾**而不是报错：模型无从知道文件多少行，猜大的很常见；
        # 报错只会让它再试一次，而返回实际读到的区间（结果里带 `total_lines`）它自己就能纠正。
        end = _clamped_int(args, "end", start, start, total)
        sliced = slice_lines(
            lines,
            path,
            self._version,
            start,
            end,
            _bounded_int(args, "max_tokens", DEFAULT_MAX_TOKENS, 100, 32000),
        )

        return ToolOutcome(
            ok=True,
            text=_format_slice(sliced),
            hits=len(sliced.content.splitlines()),
            citations=(Citation(path=path, lines=[sliced.start, sliced.end]),),
        )


def _format_search(response: SearchResponse) -> str:
    if not response.hits:
        return f"没有命中（query={response.query}，total={response.total}）"

    blocks = [f"命中 {len(response.hits)} / total {response.total}（query={response.query}）"]

    for index, hit in enumerate(response.hits, start=1):
        where = f"{hit.path}:{hit.lines[0]}-{hit.lines[1]}"
        channels = ",".join(hit.why)
        blocks.append(
            f"[{index}] {hit.type} {hit.symbol or hit.path} @ {where} why={channels}"
        )

        if hit.summary:
            blocks.append(f"    摘要：{hit.summary.replace(chr(10), ' / ')}")

        if hit.snippet:
            blocks.append("    片段：")
            blocks.extend(f"      {line}" for line in hit.snippet.splitlines())

    return _clamp("\n".join(blocks))


def _format_symbols(matches: SymbolMatches) -> str:
    if not matches.matches:
        return "没有找到这个符号"

    blocks = [f"找到 {len(matches.matches)} 个符号"]

    for item in matches.matches:
        blocks.append(
            f"- {item.kind} {item.fqn} @ {item.path}:{item.lines[0]}-{item.lines[1]}"
            + (f" signature={item.signature}" if item.signature else "")
        )

        for member in item.members or []:
            lines = member.lines or []
            where = f"{lines[0]}-{lines[1]}" if len(lines) == 2 else "?"
            blocks.append(f"    {member.kind} {member.signature or member.name} @ {where}")

    return _clamp("\n".join(blocks))


def _format_references(response: ReferenceResponse) -> str:
    if not response.references:
        return f"没有找到 {response.symbol} 的引用"

    blocks = [f"{response.symbol} 的引用 {len(response.references)} 处"]

    for item in response.references:
        enclosing = f"（在 {item.enclosing} 里）" if item.enclosing else ""
        blocks.append(f"- {item.path}:{item.lines[0]} {item.kind}{enclosing}")

    return _clamp("\n".join(blocks))


def _format_outline(outline: Outline) -> str:
    blocks = [f"骨架 {outline.path}（package {outline.package or '无'}）"]

    if outline.imports:
        blocks.append(f"imports：{', '.join(outline.imports)}")

    for item in outline.classes:
        lines = item.lines or []
        where = f"{lines[0]}-{lines[1]}" if len(lines) == 2 else "?"
        blocks.append(f"- {item.kind} {item.fqn} @ {where}")

        for method in item.methods or []:
            method_lines = method.lines or []
            method_where = (
                f"{method_lines[0]}-{method_lines[1]}" if len(method_lines) == 2 else "?"
            )
            blocks.append(f"    {method.signature or method.name} @ {method_where}")

    return _clamp("\n".join(blocks))


def _format_slice(sliced: FileSlice) -> str:
    header = (
        f"{sliced.path}:{sliced.start}-{sliced.end}（共 {sliced.total_lines} 行"
        + ("，超出预算已从尾部截断" if sliced.truncated else "")
        + "）"
    )
    body = [
        f"{sliced.start + offset}: {line}"
        for offset, line in enumerate(sliced.content.splitlines())
    ]

    return _clamp("\n".join([header, *body]))


def _clamp(text: str) -> str:
    """单个工具结果的字符上限；超出时截断并注明，避免一个结果吃掉整个上下文预算。"""
    if len(text) <= RESULT_MAX_CHARS:
        return text

    return text[:RESULT_MAX_CHARS] + "\n…（结果过长，已截断）"


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少参数 {key}")

    return value


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)

    if value is None or value == "":
        return None

    if not isinstance(value, str):
        raise ValueError(f"参数 {key} 必须是字符串")

    return value


def _detail(args: dict[str, Any]) -> SearchDetail:
    value = args.get("detail")

    if value is None:
        return "snippet"

    if value not in ("locate", "snippet"):
        raise ValueError("参数 detail 只能是 locate 或 snippet")

    return cast("SearchDetail", value)


def _clamped_int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    """取整并**夹到 [low, high]**（越界不报错）。给「模型猜行号」这类场景用。"""
    value = args.get(key, default)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"参数 {key} 必须是整数")

    return max(low, min(value, high))


def _bounded_int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key)

    if value is None:
        return default

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"参数 {key} 必须是整数")

    if value < low or value > high:
        raise ValueError(f"参数 {key} 超出范围 [{low}, {high}]：{value}")

    return value


def _hit_citation(hit: Hit) -> Citation:
    """`Hit` → `Citation`（`search` 命中的定位信息）。

    补丁命中（`hit.target` 非空）要**指向被改的源文件**，不是补丁文件：`hit.lines` 是目标
    `.java` 里的新行号（issue #24 规格 §5 重建过 `@@ -a,b +_,c @@` 的新行号），拿它去读
    `xxx.java.patch` 既不存在那一行、`.patch` 本身也不在源码树里。`symbol` 保留补丁路径，
    界面上一眼能看出「这条是补丁带来的」。
    """
    if hit.target:
        return Citation(path=hit.target, lines=hit.lines, symbol=hit.symbol)

    return Citation(path=hit.path, lines=hit.lines, symbol=hit.symbol)
