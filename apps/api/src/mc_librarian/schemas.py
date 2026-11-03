"""API 请求 / 响应模型。

一律对齐 `docs/openapi.yaml` 的 `components.schemas`：字段名、required 与语义都不许漂移，
`tests/test_contract.py` 会比对运行时 schema 与契约。时间字段用 `datetime`，
FastAPI 会声明成 `type: string, format: date-time`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["admin", "user"]

__all__ = [
    "LoginRequest",
    "Role",
    "SetupRequest",
    "SetupStatus",
    "TokenCreateRequest",
    "TokenCreated",
    "TokenInfo",
    "TokenList",
    "User",
]


class User(BaseModel):
    """对外可见的用户信息：**永远不含** `password_hash`。"""

    id: str
    username: str
    role: Role
    enabled: bool
    created_at: datetime | None = None
    created_by: str | None = None
    last_login_at: datetime | None = None


class SetupStatus(BaseModel):
    """契约里的 `SetupStatus`；`required` 是字段名，与 pydantic 的 required 无关。"""

    required: bool


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=128)


class TokenInfo(BaseModel):
    """契约 `components.schemas.TokenInfo`：**不含** `token_hash`，列表与创建响应共用。"""

    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class TokenCreateRequest(BaseModel):
    name: str = Field(max_length=64)
    expires_in_days: int = Field(default=365, ge=1, le=3650)


class TokenCreated(BaseModel):
    """`POST /v1/auth/tokens` 的 201：明文 `token` 仅此一次返回。"""

    token: str
    info: TokenInfo


class TokenList(BaseModel):
    """`GET /v1/auth/tokens` 的 200。"""

    tokens: list[TokenInfo]
