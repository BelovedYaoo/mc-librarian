"""v1 路由聚合。

目前只有 meta 分组；retrieve / read / answer / auth / admin 由后续 ticket 逐个挂载。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import health

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)

__all__ = ["api_router"]
