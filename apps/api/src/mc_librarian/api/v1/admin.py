"""`/v1/admin/users` 与 `/v1/admin/audit`：用户管理与审计（契约见 docs/openapi.yaml）。

所有端点都走 `CurrentAdmin`：前端隐藏菜单只是体验，授权判定在这里。
「判定 + 写入 + 记审计」放在同一个 `transaction()` 里，保证不会出现改了一半、账没记的情况。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response

from mc_librarian import audit
from mc_librarian.auth import invites, sessions, users
from mc_librarian.auth.deps import CurrentAdmin, DbConn, EffectiveSettingsDep, client_ip
from mc_librarian.db import transaction
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import (
    AuditList,
    CreatedUser,
    CreateUserRequest,
    PatchUserRequest,
    Role,
    User,
    UserList,
)

router = APIRouter(tags=["admin"])

USER_NOT_FOUND_TITLE = "资源不存在"
USER_NOT_FOUND_DETAIL = "用户不存在"
USERNAME_TAKEN_TITLE = "冲突"
USERNAME_TAKEN_DETAIL = "用户名已存在"
LAST_ADMIN_TITLE = "冲突"
LAST_ADMIN_DETAIL = "至少需要保留一个启用的管理员"


def invite_url(raw_token: str) -> str:
    """邀请链接（站点根下的相对路径）。前端用 `window.location.origin` 拼成绝对地址。"""
    return f"/invite/{raw_token}"


@router.get(
    "/admin/users",
    summary="用户列表",
    operation_id="listUsers",
    responses={
        401: problem_response("未认证"),
        403: problem_response("需要管理员权限"),
    },
)
def list_users(admin: CurrentAdmin, conn: DbConn) -> UserList:
    return UserList(users=users.list_all(conn))


@router.post(
    "/admin/users",
    status_code=201,
    summary="创建用户",
    description="创建后返回一次性邀请链接；管理员不设置也不需要知道用户密码。",
    operation_id="createUser",
    responses={
        401: problem_response("未认证"),
        403: problem_response("需要管理员权限"),
        409: problem_response("用户名已存在"),
    },
)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    admin: CurrentAdmin,
    conn: DbConn,
    settings: EffectiveSettingsDep,
) -> CreatedUser:
    with transaction(conn):
        if users.find_by_username(conn, payload.username) is not None:
            raise ProblemException(409, USERNAME_TAKEN_TITLE, USERNAME_TAKEN_DETAIL)

        user = users.create_user(
            conn,
            username=payload.username,
            role=payload.role,
            created_by=admin.id,
        )
        raw_token = invites.create(conn, user.id, ttl_hours=settings.invite_ttl_hours)

        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_USER_CREATED,
            target=user.username,
            detail={"role": user.role},
            ip=client_ip(request),
        )
        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_INVITE_CREATED,
            target=user.username,
            detail={"expires_in_hours": settings.invite_ttl_hours},
            ip=client_ip(request),
        )

    return CreatedUser(user=user, invite_url=invite_url(raw_token))


@router.patch(
    "/admin/users/{userId}",
    summary="修改角色或启用状态",
    description="不允许把最后一个 admin 降级或停用；禁用会同时删除该用户的全部会话。",
    operation_id="patchUser",
    responses={
        401: problem_response("未认证"),
        403: problem_response("需要管理员权限"),
        404: problem_response("用户不存在"),
        409: problem_response("违反最后管理员保护"),
    },
)
def patch_user(
    userId: str,
    payload: PatchUserRequest,
    request: Request,
    admin: CurrentAdmin,
    conn: DbConn,
) -> User:
    with transaction(conn):
        row = users.find_by_id(conn, userId)

        if row is None:
            raise ProblemException(404, USER_NOT_FOUND_TITLE, USER_NOT_FOUND_DETAIL)

        current_role: Role = row["role"]
        current_enabled = bool(row["enabled"])
        new_role: Role = payload.role if payload.role is not None else current_role
        new_enabled = payload.enabled if payload.enabled is not None else current_enabled

        # 最后一个**启用**的 admin：降级与停用都拒绝（禁用中的 admin 不算数）。
        if (
            current_role == "admin"
            and current_enabled
            and (new_role != "admin" or not new_enabled)
            and users.count_enabled_admins(conn) <= 1
        ):
            raise ProblemException(409, LAST_ADMIN_TITLE, LAST_ADMIN_DETAIL)

        user = users.row_to_user(row)

        if new_role != current_role:
            user = users.set_role(conn, userId, new_role)
            audit.record(
                conn,
                actor_id=admin.id,
                action=audit.ACTION_USER_ROLE_CHANGED,
                target=user.username,
                detail={"from": current_role, "to": new_role},
                ip=client_ip(request),
            )

        if new_enabled != current_enabled:
            user = users.set_enabled(conn, userId, new_enabled)
            # 禁用即踢下线：会话行全部删掉，旧 cookie 立刻失效。
            revoked = 0 if new_enabled else sessions.delete_for_user(conn, userId)
            detail: dict[str, Any] | None = None if new_enabled else {"sessions_revoked": revoked}

            audit.record(
                conn,
                actor_id=admin.id,
                action=audit.ACTION_USER_ENABLED if new_enabled else audit.ACTION_USER_DISABLED,
                target=user.username,
                detail=detail,
                ip=client_ip(request),
            )

    return user


@router.delete(
    "/admin/users/{userId}",
    status_code=204,
    summary="删除用户",
    description=(
        "硬删一个用户：会话、PAT、邀请随人删除，问答历史一并删除；"
        "审计日志、任务、用量里的引用置 NULL 保留。"
        "不能删自己，也不能删最后一个启用的管理员。"
    ),
    operation_id="deleteUser",
    responses={
        204: {"description": "已删除"},
        401: problem_response("未认证"),
        403: problem_response("需要管理员权限"),
        404: problem_response("用户不存在"),
        409: problem_response("违反最后管理员保护，或试图删除自己"),
    },
)
def delete_user(userId: str, request: Request, admin: CurrentAdmin, conn: DbConn) -> Response:
    with transaction(conn):
        row = users.find_by_id(conn, userId)

        if row is None:
            raise ProblemException(404, USER_NOT_FOUND_TITLE, USER_NOT_FOUND_DETAIL)

        if userId == admin.id:
            # 删自己会立刻把自己的会话带走、并可能让系统没有管理员；不允许，改用别的管理员操作。
            raise ProblemException(409, "冲突", "不能删除自己，请让另一个管理员操作")

        # 最后一个启用的 admin 不能删（与「不能停用/降级」同一条保护）。
        is_last_admin = (
            row["role"] == "admin"
            and bool(row["enabled"])
            and users.count_enabled_admins(conn) <= 1
        )

        if is_last_admin:
            raise ProblemException(409, LAST_ADMIN_TITLE, LAST_ADMIN_DETAIL)

        username = str(row["username"])
        users.delete(conn, userId)
        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_USER_DELETED,
            target=username,
            detail={"role": str(row["role"])},
            ip=client_ip(request),
        )

    return Response(status_code=204)


@router.get(
    "/admin/audit",
    summary="审计日志",
    operation_id="listAudit",
    responses={
        401: problem_response("未认证"),
        403: problem_response("需要管理员权限"),
    },
)
def list_audit(
    admin: CurrentAdmin,
    conn: DbConn,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    actor: Annotated[str | None, Query()] = None,
) -> AuditList:
    return AuditList(entries=audit.list_entries(conn, limit=limit, actor=actor))
