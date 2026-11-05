"""`POST /v1/search` 的服务层：跑四路通道 → RRF 融合 → 预算截断 → 建议（issue #6）。

检索逻辑只有这一份（`docs/00-overview.md` §4 的硬约束 2）：HTTP 端点、后续的 MCP 适配器与
`/v1/ask` 的工具调用都走这里。除了 `search`，符号 / 引用 / 骨架 / 切片四个读操作也收敛在
这里（`symbol_matches` / `reference_list` / `file_outline` / `read_source` + `slice_lines`），
`api/v1/retrieval.py` 与 `answer/tools.py` 共用同一批函数。
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Final

from mc_librarian.config import Settings
from mc_librarian.indexing import java, query
from mc_librarian.indexing.layout import source_root
from mc_librarian.indexing.text import count_tokens, split_lines
from mc_librarian.retrieval.channels import (
    Candidate,
    Channel,
    literal_candidates,
    patch_candidates,
    reference_candidates,
    summary_candidates,
    symbol_candidates,
)
from mc_librarian.retrieval.fuse import FusedHit, apply_token_budget, fuse
from mc_librarian.schemas import (
    FileSlice,
    Hit,
    Outline,
    OutlineClass,
    OutlineMethod,
    Reference,
    ReferenceResponse,
    SearchRequest,
    SearchResponse,
    SymbolDetail,
    SymbolMatches,
    SymbolMember,
)

# `snippet` 的截断上限：符号体可能很大，截断保证响应体量可控（规格 §5）。
SNIPPET_MAX_LINES: Final = 40
SNIPPET_MAX_CHARS: Final = 2000
# `suggestions` 取前 5 个（规格 §9）。
SUGGESTION_LIMIT: Final = 5


class RetrievalNotFound(LookupError):
    """检索目标不存在（索引里没有该文件，或源码树里没有该路径）。

    HTTP 层翻成 404，`/v1/ask` 的工具层翻成一条给模型的失败说明——不把「没找到」升级成 500。
    """


def search(
    connection: sqlite3.Connection, settings: Settings, request: SearchRequest
) -> SearchResponse:
    """跑一次混合检索。`connection` 是已只读打开的索引库连接。"""
    started = time.perf_counter()
    channels: dict[Channel, list[Candidate]] = {
        "summary": summary_candidates(
            connection,
            request.query,
            path_prefix=request.path_prefix,
            source=request.source,
        ),
        "symbol": symbol_candidates(
            connection,
            request.query,
            kind=request.kind,
            path_prefix=request.path_prefix,
            source=request.source,
        ),
        "literal": literal_candidates(
            connection,
            source_root(settings.data_dir, request.version),
            request.query,
            path_prefix=request.path_prefix,
            source=request.source,
        ),
        "reference": reference_candidates(
            connection, request.query, path_prefix=request.path_prefix, source=request.source
        ),
        "patch": patch_candidates(
            connection,
            request.query,
            path_prefix=request.path_prefix,
            source=request.source,
        ),
    }
    # `total` 是融合前的候选总数（各路候选之和），不因 `limit` / `max_tokens` 截断而变（规格 §6）。
    total = sum(len(candidates) for candidates in channels.values())
    fused = fuse(channels)[: request.limit]

    snippets = _snippets(settings, request, fused)
    costs = [
        count_tokens(_content_text(request, hit, snippet))
        for hit, snippet in zip(fused, snippets, strict=True)
    ]
    kept = apply_token_budget(costs, request.max_tokens)
    hits = [_to_hit(request, fused[index], snippets[index], costs[index]) for index in kept]

    return SearchResponse(
        query=request.query,
        version=request.version,
        hits=hits,
        suggestions=_suggestions(connection, request.query, hits),
        total=total,
        took_ms=int((time.perf_counter() - started) * 1000),
    )


def symbol_matches(
    connection: sqlite3.Connection,
    name: str,
    *,
    fuzzy: bool = True,
    kind: str = "any",
    source: str | None = None,
) -> SymbolMatches:
    """`GET /v1/symbol` 与 ask 的 `symbol` 工具共用的符号查找（`source` 见 issue #24 规格 §5）。"""
    hits = query.find_symbols(connection, name, fuzzy=fuzzy, kind=kind, source=source)

    return SymbolMatches(matches=[_to_symbol_detail(connection, hit) for hit in hits])


def reference_list(
    connection: sqlite3.Connection, version: str, symbol: str, *, limit: int = 50
) -> ReferenceResponse:
    """`GET /v1/references` 与 ask 的 `references` 工具共用的引用反查。"""
    hits = query.find_references(connection, symbol, limit=limit)

    return ReferenceResponse(
        symbol=symbol,
        version=version,
        references=[
            Reference(
                path=hit.path,
                lines=[hit.line, hit.line],
                enclosing=hit.enclosing,
                kind=hit.kind,
            )
            for hit in hits
        ],
    )


def read_source(settings: Settings, version: str, path: str) -> str | None:
    """读 `<data_dir>/src/<version>/<path>` 的文本；缺失或越出源码根返回 None。

    `GET /v1/outline`、`GET /v1/read` 与 ask 的 `outline`/`read` 工具共用这一个入口：
    路径穿越（`../`）在这里就被挡掉，不在每个调用方各写一遍。
    """
    root = source_root(settings.data_dir, version).resolve()
    candidate = (root / path).resolve()

    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None

    return candidate.read_text(encoding="utf-8", errors="replace")


def read_patch(connection: sqlite3.Connection, path: str) -> str | None:
    """从索引库的 `patches` 表重建补丁文本；不是补丁或没有这张表时返回 None。

    为什么需要它：`search` 的补丁命中里 `path` 是 `xxx.java.patch`，模型照着它去调 `read`
    是很自然的动作；但 `.patch` 文件**不在源码树里**（只在索引里切成了 hunk），于是这一读
    必然失败，白花一轮工具调用。

    重建按 `hunk_index` 拼 `context`：每段 `context` 自带 `@@ -a,b +c,d @@` 头与上下文行，
    拼起来就是可读的补丁正文。**不是**逐字节还原原文件（文件头那几行 `diff --git`、`---`、
    `+++` 没存），读补丁的人要看的是「改了哪几行」，这里够了。
    """
    if not query.has_table(connection, "patches"):
        return None

    rows = connection.execute(
        "SELECT context FROM patches WHERE path = ? ORDER BY hunk_index", (path,)
    ).fetchall()

    if not rows:
        return None

    # 用 `chr(10)` 而不是字面换行：脚本里写 `"\\n"` 时转义层数容易数错，这里直接避开。
    return chr(10).join(str(row[0]) for row in rows)


def source_tree_present(settings: Settings, version: str) -> bool:
    """该版本有没有源码树。

    调用方据此区分两种 404：「整棵源码树没上传」与「树在、但这个文件不在索引/磁盘上」。
    两者的排查动作完全不同（补传源码 vs 换路径），不能共用一句话。
    """
    return source_root(settings.data_dir, version).is_dir()


def slice_lines(
    lines: list[str], path: str, version: str, start: int, end: int, max_tokens: int
) -> FileSlice:
    """取 `lines[start-1:end]` 并按 `max_tokens` 从尾部截断（`/v1/read` 与 ask 的 `read` 共用）。

    行号合法性由调用方先校验（HTTP 层要回 422 并带上文件总行数，工具层要把它变成一条失败说明）。
    """
    selected = lines[start - 1 : end]
    kept: list[str] = []
    used = 0

    for line in selected:
        cost = count_tokens(line + "\n")

        if kept and used + cost > max_tokens:
            break

        kept.append(line)
        used += cost

    return FileSlice(
        path=path,
        version=version,
        start=start,
        end=start + len(kept) - 1,
        total_lines=len(lines),
        content="\n".join(kept),
        truncated=len(kept) < len(selected),
    )


def file_outline(
    connection: sqlite3.Connection, settings: Settings, version: str, path: str
) -> Outline:
    """文件骨架（`GET /v1/outline` 与 ask 的 `outline` 工具共用）。

    文件不在索引里、或源码树里没有该路径时抛 `RetrievalNotFound`（HTTP 层翻 404，工具层翻失败）。

    补丁文件在这里就拦下：它不是 Java 源码，没有类/方法骨架。之前只报「源码树里没有这个文件」，
    模型会误以为路径写错而反复重试（实测它对着 `.patch` 先 outline 再 read）。这条检查放在服务层
    而不是工具层，HTTP / MCP / ask 三条入口才能给同一句话。
    """
    if path.endswith(".patch"):
        raise RetrievalNotFound(
            f"{path} 是补丁文件，没有类/方法骨架；用 read 读它的改动内容（含行号）"
        )

    record = query.find_file(connection, path)

    if record is None:
        raise RetrievalNotFound(f"索引里没有文件：{path}")

    text = read_source(settings, version, path)

    if text is None:
        raise RetrievalNotFound(
            f"该版本的源码树缺失（期望路径 {source_root(settings.data_dir, version)}）"
        )

    imports = java.parse_imports(text)
    classes = query.file_outline(connection, record.id)

    return Outline(
        path=path,
        package=record.package,
        imports=imports,
        classes=[
            OutlineClass(
                fqn=item.fqn,
                kind=item.kind,
                signature=item.signature,
                lines=[item.start_line, item.end_line],
                methods=[
                    OutlineMethod(
                        name=method.name,
                        signature=method.signature,
                        lines=[method.start_line, method.end_line],
                    )
                    for method in item.methods
                ],
            )
            for item in classes
        ],
    )


def _to_symbol_detail(connection: sqlite3.Connection, hit: query.SymbolHit) -> SymbolDetail:
    """符号命中 → `SymbolDetail`；类的命中附带成员列表（方法 + 字段）。"""
    members: list[SymbolMember] = []

    if hit.kind == "class":
        members = [
            SymbolMember(
                kind=member.kind,
                name=member.name,
                signature=member.signature,
                lines=[member.start_line, member.end_line],
            )
            for member in query.class_members(connection, hit.class_id)
        ]

    return SymbolDetail(
        fqn=hit.fqn,
        kind=hit.kind,
        signature=hit.signature,
        path=hit.path,
        lines=[hit.start_line, hit.end_line],
        superclass=hit.superclass,
        interfaces=hit.interfaces,
        annotations=hit.annotations,
        members=members,
    )


def _to_hit(request: SearchRequest, hit: FusedHit, snippet: str | None, tokens: int) -> Hit:
    """融合命中 → 契约 `Hit`；`detail=locate` 时不带 `snippet` 与 `summary`（规格 §5）。

    `id` 用融合用的实体键（`Candidate.key`，带行区间）：Java 允许同名重载方法，
    只拼 `version:path#symbol` 会让两条重载命中拿到同一个 id，前端列表撞 key。
    """
    candidate = hit.candidate
    identifier = f"{request.version}:{candidate.key}"
    include_body = request.detail == "snippet"

    return Hit(
        id=identifier,
        type=candidate.kind,
        version=request.version,
        path=candidate.path,
        symbol=candidate.symbol,
        lines=[candidate.start_line, candidate.end_line],
        score=round(hit.score, 6),
        why=list(hit.channels),
        target=candidate.target,
        summary=candidate.summary if include_body else None,
        snippet=snippet if include_body else None,
        tokens=tokens,
    )


def _snippets(
    settings: Settings, request: SearchRequest, fused: list[FusedHit]
) -> list[str | None]:
    """每条命中的源码片段；`locate` 不返回片段，也就不为它读盘（规格 §5）。"""
    if request.detail != "snippet":
        return [None] * len(fused)

    reader = _SourceReader(settings, request.version)

    return [_snippet(reader, hit) for hit in fused]


def _content_text(request: SearchRequest, hit: FusedHit, snippet: str | None) -> str:
    """一条命中**实际返回**的文本内容，`tokens` 与 `max_tokens` 预算都按它算（规格 §5/§6）。

    `locate` 只有定位字段（版本、路径、行区间、符号、`why`），没有片段与摘要；`snippet` 才
    把它们算进去。拿片段成本给 `locate` 记账，既虚报了它返回的 token，又让预算被不存在的
    片段吃掉——与 `docs/01-api.md` 的「`locate` 省 5–10 倍 token」相悖。
    """
    candidate = hit.candidate
    parts = [
        f"{request.version}:{candidate.path}",
        f"{candidate.start_line}-{candidate.end_line}",
        candidate.symbol or "",
    ]

    # 只在真有 `target`（patch 命中）时追加：否则会多出一个分隔空格，把老索引的 token 记账
    # 悄悄 +1（回归对照实测）。
    if candidate.target:
        parts.append(candidate.target)

    parts.extend(hit.channels)

    if request.detail == "snippet":
        parts.append(candidate.summary or "")
        parts.append(snippet or "")

    return " ".join(parts)


def _snippet(reader: _SourceReader, hit: FusedHit) -> str | None:
    """源码截断：取命中符号的行区间（文件级命中就是那一行），行数与字符数都设上限。

    补丁命中（`type = patch`）直接给 hunk 正文（`patch_context`，hunk 头 + diff）：补丁文件不在
    `<data_dir>/src/<version>/` 里（不复制进去，避免污染 ripgrep 的源码扫描），读盘路径不适用。
    """
    if hit.candidate.patch_context is not None:
        return hit.candidate.patch_context[:SNIPPET_MAX_CHARS]

    lines = reader.lines(hit.candidate.path)

    if lines is None:
        return None

    start = hit.candidate.start_line
    end = min(hit.candidate.end_line, start + SNIPPET_MAX_LINES - 1)
    text = "\n".join(lines[start - 1 : end])

    return text[:SNIPPET_MAX_CHARS]


def _suggestions(
    connection: sqlite3.Connection, query_text: str, hits: list[Hit]
) -> list[str]:
    """从同批命中的符号名里挑区分度最高的几个（规格 §9）。

    1. 收集命中涉及的符号名（类名 / 方法名 / 字段名）；2. 排除 query 中已出现的词（大小写
    不敏感）；3. 按「在索引库中的出现次数升序」排序；4. 取前 5 个。
    """
    words = {word.lower() for word in re.findall(r"\w+", query_text)}
    names: list[str] = []

    for hit in hits:
        name = _simple_name(hit)

        if name is None or name.lower() in words or name in names:
            continue

        names.append(name)

    if not names:
        return []

    counts = query.count_symbol_names(connection, names)
    names.sort(key=lambda name: (counts.get(name, 0), name))

    return names[:SUGGESTION_LIMIT]


def _simple_name(hit: Hit) -> str | None:
    if not hit.symbol:
        return None

    separator = "." if hit.type == "class" else "#"

    return hit.symbol.rsplit(separator, 1)[-1]


class _SourceReader:
    """按路径缓存源码行；缺失或越出源码根时返回 None（该条不给 `snippet`，不影响其他命中）。"""

    def __init__(self, settings: Settings, version: str) -> None:
        self._root = source_root(settings.data_dir, version).resolve()
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, path: str) -> list[str] | None:
        if path not in self._cache:
            self._cache[path] = self._read(path)

        return self._cache[path]

    def _read(self, path: str) -> list[str] | None:
        candidate = (self._root / path).resolve()

        if not candidate.is_relative_to(self._root) or not candidate.is_file():
            return None

        return split_lines(candidate.read_text(encoding="utf-8", errors="replace"))
