"""v1 路由聚合。

已挂载：meta（health）、auth（setup / login / logout / me / tokens）。
retrieve / read / answer / admin 由后续 ticket 逐个挂载。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import auth, health, setup, tokens

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(setup.router)
api_router.include_router(auth.router)
api_router.include_router(tokens.router)

__all__ = ["api_router"]
