"""`POST /v1/ask`：中文问答（服务端检索循环 + SSE）。

契约见 `docs/openapi.yaml` 的 `/v1/ask`；规格见 issue #8 的规格补充 §1–§8。这一层只做四件事：

1. 前置检查：全局开关 / 并发上限（429）、版本索引（404）、模型配置（503）；
2. 把 `AskRun` 的事件流编码成 SSE（**手写 `StreamingResponse` + `text/event-stream`**，
   不引入 `sse-starlette` 之类的新依赖）；`Accept: application/json` 时退化成一次性 `AskResponse`；
3. 每次请求写**一行** `usage`（含降级的情况）；
4. 并发槽位在任何退出路径上都归还（含客户端中途断开）——收尾绑在 ASGI 响应的 `__call__` 上，
   不依赖 async generator 被正确关闭（见 `_AskStreamResponse`）。

模型超时 / 不可用 / 返回非法响应都在 `AskRun` 里翻成降级，**HTTP 仍是 200**——不冒泡成 500。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from functools import partial
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from mc_librarian import usage as usage_store
from mc_librarian.answer import AskLimiter, AskRun, OpenAiCompatChatClient
from mc_librarian.answer.tools import ToolBox
from mc_librarian.api.v1.retrieval import require_index
from mc_librarian.auth.deps import CurrentUser, DatabaseDep, DbConn, SettingsDep
from mc_librarian.config import (
    LlmConfigError,
    resolve_llm_config,
    resolve_llm_pricing,
)
from mc_librarian.db import Database
from mc_librarian.indexing import query
from mc_librarian.problems import Problem, ProblemException, problem_response
from mc_librarian.schemas import AskRequest, AskResponse, User

logger = logging.getLogger("mc_librarian.api.answer")

router = APIRouter()

ASK_DISABLED_TITLE: Final = "问答已暂停"
TOO_MANY_TITLE: Final = "请求过于频繁"
JSON_ACCEPT: Final = "application/json"
# 浏览器与反代对 SSE 的常规要求：不缓存、不缓冲（nginx 默认会缓冲 `text/event-stream`）。
SSE_HEADERS: Final = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class _AskStreamResponse(StreamingResponse):
    """`/v1/ask` 的 SSE 响应：把收尾挂在 ASGI 响应的 `__call__` 上，而不是生成器的 `finally` 上。

    Starlette 在客户端中途断开时会**取消流任务**（`spec_version < 2.4` 走取消作用域），此时生成器
    停在 `yield`，它自己的 `finally` 要等 async generator 被 GC / `aclose` 才执行——不是显式保证。
    槽位一旦漏掉就再也回不来（服务最终对所有新问答回 429），所以收尾必须绑在一个一定会执行的
    位置：`__call__` 的 `finally`，正常结束、异常、任务被取消三条路径都会走到。
    """

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        on_close: Callable[[], None],
        media_type: str,
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def ask_responses() -> dict[int | str, dict[str, Any]]:
    """`/v1/ask` 的响应声明。200 有两个 content type，只能写裸 `$ref`（见 `answer_schemas`）。"""
    return {
        200: {
            "description": (
                "OK：默认 `text/event-stream`（`step` → `delta`* → `citation`* → `done`）；"
                "请求头 `Accept: application/json` 时一次性返回完整 `AskResponse`"
            ),
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/AskEvent"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/AskResponse"}},
            },
        },
        401: problem_response("未认证"),
        404: problem_response("版本未注册或索引文件不存在"),
        422: problem_response("请求不合法"),
        429: problem_response("问答已被管理员暂停，或并发问答已达上限"),
        503: problem_response("未配置生成模型"),
    }


@router.post(
    "/ask",
    tags=["answer"],
    summary="中文问答（服务端检索循环）",
    description=(
        "服务端把检索服务层包成 5 个工具（search / symbol / references / outline / read），"
        "跑「检索 → 读取 → 作答」循环：默认流式返回 `step` → `delta`* → `citation`* → `done`；"
        "请求头 `Accept: application/json` 时退化为一次性返回完整 `AskResponse`。"
        "模型超时 / 不可用 / 返回非法响应时降级为「已召回的代码位置」，HTTP 仍是 200。"
    ),
    operation_id="ask",
    response_class=StreamingResponse,
    response_model=None,
    responses=ask_responses(),
)
async def ask(
    request: Request,
    database: DatabaseDep,
    conn: DbConn,
    user: CurrentUser,
    settings: SettingsDep,
    body: AskRequest,
) -> Response:
    if not settings.ask_enabled:
        raise ProblemException(429, ASK_DISABLED_TITLE, "问答已被管理员暂停")

    limiter: AskLimiter = request.app.state.ask_limiter

    if not limiter.try_acquire():
        raise ProblemException(
            429,
            TOO_MANY_TITLE,
            f"并发问答已达上限（{settings.max_concurrent_asks}），请稍后重试",
        )

    # 槽位从这一刻起被占用：到响应返回之前的任何失败路径都必须归还，否则闸门会永久收紧。
    index: sqlite3.Connection | None = None

    try:
        index_path = require_index(conn, settings, body.version)
        config = resolve_llm_config(settings)
        index = query.open_readonly(index_path, check_same_thread=False)
        run = AskRun(
            client=OpenAiCompatChatClient(config),
            toolbox=ToolBox(index, settings, body.version),
            question=body.question,
            version=body.version,
            max_steps=body.max_steps,
            max_answer_tokens=body.max_answer_tokens,
            max_context_tokens=settings.ask_max_context_tokens,
            timeout_s=float(settings.ask_timeout_s),
            llm_timeout_s=settings.llm_timeout_s,
            pricing=resolve_llm_pricing(settings),
        )
    except LlmConfigError as error:
        _abandon(index, limiter)
        raise ProblemException(503, "服务不可用", str(error)) from error
    except BaseException:
        _abandon(index, limiter)
        raise

    if _wants_json(request):
        return await _json_answer(run, database, user, config.model, body.version, index, limiter)

    return _AskStreamResponse(
        _sse_stream(run),
        on_close=partial(
            _close_ask, run, database, user, config.model, body.version, index, limiter
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _json_answer(
    run: AskRun,
    database: Database,
    user: User,
    model: str,
    version: str,
    index: sqlite3.Connection,
    limiter: AskLimiter,
) -> JSONResponse:
    """`Accept: application/json`：把事件流跑完，一次性返回 `AskResponse`。"""
    try:
        async for _event in run.stream():
            pass
    finally:
        _close_ask(run, database, user, model, version, index, limiter)

    payload = AskResponse(
        answer=run.answer,
        citations=run.citations,
        steps=run.steps,
        usage=run.usage,
        reason=run.reason,
    )

    return JSONResponse(content=payload.model_dump(exclude_none=True))


async def _sse_stream(run: AskRun) -> AsyncIterator[bytes]:
    """事件流 → SSE。收尾（关索引 / 还槽位 / 记用量）不在这里，见 `_AskStreamResponse`。"""
    try:
        async for event in run.stream():
            yield _encode(event)
    except Exception as error:  # 流已经开始，改不了状态码，只能发一个 error 事件收尾
        logger.exception("问答流中断（%s）", run.version)
        yield _encode(
            {
                "type": "error",
                "problem": Problem(status=500, title="问答失败", detail=str(error)).model_dump(
                    exclude_none=True
                ),
            }
        )


def _abandon(index: sqlite3.Connection | None, limiter: AskLimiter) -> None:
    """响应还没返回就失败：关掉可能已打开的索引连接，并归还槽位。"""
    if index is not None:
        index.close()

    limiter.release()


def _close_ask(
    run: AskRun,
    database: Database,
    user: User,
    model: str,
    version: str,
    index: sqlite3.Connection,
    limiter: AskLimiter,
) -> None:
    """一次问答的收尾：还槽位、关索引、记一行用量。任何退出路径都必须走到这里一次。"""
    # 槽位先还：后面两步即使失败，槽位也不会漏（漏了就没有任何补救路径）。
    limiter.release()
    index.close()
    _record_usage(database, run, user, model, version)


def _encode(event: dict[str, Any]) -> bytes:
    """一个 SSE 事件：`event:` 名与 `data:` 里的 `type` 都带上，前端两种读法都能用。"""
    payload = json.dumps(event, ensure_ascii=False)

    return f"event: {event['type']}\ndata: {payload}\n\n".encode()


def _wants_json(request: Request) -> bool:
    """契约：请求头 `Accept: application/json` 时退化为一次性返回。"""
    return JSON_ACCEPT in request.headers.get("accept", "")


def _record_usage(
    database: Database, run: AskRun, user: User, model: str, version: str
) -> None:
    """每次 `/v1/ask` 写一行用量（含降级）；写失败只记日志，不影响已经发出去的响应。"""
    try:
        with database.connect() as connection:
            usage_store.record(
                connection,
                user_id=user.id,
                kind=usage_store.KIND_ASK,
                model=model,
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                cost_usd=run.usage.cost_usd,
                latency_ms=run.latency_ms,
                version=version,
            )
    except Exception:  # 记账失败不该把一次成功的问答变成 500
        logger.exception("写入 usage 失败（%s）", version)
