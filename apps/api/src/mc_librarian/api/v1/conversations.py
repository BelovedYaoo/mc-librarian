"""历史会话端点（问答页左栏）：列表 / 详情 / 删除。

契约见 `docs/openapi.yaml` 的 `/v1/conversations*`。三条端点都只要求登录（`user` 即可），
且**严格按用户隔离**：`conversations.find_owned` 同时按 id 与 `user_id` 查，别人的会话一律
404（不是 403——不泄露会话是否存在）。会话的写入时机在 `/v1/ask`（见 `api/v1/answer.py`）：
首次提问自动建会话，收尾时写一条助手消息。
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, Response

from mc_librarian import conversations
from mc_librarian.answer import AskRuns
from mc_librarian.auth.deps import CurrentUser, DbConn
from mc_librarian.db import transaction
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import ConversationDetail, ConversationList

router = APIRouter()


def _runs(request: Request) -> AskRuns:
    """本实例的「进行中问答」表（`lifespan` 里创建）。"""
    runs: AskRuns = request.app.state.ask_runs

    return runs

NOT_FOUND_TITLE: Final = "资源不存在"


def _responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """错误响应：未认证 / 资源不存在（每次构造新字典，避免多条路由共享可变对象）。"""
    mapping = {
        401: problem_response("未认证"),
        404: problem_response("会话不存在，或不属于当前用户"),
    }

    return {status: mapping[status] for status in statuses}


@router.get(
    "/conversations",
    tags=["answer"],
    summary="我的历史会话列表",
    description=(
        "只返回当前用户自己的会话，按 `updated_at` 降序；`message_count` 是会话内的消息条数。"
    ),
    operation_id="listConversations",
    responses=_responses(401),
)
def list_conversations(
    conn: DbConn,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConversationList:
    return ConversationList(
        conversations=conversations.list_summaries(conn, user_id=user.id, limit=limit)
    )


@router.get(
    "/conversations/{conversationId}",
    tags=["answer"],
    summary="历史会话详情",
    description=(
        "会话标题与全部消息（含引用与用量）。别人的会话一律 404。"
        "`running` 为真表示这轮回答**仍在后端生成**（客户端断开会话不会中断它），"
        "前端可据此接到 `/v1/conversations/{conversationId}/stream` 继续看。"
    ),
    operation_id="getConversation",
    responses=_responses(401, 404),
)
def get_conversation(
    request: Request,
    conn: DbConn,
    user: CurrentUser,
    conversationId: str,
) -> ConversationDetail:
    row = conversations.find_owned(conn, conversation_id=conversationId, user_id=user.id)

    if row is None:
        raise ProblemException(404, NOT_FOUND_TITLE, "会话不存在")

    return ConversationDetail(
        id=str(row["id"]),
        title=str(row["title"]),
        messages=conversations.messages(conn, conversation_id=conversationId),
        running=_runs(request).active(conversationId) is not None,
    )


@router.delete(
    "/conversations/{conversationId}",
    status_code=204,
    tags=["answer"],
    summary="删除历史会话",
    description="级联删除该会话的全部消息；别人的会话一律 404。",
    operation_id="deleteConversation",
    responses=_responses(401, 404),
)
def delete_conversation(conn: DbConn, user: CurrentUser, conversationId: str) -> Response:
    with transaction(conn):
        row = conversations.find_owned(conn, conversation_id=conversationId, user_id=user.id)

        if row is None:
            raise ProblemException(404, NOT_FOUND_TITLE, "会话不存在")

        conversations.remove(conn, conversation_id=conversationId)

    return Response(status_code=204)
