"""v1 路由聚合。

已挂载：meta（health / versions）、auth（setup / login / logout / me / tokens / invites）、
admin（users / audit / versions 注册 / jobs 任务框架 / settings 只读）、
retrieve+read（search 混合检索 / symbol / references / outline / read）、
answer（`/v1/ask` 问答）。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    admin,
    admin_jobs,
    admin_settings,
    admin_versions,
    answer,
    auth,
    health,
    invites,
    retrieval,
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
api_router.include_router(admin_settings.router)
api_router.include_router(retrieval.router)
api_router.include_router(answer.router)

__all__ = ["api_router"]
