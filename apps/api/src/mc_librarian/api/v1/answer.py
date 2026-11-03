"""`POST /v1/ask`：中文问答（服务端检索循环 + SSE）。

契约见 `docs/openapi.yaml` 的 `/v1/ask`；规格见 issue #8 的规格补充 §1–§8。这一层只做四件事：

1. 前置检查：全局开关 / 并发上限（429）、版本索引（404）、模型配置（503）；
2. 把 `AskRun` 的事件流编码成 SSE（**手写 `StreamingResponse` + `text/event-stream`**，
   不引入 `sse-starlette` 之类的新依赖）；`Accept: application/json` 时退化成一次性 `AskResponse`；
3. 每次请求写**一行** `usage`（含降级的情况）；
4. 并发槽位在任何退出路径上都归还（含客户端中途断开）。

模型超时 / 不可用 / 返回非法响应都在 `AskRun` 里翻成降级，**HTTP 仍是 200**——不冒泡成 500。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

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

    try:
        index_path = require_index(conn, settings, body.version)
        config = resolve_llm_config(settings)
        index = query.open_readonly(index_path, check_same_thread=False)
    except LlmConfigError as error:
        limiter.release()
        raise ProblemException(503, "服务不可用", str(error)) from error
    except BaseException:
        limiter.release()
        raise

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

    if _wants_json(request):
        return await _json_answer(run, database, user, config.model, body.version, index, limiter)

    return StreamingResponse(
        _sse_stream(run, database, user, config.model, body.version, index, limiter),
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
        index.close()
        limiter.release()
        _record_usage(database, run, user, model, version)

    payload = AskResponse(
        answer=run.answer,
        citations=run.citations,
        steps=run.steps,
        usage=run.usage,
        reason=run.reason,
    )

    return JSONResponse(content=payload.model_dump(exclude_none=True))


async def _sse_stream(
    run: AskRun,
    database: Database,
    user: User,
    model: str,
    version: str,
    index: sqlite3.Connection,
    limiter: AskLimiter,
) -> AsyncIterator[bytes]:
    """事件流 → SSE；无论正常结束、异常还是客户端断开，都归还槽位并记一行用量。"""
    try:
        async for event in run.stream():
            yield _encode(event)
    except Exception as error:  # 流已经开始，改不了状态码，只能发一个 error 事件收尾
        logger.exception("问答流中断（%s）", version)
        yield _encode(
            {
                "type": "error",
                "problem": Problem(status=500, title="问答失败", detail=str(error)).model_dump(
                    exclude_none=True
                ),
            }
        )
    finally:
        index.close()
        limiter.release()
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
