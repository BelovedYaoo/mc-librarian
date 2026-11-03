"""`GET /v1/health`：存活探针（契约见 docs/openapi.yaml）。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from mc_librarian import __version__

router = APIRouter(tags=["meta"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


@router.get("/health", summary="存活探针", operation_id="getHealth")
async def get_health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
