"""FastAPI 应用装配。

启动方式：`uvicorn mc_librarian.main:app --reload`（或仓库根 `bun run dev`）。
`/openapi.json`、`/docs` 由 FastAPI 默认提供；契约以 docs/openapi.yaml 为准。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mc_librarian import __version__
from mc_librarian.api.v1 import api_router
from mc_librarian.config import Settings, get_settings

logger = logging.getLogger("mc_librarian")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("data root: %s", settings.data_dir)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """工厂，便于测试注入临时数据根。"""
    settings = settings or get_settings()
    app = FastAPI(
        title="MC Librarian API",
        summary="Minecraft 源码知识库与问答服务",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(api_router)
    return app


app = create_app()
