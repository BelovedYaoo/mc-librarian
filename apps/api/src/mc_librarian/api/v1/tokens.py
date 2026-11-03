"""`/v1/auth/tokens`：个人访问令牌的签发、列表与吊销（契约见 docs/openapi.yaml）。

令牌归属人只能操作自己的令牌；明文只在 `POST` 的 201 里返回一次，此后列表只有 `prefix`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from mc_librarian import audit
from mc_librarian.auth import tokens
from mc_librarian.auth.deps import CurrentUser, DbConn, client_ip
from mc_librarian.db import transaction
from mc_librarian.problems import ProblemException
from mc_librarian.schemas import TokenCreated, TokenCreateRequest, TokenList

router = APIRouter(tags=["auth"])


@router.get(
    "/auth/tokens",
    summary="我的 access token 列表",
    operation_id="listTokens",
)
def list_tokens(user: CurrentUser, conn: DbConn) -> TokenList:
    rows = tokens.list_for_user(conn, user.id)

    return TokenList(tokens=[tokens.row_to_info(row) for row in rows])


@router.post(
    "/auth/tokens",
    status_code=201,
    summary="签发 access token",
    description="明文 token 只在创建时返回一次，之后只能看到前缀。",
    operation_id="createToken",
)
def create_token(
    payload: TokenCreateRequest, request: Request, user: CurrentUser, conn: DbConn
) -> TokenCreated:
    with transaction(conn):
        raw_token, row = tokens.create(
            conn,
            user.id,
            name=payload.name,
            expires_in_days=payload.expires_in_days,
        )
        audit.record(
            conn,
            actor_id=user.id,
            action=audit.ACTION_TOKEN_CREATED,
            target=str(row["prefix"]),
            detail={"name": payload.name, "expires_in_days": payload.expires_in_days},
            ip=client_ip(request),
        )

    return TokenCreated(token=raw_token, info=tokens.row_to_info(row))


@router.delete(
    "/auth/tokens/{tokenId}",
    status_code=204,
    summary="吊销 access token",
    operation_id="revokeToken",
)
def revoke_token(tokenId: str, request: Request, user: CurrentUser, conn: DbConn) -> Response:
    with transaction(conn):
        revoked = tokens.revoke(conn, user.id, tokenId)

        if revoked is None:
            raise ProblemException(404, "资源不存在", "访问令牌不存在或已吊销")

        audit.record(
            conn,
            actor_id=user.id,
            action=audit.ACTION_TOKEN_REVOKED,
            target=str(revoked["prefix"]),
            detail={"name": str(revoked["name"])},
            ip=client_ip(request),
        )

    return Response(status_code=204)
