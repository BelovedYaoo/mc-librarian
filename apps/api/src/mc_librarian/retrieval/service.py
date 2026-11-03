"""`POST /v1/search` 的服务层：跑四路通道 → RRF 融合 → 预算截断 → 建议（issue #6）。

检索逻辑只有这一份（`docs/00-overview.md` §4 的硬约束 2）：HTTP 端点、后续的 MCP 适配器与
`/v1/ask` 的工具调用都走这里。
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Final

from mc_librarian.config import Settings
from mc_librarian.indexing import query
from mc_librarian.indexing.layout import source_root
from mc_librarian.indexing.text import split_lines, tokens_from_chars
from mc_librarian.retrieval.channels import (
    Candidate,
    Channel,
    literal_candidates,
    reference_candidates,
    summary_candidates,
    symbol_candidates,
)
from mc_librarian.retrieval.fuse import FusedHit, apply_token_budget, fuse
from mc_librarian.schemas import Hit, SearchRequest, SearchResponse

# `snippet` 的截断上限：符号体可能很大，截断保证响应体量可控（规格 §5）。
SNIPPET_MAX_LINES: Final = 40
SNIPPET_MAX_CHARS: Final = 2000
# `suggestions` 取前 5 个（规格 §9）。
SUGGESTION_LIMIT: Final = 5


def search(
    connection: sqlite3.Connection, settings: Settings, request: SearchRequest
) -> SearchResponse:
    """跑一次混合检索。`connection` 是已只读打开的索引库连接。"""
    started = time.perf_counter()
    channels: dict[Channel, list[Candidate]] = {
        "summary": summary_candidates(
            connection, request.query, path_prefix=request.path_prefix
        ),
        "symbol": symbol_candidates(
            connection, request.query, kind=request.kind, path_prefix=request.path_prefix
        ),
        "literal": literal_candidates(
            connection,
            source_root(settings.data_dir, request.version),
            request.query,
            path_prefix=request.path_prefix,
        ),
        "reference": reference_candidates(
            connection, request.query, path_prefix=request.path_prefix
        ),
    }
    # `total` 是融合前的候选总数（各路候选之和），不因 `limit` / `max_tokens` 截断而变（规格 §6）。
    total = sum(len(candidates) for candidates in channels.values())
    fused = fuse(channels)[: request.limit]

    reader = _SourceReader(settings, request.version)
    snippets = [_snippet(reader, hit) for hit in fused]
    costs = [tokens_from_chars(len(snippet)) if snippet else 0 for snippet in snippets]
    kept = apply_token_budget(fused, costs, request.max_tokens)
    hits = [_to_hit(request, fused[index], snippets[index], costs[index]) for index in kept]

    return SearchResponse(
        query=request.query,
        version=request.version,
        hits=hits,
        suggestions=_suggestions(connection, request.query, hits),
        total=total,
        took_ms=int((time.perf_counter() - started) * 1000),
    )


def _to_hit(request: SearchRequest, hit: FusedHit, snippet: str | None, tokens: int) -> Hit:
    """融合命中 → 契约 `Hit`；`detail=locate` 时不带 `snippet` 与 `summary`（规格 §5）。"""
    candidate = hit.candidate
    identifier = (
        f"{request.version}:{candidate.path}#{candidate.symbol}"
        if candidate.symbol
        else f"{request.version}:{candidate.path}:{candidate.start_line}"
    )
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
        summary=candidate.summary if include_body else None,
        snippet=snippet if include_body else None,
        tokens=tokens,
    )


def _snippet(reader: _SourceReader, hit: FusedHit) -> str | None:
    """源码截断：取命中符号的行区间（文件级命中就是那一行），行数与字符数都设上限。"""
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
