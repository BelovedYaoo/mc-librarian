"""检索服务层：四路召回 + RRF 融合（issue #6）。

对外只暴露 `search`，HTTP 端点、MCP 适配器与 `/v1/ask` 的工具调用共用同一份逻辑。
"""

from __future__ import annotations

from mc_librarian.retrieval.service import search

__all__ = ["search"]
