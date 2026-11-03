"""`/v1/invites/{token}`：邀请链接的校验与接受（契约见 docs/openapi.yaml）。

公开端点（被邀请人还没有账号，自然也还没有会话）：链接本身就是凭证，
明文 token 只在管理员创建用户时返回一次，库里只有 sha256。
接受即设密码 + 激活 + 直接下发会话 cookie（201），与 setup 的体验一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from mc_librarian import audit
from mc_librarian.auth import cookies, invites, sessions, users
from mc_librarian.auth.deps import DbConn, SettingsDep, client_ip
from mc_librarian.db import transaction
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import AcceptInviteRequest, InviteStatus, User

router = APIRouter(tags=["auth"])

INVITE_INVALID_TITLE = "邀请无效"
# 不存在 / 已使用 / 已过期 / 账号被禁用共用一句文案，不暴露链接的具体状态。
INVITE_INVALID_DETAIL = "邀请链接无效、已过期或已被使用"


@router.get(
    "/invites/{token}",
    summary="校验邀请链接",
    description=(
        "链接仍可用时 `valid: true` 并给出对应的用户名；不存在、已使用、已过期或账号被禁用"
        "一律 `valid: false`（不区分具体原因）。公开端点，无需会话。"
    ),
    operation_id="getInvite",
    openapi_extra={"security": []},
    response_model_exclude_none=True,
)
def get_invite(token: str, conn: DbConn) -> InviteStatus:
    row = invites.find(conn, token)

    if row is None or not invites.is_valid(row):
        return InviteStatus(valid=False)

    return InviteStatus(valid=True, username=str(row["username"]))


@router.post(
    "/invites/{token}/accept",
    status_code=201,
    summary="接受邀请（设置密码并登录）",
    description=(
        "被邀请人自设密码，成功后激活账号并**直接下发会话 cookie**（与登录同一套 cookie）。"
        "链接一次性：成功即标记已用，再用返回 404。公开端点，无需会话。"
    ),
    operation_id="acceptInvite",
    openapi_extra={"security": []},
    responses={
        404: problem_response("邀请链接无效"),
        422: problem_response("请求不合法"),
    },
)
def accept_invite(
    token: str,
    payload: AcceptInviteRequest,
    request: Request,
    response: Response,
    conn: DbConn,
    settings: SettingsDep,
) -> User:
    """同步 def：argon2 哈希与 sqlite 写入都是阻塞调用，交给 FastAPI 的线程池跑。"""
    with transaction(conn):
        row = invites.find(conn, token)

        if row is None or not invites.is_valid(row):
            raise ProblemException(404, INVITE_INVALID_TITLE, INVITE_INVALID_DETAIL)

        user_id = str(row["user_id"])
        user = users.set_password(conn, user_id, payload.password)
        invites.mark_used(conn, str(row["id"]))
        raw_session = sessions.create(
            conn,
            user_id,
            ttl_hours=settings.session_ttl_hours,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        audit.record(
            conn,
            actor_id=user_id,
            action=audit.ACTION_INVITE_ACCEPTED,
            target=user.username,
            ip=client_ip(request),
        )

    cookies.set_session_cookie(response, raw_session, settings)

    return user
