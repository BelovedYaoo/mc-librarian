"""`/v1/auth/*`：登录、登出、当前用户（契约见 docs/openapi.yaml）。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from mc_librarian.auth import cookies, passwords, sessions, users
from mc_librarian.auth.deps import CurrentUser, DbConn, SettingsDep, client_ip
from mc_librarian.config import SESSION_COOKIE_NAME
from mc_librarian.db import transaction
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import LoginRequest, User

router = APIRouter(tags=["auth"])

# 契约要求「不区分用户不存在 / 密码错」，所以两种失败共用同一句文案。
INVALID_CREDENTIALS_TITLE = "登录失败"
INVALID_CREDENTIALS_DETAIL = "用户名或密码不正确"
NOT_AUTHENTICATED_TITLE = "未认证"


@router.post(
    "/auth/login",
    summary="登录（换取 session cookie）",
    description=(
        "用户名不存在、密码错误、账号被禁用一律返回同一个 401 与同一句文案；"
        "成功后更新 `last_login_at` 并下发新的会话 cookie。"
    ),
    operation_id="login",
    openapi_extra={"security": []},
    responses={401: problem_response("凭据不正确")},
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    conn: DbConn,
    settings: SettingsDep,
) -> User:
    """同步 def：argon2 校验（故意很慢）与 sqlite 读写都不能占着事件循环。"""
    row = users.find_by_username(conn, payload.username)

    if row is None:
        # 用户不存在也跑一次等价开销的校验，避免用响应时间探测用户名。
        passwords.burn_verification(payload.password)
        raise ProblemException(401, INVALID_CREDENTIALS_TITLE, INVALID_CREDENTIALS_DETAIL)

    matched = passwords.verify_password(payload.password, str(row["password_hash"]))

    if not matched or not bool(row["enabled"]):
        raise ProblemException(401, INVALID_CREDENTIALS_TITLE, INVALID_CREDENTIALS_DETAIL)

    with transaction(conn):
        user = users.touch_last_login(conn, str(row["id"]))
        token = sessions.create(
            conn,
            user.id,
            ttl_hours=settings.session_ttl_hours,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    cookies.set_session_cookie(response, token, settings)

    return user


@router.post(
    "/auth/logout",
    status_code=204,
    summary="注销当前会话",
    description="删除服务端会话行并清除 cookie；没有有效会话时返回 401。",
    operation_id="logout",
    responses={401: problem_response("未认证")},
)
def logout(request: Request, conn: DbConn, settings: SettingsDep) -> Response:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not raw_token:
        raise ProblemException(401, NOT_AUTHENTICATED_TITLE, "没有可注销的会话")

    with transaction(conn):
        deleted = sessions.delete(conn, raw_token)

    if not deleted:
        raise ProblemException(401, NOT_AUTHENTICATED_TITLE, "没有可注销的会话")

    response = Response(status_code=204)
    cookies.clear_session_cookie(response, settings)

    return response


@router.get(
    "/auth/me",
    summary="当前用户",
    operation_id="getMe",
    responses={401: problem_response("未认证")},
)
def get_me(user: CurrentUser) -> User:
    return user
