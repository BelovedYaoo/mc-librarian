"""FastAPI 应用装配。

启动方式：`python -m mc_librarian --reload`（或仓库根 `bun run dev`）。
`/openapi.json`、`/docs` 由 FastAPI 默认提供；契约以 docs/openapi.yaml 为准。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from mc_librarian import __version__
from mc_librarian.api.v1 import api_router
from mc_librarian.config import Settings, get_settings

logger = logging.getLogger("mc_librarian")

# 与 docs/openapi.yaml 的 `servers` / `security` / `components.securitySchemes` 对齐。
SERVERS: Final = [
    {
        "url": "https://mc-librarian.belovedyaoo.top",
        "description": "生产（公网边缘节点 47.122.113.116）",
    },
    {"url": "http://127.0.0.1:8787", "description": "本地开发"},
]

GLOBAL_SECURITY: Final[list[dict[str, list[str]]]] = [{"cookieAuth": []}, {"bearerAuth": []}]

SECURITY_SCHEMES: Final = {
    "cookieAuth": {
        "type": "apiKey",
        "in": "cookie",
        "name": "mc_librarian_session",
    },
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": "个人 access token（`mcl_` 前缀）",
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    source = "MC_LIBRARIAN_DATA_DIR" if settings.data_dir_from_env else "默认（仓库之外）"
    logger.info("data root: %s（来源：%s）", settings.data_dir, source)
    yield


def _build_openapi(app: FastAPI) -> dict[str, Any]:
    """在 FastAPI 默认产物上补全局 security 与 securitySchemes（构造函数不支持）。

    路由级 `openapi_extra={"security": []}` 会覆盖全局值，`/v1/health` 用它保持公开。
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        servers=app.servers,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(SECURITY_SCHEMES)
    schema["security"] = GLOBAL_SECURITY

    app.openapi_schema = schema

    return schema


def create_app(settings: Settings | None = None) -> FastAPI:
    """工厂，便于测试注入临时数据根。"""
    settings = settings or get_settings()
    app = FastAPI(
        title="MC Librarian API",
        summary="Minecraft 源码知识库与问答服务",
        version=__version__,
        servers=SERVERS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.openapi = lambda: _build_openapi(app)  # type: ignore[method-assign]
    app.include_router(api_router)

    return app


app = create_app()
