"""`POST /v1/ask`：中文问答（服务端检索循环 + SSE）。

契约见 `docs/openapi.yaml` 的 `/v1/ask`；规格见 issue #8 的规格补充 §1–§8。这一层只做四件事：

1. 前置检查：全局开关 / 并发上限（429）、版本索引（404）、模型配置（503）；
2. **生成属于后端任务**：`AskRuns` 起一个后台协程跑完整个循环，SSE 只是它的一个订阅视图
   （`answer.runs`）。客户端断开只退订，不影响生成与落库——之前生成由 HTTP 响应驱动，
   用户切到别的页面就把这次问答掐死了，那是架构错误；
3. 事件流编码成 SSE（**手写 `StreamingResponse` + `text/event-stream`**，不引入
   `sse-starlette`）；`Accept: application/json` 时等运行结束，退化成一次性 `AskResponse`；
4. 每次请求写**一行** `usage`（含降级），写完在后台检查一次当日告警阈值（`#12`，不阻断）；
5. 历史会话（问答页左栏）：首次提问自动建会话、把用户问题落库，运行结束时把助手答案（含引用
   与 usage）写一条消息；`conversation_id` 随 `done` 事件回传；
6. 并发槽位由**运行本身**持有，跑到结束才归还（含断开、服务停止的取消路径）。

模型超时 / 不可用 / 返回非法响应都在 `AskRun` 里翻成降级，**HTTP 仍是 200**——不冒泡成 500。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from functools import partial
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mc_librarian import alerts, conversations
from mc_librarian import usage as usage_store
from mc_librarian.answer import AskLimiter, AskRun, AskRuns, OpenAiCompatChatClient, RunningAsk
from mc_librarian.answer.agent import TEXT_SEPARATOR
from mc_librarian.answer.tools import ToolBox
from mc_librarian.api.v1.retrieval import require_index
from mc_librarian.auth.deps import CurrentUser, DatabaseDep, DbConn, EffectiveSettingsDep
from mc_librarian.config import (
    LlmConfigError,
    Settings,
    resolve_llm_config,
)
from mc_librarian.db import Database, transaction
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import AskRequest, AskResponse, User

logger = logging.getLogger("mc_librarian.api.answer")

router = APIRouter()

ASK_DISABLED_TITLE: Final = "问答已暂停"
TOO_MANY_TITLE: Final = "请求过于频繁"
NOT_FOUND_TITLE: Final = "资源不存在"
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
    settings: EffectiveSettingsDep,
    body: AskRequest,
) -> Response:
    if not settings.ask_enabled:
        raise ProblemException(429, ASK_DISABLED_TITLE, "问答已被管理员暂停")

    limiter: AskLimiter = request.app.state.ask_limiter

    if not limiter.try_acquire(settings.max_concurrent_asks):
        raise ProblemException(
            429,
            TOO_MANY_TITLE,
            f"并发问答已达上限（{settings.max_concurrent_asks}），请稍后重试",
        )

    # 槽位从这一刻起被占用：到响应返回之前的任何失败路径都必须归还，否则闸门会永久收紧。
    try:
        index_path = require_index(conn, settings, body.version)
        config = resolve_llm_config(settings)
        # 会话解析 / 新建 + 用户问题落库。给错 `conversation_id` 时这里抛 404，与其余失败路径
        # 一样走到 `except BaseException` 归还槽位。
        conversation_id = _open_conversation(conn, user, body)
        run = AskRun(
            client=OpenAiCompatChatClient(config),
            # 工具自己按调用开连接（`ToolBox._index`），这里只给路径。
            toolbox=ToolBox(index_path, settings, body.version),
            question=body.question,
            version=body.version,
            # 省略时用服务端设置（设置页可改，见 `settings.ask.max_steps`）。
            max_steps=body.max_steps or settings.ask_max_steps,
            max_answer_tokens=body.max_answer_tokens or settings.ask_max_answer_tokens,
            max_context_tokens=settings.ask_max_context_tokens,
            timeout_s=float(settings.ask_timeout_s),
            llm_timeout_s=settings.llm_timeout_s,
            conversation_id=conversation_id,
        )
    except LlmConfigError as error:
        _abandon(limiter)
        raise ProblemException(503, "服务不可用", str(error)) from error
    except BaseException:
        _abandon(limiter)
        raise

    runs: AskRuns = request.app.state.ask_runs
    running = runs.start(
        run=run,
        conversation_id=conversation_id,
        on_finish=partial(
            _finalize_ask,
            database=database,
            user=user,
            model=config.model,
            version=body.version,
            limiter=limiter,
            settings=settings,
        ),
    )

    if _wants_json(request):
        # 一次性响应也走同一个后台任务：连接断了照样跑完、落库，只是没人读返回值。
        await running.wait()

        return JSONResponse(
            content=AskResponse(
                answer=run.answer,
                citations=run.citations,
                steps=run.steps,
                usage=run.usage,
                reason=run.reason,
            ).model_dump(exclude_none=True)
        )

    return StreamingResponse(
        _sse_stream(running),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _sse_stream(running: RunningAsk) -> AsyncIterator[bytes]:
    """把一个进行中的问答编码成 SSE。

    这里**不驱动生成**：事件由后台任务产出，本生成器只是订阅者。客户端断开时 Starlette 取消
    这个生成器，`iter_events` 的 `finally` 退订，生成继续跑到结束并落库。
    """
    async for event in running.iter_events():
        yield _encode(event)


def _abandon(limiter: AskLimiter) -> None:
    """响应还没返回就失败：归还槽位（连接由各工具自己开/关，这里没有要收的）。"""
    limiter.release()


def _open_conversation(conn: sqlite3.Connection, user: User, body: AskRequest) -> str:
    """解析或新建会话，并把用户问题落库；返回会话 id。

    - 省略 `conversation_id` → 新建会话（标题取首条问题，超出 30 字截断），并裁剪到每用户上限；
    - 给了 `conversation_id` → 必须是本人的会话，否则 404（不泄露会话是否存在）。

    用户问题在建会话时立即落库（助手答案在收尾时落），这样客户端中途断开也不会丢掉问题。
    """
    if body.conversation_id is None:
        with transaction(conn):
            conversation_id = conversations.create(
                conn, user_id=user.id, title=conversations.title_from_question(body.question)
            )
            conversations.append_message(
                conn,
                conversation_id=conversation_id,
                role="user",
                content=body.question,
                version=body.version,
            )
            conversations.prune(conn, user_id=user.id)

        return conversation_id

    with transaction(conn):
        owned = conversations.find_owned(
            conn, conversation_id=body.conversation_id, user_id=user.id
        )

        if owned is None:
            raise ProblemException(404, "资源不存在", "会话不存在")

        conversations.append_message(
            conn,
            conversation_id=body.conversation_id,
            role="user",
            content=body.question,
            version=body.version,
        )
        conversations.touch(conn, conversation_id=body.conversation_id)

    return body.conversation_id


# 客户端中途断开时追加在答案末尾的说明。它不是模型输出，但必须写进历史：
# 用户下一刻回到这个会话，看到的是一段**自然结束**的答案会以为是模型答完了。
INTERRUPTED_NOTICE: Final = "（连接中断，本次回答未完成）"


def _record_answer(database: Database, run: AskRun) -> None:
    """收尾时把助手答案（含引用与 usage）落库；流式过程中的增量不落库。

    **中途断开也写**：客户端切到别的页面时生成器停在 `yield`，`run.finished` 为假，答案只有
    半截。以前整条不写，用户回来只看到一个孤零零的问题，看起来像「刚才的问答没发生过」。
    现在把已经发出去的那部分存下来，并在末尾标注未完成——残留比空白可解释，也不丢已花的 token。

    一个字的正文都没有（第一步就失败）时不写，避免留下一条空消息。
    写失败只记日志，不影响已经发出去的响应。
    """
    if run.conversation_id is None:
        return

    content = run.answer
    segments = list(run.segments)

    if not run.finished:
        if not content.strip():
            return

        notice = f"{TEXT_SEPARATOR}{INTERRUPTED_NOTICE}"
        content = f"{content}{notice}"
        segments = [*segments, {"kind": "text", "text": notice}]

    try:
        with database.connect() as connection, transaction(connection):
            conversations.append_message(
                connection,
                conversation_id=run.conversation_id,
                role="assistant",
                content=content,
                citations=run.citations,
                usage=run.usage,
                version=run.version,
                # 有序片段（正文 / 工具批次交替）：历史恢复时按它还原轨迹，
                # 否则一次深度检索的历史只剩一大段正文，中间调过什么全看不见。
                segments=segments,
            )
            conversations.touch(connection, conversation_id=run.conversation_id)
    except Exception:  # 写历史失败不该把一次成功的问答变成 500
        logger.exception("写入问答历史失败（%s）", run.conversation_id)


@router.get(
    "/conversations/{conversationId}/stream",
    tags=["answer"],
    summary="接着看：订阅该会话正在生成的回答",
    description=(
        "这个会话若仍有回答在后端生成，返回与 `/v1/ask` 相同的 SSE 事件流"
        "（先回放已经发生的，再实时跟随）；没有在跑的运行则 404。\n\n"
        "回答的生成不依赖这条连接：断开只是停止观看，后台跑完照样落库。"
    ),
    operation_id="streamConversation",
    responses={
        401: problem_response("未认证"),
        404: problem_response("会话不存在，或该会话没有正在生成的回答"),
    },
)
async def stream_conversation(
    request: Request,
    conn: DbConn,
    user: CurrentUser,
    conversationId: str,
) -> Response:
    # 归属先校验：别人的会话一律 404（不泄露是否存在），与详情端点同一口径。
    row = conversations.find_owned(conn, conversation_id=conversationId, user_id=user.id)

    if row is None:
        raise ProblemException(404, NOT_FOUND_TITLE, "会话不存在")

    runs: AskRuns = request.app.state.ask_runs
    running = runs.active(conversationId)

    if running is None:
        raise ProblemException(404, NOT_FOUND_TITLE, "该会话没有正在生成的回答")

    return StreamingResponse(
        _sse_stream(running),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _finalize_ask(
    run: AskRun,
    *,
    database: Database,
    user: User,
    model: str,
    version: str,
    limiter: AskLimiter,
    settings: Settings,
) -> None:
    """一次问答的收尾：还槽位、关索引、写一条助手消息、记一行用量。

    由**运行本身**在结束时调用（正常结束、生成异常、服务停止时被取消三条路径），
    不再挂在 HTTP 响应的关闭上：客户端断开不影响生成，自然也不该由它触发收尾。
    """
    # 槽位先还：后面几步即使失败，槽位也不会漏（漏了就没有任何补救路径）。
    limiter.release()
    _record_answer(database, run)
    _record_usage(database, run, user, model, version, settings)


def _encode(event: dict[str, Any]) -> bytes:
    """一个 SSE 事件：`event:` 名与 `data:` 里的 `type` 都带上，前端两种读法都能用。"""
    payload = json.dumps(event, ensure_ascii=False)

    return f"event: {event['type']}\ndata: {payload}\n\n".encode()


def _wants_json(request: Request) -> bool:
    """契约：请求头 `Accept: application/json` 时退化为一次性返回。"""
    return JSON_ACCEPT in request.headers.get("accept", "")


def _record_usage(
    database: Database,
    run: AskRun,
    user: User,
    model: str,
    version: str,
    settings: Settings,
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
                latency_ms=run.latency_ms,
                version=version,
            )
    except Exception:  # 记账失败不该把一次成功的问答变成 500
        logger.exception("写入 usage 失败（%s）", version)

    # #12：写入之后检查一次当日阈值。后台线程里跑，响应不等它；Webhook 失败只 warn。
    alerts.schedule(database, settings)
