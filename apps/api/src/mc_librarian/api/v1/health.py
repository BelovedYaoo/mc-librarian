"""`GET /v1/health`：存活探针（契约见 docs/openapi.yaml）。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from mc_librarian import __version__

router = APIRouter(tags=["meta"])


class HealthResponse(BaseModel):
    """两个字段都是 required，与契约一致（不给默认值，否则 OpenAPI 里会掉出 required）。"""

    status: Literal["ok"]
    version: str


@router.get(
    "/health",
    summary="存活探针",
    operation_id="getHealth",
    # 契约里 health 是 `security: []`：公开端点，不受全局鉴权约束。
    openapi_extra={"security": []},
)
async def get_health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
