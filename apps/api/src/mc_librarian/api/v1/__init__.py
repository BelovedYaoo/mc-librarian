"""v1 路由聚合。

已挂载：meta（health / versions）、auth（setup / login / logout / me / tokens / invites）、
admin（users / audit / versions 注册 / jobs 任务框架）。
retrieve / read / answer 由后续 ticket 逐个挂载。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    admin,
    admin_jobs,
    admin_versions,
    auth,
    health,
    invites,
    setup,
    tokens,
    versions,
)

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(setup.router)
api_router.include_router(auth.router)
api_router.include_router(tokens.router)
api_router.include_router(invites.router)
api_router.include_router(admin.router)
api_router.include_router(versions.router)
api_router.include_router(admin_versions.router)
api_router.include_router(admin_jobs.router)

__all__ = ["api_router"]
