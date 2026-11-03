"""`/v1/setup`：首次启动创建唯一管理员（契约见 docs/openapi.yaml）。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from mc_librarian.auth import cookies, sessions, users
from mc_librarian.auth.deps import DbConn, EffectiveSettingsDep, client_ip
from mc_librarian.db import transaction
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import SetupRequest, SetupStatus, User

router = APIRouter(tags=["auth"])

SETUP_COMPLETED_TITLE = "已完成初始化"
SETUP_COMPLETED_DETAIL = "管理员已存在，setup 已永久关闭"


@router.get(
    "/setup",
    summary="是否需要初始化管理员",
    description="控制面还没有任何用户时返回 `required: true`。公开端点，无需会话。",
    operation_id="getSetupStatus",
    openapi_extra={"security": []},
)
def get_setup_status(conn: DbConn) -> SetupStatus:
    return SetupStatus(required=not users.any_user(conn))


@router.post(
    "/setup",
    status_code=201,
    summary="创建唯一管理员并直接登录",
    description=(
        "仅在控制面没有任何用户时可用：成功后建 admin、下发会话 cookie 并返回 201；"
        "已初始化时返回 410，不再重建。"
    ),
    operation_id="createFirstAdmin",
    openapi_extra={"security": []},
    responses={
        410: problem_response("已完成初始化"),
        422: problem_response("请求不合法"),
    },
)
def create_first_admin(
    payload: SetupRequest,
    request: Request,
    response: Response,
    conn: DbConn,
    settings: EffectiveSettingsDep,
) -> User:
    """同步 def：argon2 哈希与 sqlite 写入都是阻塞调用，交给 FastAPI 的线程池跑。"""
    # 判定与写入在同一个写事务里：`BEGIN IMMEDIATE` 先拿写锁，两个并发 setup
    # 会串行执行，后到的那个看到 users 非空 → 410。因此事务内的 IntegrityError
    # 不再当作「已初始化」，而是真正的数据异常，原样向上抛（500）。
    with transaction(conn):
        if users.any_user(conn):
            raise ProblemException(410, SETUP_COMPLETED_TITLE, SETUP_COMPLETED_DETAIL)

        user = users.create_admin(
            conn,
            username=payload.username,
            password=payload.password,
        )
        token = sessions.create(
            conn,
            user.id,
            ttl_hours=settings.session_ttl_hours,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

    cookies.set_session_cookie(response, token, settings)

    return user
