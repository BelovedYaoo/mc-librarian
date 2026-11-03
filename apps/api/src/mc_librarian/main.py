"""FastAPI 应用装配。

启动方式：`python -m mc_librarian --reload`（或仓库根 `bun run dev`）。
`/openapi.json`、`/docs` 由 FastAPI 默认提供；契约以 docs/openapi.yaml 为准。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from mc_librarian import __version__
from mc_librarian.answer import AskLimiter
from mc_librarian.api.v1 import api_router
from mc_librarian.api.v1.admin_versions import read_index_stats
from mc_librarian.config import Settings, get_settings
from mc_librarian.db import Database, transaction
from mc_librarian.indexing.layout import index_path
from mc_librarian.indexing.summarize import warmup_segmentation
from mc_librarian.indexing.text import warmup_token_counting
from mc_librarian.jobs import JobRunner, store
from mc_librarian.problems import component_schema, install_exception_handlers
from mc_librarian.schemas import answer_schemas
from mc_librarian.secretbox import require_secret

logger = logging.getLogger("mc_librarian")

# 最小 logging 配置：uvicorn 只配自己的 logger，不配 root，导致这里的 logger.info 被吞掉。
LOG_FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

# 与 docs/openapi.yaml 的 `servers` / `security` / `components.securitySchemes` 对齐。
SERVERS: Final = [
    {
        "url": "https://mc-librarian.belovedyaoo.top",
        "description": "生产（公网边缘节点 47.122.113.116）",
    },
    {"url": "http://127.0.0.1:8787", "description": "本地开发"},
]

GLOBAL_SECURITY: Final[list[dict[str, list[str]]]] = [{"cookieAuth": []}, {"bearerAuth": []}]

# 与 docs/openapi.yaml 的 `info.description` / `tags` 对齐（逐字一致，由 test_contract 校验）。
DESCRIPTION: Final = (
    "面向 agent 与人类、带账号体系的代码知识库服务。"
    "对每个 Minecraft 版本提供三路混合检索：\n"
    "\n"
    "- **summary** — 中文类摘要（中文问题直接映射到类）\n"
    "- **symbol** — 类/方法/字段符号表（tree-sitter 派生）\n"
    "- **literal** — 源码字面量与正则检索（ripgrep 派生）\n"
    "\n"
    "设计约束：检索接口无状态、`version` 显式必传（不支持 `latest`）；"
    "响应带 `why` 与\n"
    "`suggestions`；调用方用 `max_tokens` 控制上下文预算；"
    "索引由 `JobRunner` 异步执行，\n"
    "接口只返回 `202 + job_id`。\n"
)

TAGS: Final[list[dict[str, str]]] = [
    {"name": "meta"},
    {"name": "retrieve"},
    {"name": "read"},
    {"name": "answer"},
    {"name": "auth"},
    {"name": "admin"},
]

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
    database: Database = app.state.db
    # 主密钥先查：`settings` 表里的密钥用 AES-GCM 加密，没有主密钥服务无法读写它们，
    # 而自动生成并落盘会让重启后旧密文解不开——所以缺失/过短直接启动失败。
    require_secret(settings.secret)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    source = "MC_LIBRARIAN_DATA_DIR" if settings.data_dir_from_env else "默认（仓库之外）"

    # 问答并发闸门随应用创建：槽位数不在这里定，`/v1/ask` 每次拿当前有效上限（设置页可改）。
    app.state.ask_limiter = AskLimiter()

    version = database.initialize()

    logger.info("data root: %s（来源：%s）", settings.data_dir, source)
    logger.info("control db: %s（schema v%d）", database.path, version)

    # token 口径预热：tiktoken 首次使用会联网拉 BPE 词表，生产节点拉不到时会阻塞到 HTTP 超时
    # 再静默退回常数估算。启动时预热一次并把口径写进 INFO 日志，让漂移可见。
    logger.info("%s", warmup_token_counting())

    # jieba 词典预热：首次分词会同步加载词典（本机实测约 0.5 s），落在第一次摘要查询上就是
    # 一次阻塞请求。与 token 口径一样，把「真的加载了」写进启动日志。
    logger.info("%s", warmup_segmentation())

    # 一次性回填历史遗留的 `db_path`（NULL 行）：只在启动时跑，不建定时任务。
    # 回填失败（如 control 库 busy）不能拖垮启动：只记 error 日志，服务照常起来。
    try:
        backfilled = backfill_version_db_paths(database, settings.data_dir)
    except Exception:
        backfilled = []
        logger.exception("回填历史版本的 db_path 失败，已跳过（不影响启动）")

    if backfilled:
        logger.info(
            "回填 %d 个历史版本的 db_path：%s", len(backfilled), "、".join(backfilled)
        )

    # 任务队列随应用启动：单进程内并发 1 串行消费，退出时等当前任务收尾。
    runner = JobRunner(settings, database)
    app.state.job_runner = runner
    await runner.start()

    try:
        yield
    finally:
        await runner.stop()


def backfill_version_db_paths(database: Database, data_dir: Path) -> list[str]:
    """启动时一次性回填历史遗留的 `db_path`，返回被补写的版本号。

    本次修复前注册/产出的版本行 `db_path` 为 NULL，即使 `db/<version>.sqlite` 存在也永远
    404。只补 `db_path IS NULL` 且索引文件**有效**的行；已有值的行不动，重复启动幂等。

    有效性复用注册路径的 `read_index_stats`：空文件/截断文件不是合法索引，回填它们只会
    把检索端点的 404 换成 500，所以跳过并记 warn。
    """
    filled: list[str] = []

    with database.connect() as conn, transaction(conn):
        for version in store.list_versions_missing_db_path(conn):
            path = index_path(data_dir, version)

            if not path.is_file():
                continue

            if read_index_stats(path) is None:
                logger.warning("跳过回填 db_path：索引文件无效（版本 %s）：%s", version, path)
                continue

            store.set_db_path(conn, version, str(path))
            filled.append(version)

    return filled


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
    schema["info"]["description"] = DESCRIPTION
    schema["tags"] = TAGS

    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(SECURITY_SCHEMES)
    # 错误响应统一引用 `#/components/schemas/Problem`，这里保证它一定在 components 里。
    components.setdefault("schemas", {}).setdefault("Problem", component_schema())
    # `/v1/ask` 的 200 有两个 content type，schema 只能写成裸 `$ref`；裸 `$ref` 不会自动进
    # components（同 `Problem`），所以显式补上（含嵌套模型）。
    for name, definition in answer_schemas().items():
        components["schemas"].setdefault(name, definition)
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
    app.state.db = Database.for_data_dir(settings.data_dir)
    app.openapi = lambda: _build_openapi(app)  # type: ignore[method-assign]
    install_exception_handlers(app)
    app.include_router(api_router)

    return app


app = create_app()
