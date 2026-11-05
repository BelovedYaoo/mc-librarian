"""检索服务层：四路召回 + RRF 融合（issue #6），加符号 / 引用 / 骨架 / 切片（issue #8）。

对外暴露 `search` 与四个读操作，HTTP 端点、MCP 适配器与 `/v1/ask` 的工具调用共用同一份逻辑。
"""

from __future__ import annotations

from mc_librarian.retrieval.service import (
    RetrievalNotFound,
    file_outline,
    read_source,
    reference_list,
    search,
    slice_lines,
    source_tree_present,
    symbol_matches,
)

__all__ = [
    "RetrievalNotFound",
    "file_outline",
    "read_source",
    "reference_list",
    "search",
    "slice_lines",
    "source_tree_present",
    "symbol_matches",
]
