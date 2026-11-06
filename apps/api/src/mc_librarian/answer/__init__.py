"""问答层（issue #8）：服务端检索循环、模型客户端、工具集与并发闸门。

`/v1/ask` 的 HTTP 层（`api/v1/answer.py`）只负责鉴权、开关/并发/版本/模型配置的检查与
SSE 编码；循环本身在 `agent.AskRun`，工具在 `tools.ToolBox`（复用 `mc_librarian.retrieval`）。
"""

from __future__ import annotations

from mc_librarian.answer.agent import AskRun
from mc_librarian.answer.client import (
    ChatError,
    OpenAiCompatChatClient,
    post_chat_completion,
    stream_chat_completion,
)
from mc_librarian.answer.limiter import AskLimiter
from mc_librarian.answer.runs import AskRuns, RunningAsk
from mc_librarian.answer.tools import ToolBox

__all__ = [
    "AskLimiter",
    "AskRun",
    "AskRuns",
    "ChatError",
    "OpenAiCompatChatClient",
    "RunningAsk",
    "ToolBox",
    "post_chat_completion",
    "stream_chat_completion",
]
