"""`GET /v1/admin/settings`：读取实例设置（契约见 docs/openapi.yaml）。

当前**只读**：值直接从运行时配置（`MC_LIBRARIAN_*` 环境变量）派生，没有持久化，`PUT` 也还没实现
（契约里仍声明，随 `#11` 落地）。只返回**真实存在**的配置块，缺的块整块省略——拿契约里的
示例默认值填充等于告诉前端「模型已配置、密钥已设置」这类服务端自己都不知道的事。

`ask.enabled`（`MC_LIBRARIAN_ASK_ENABLED`）与 `security.max_concurrent_asks`
（`MC_LIBRARIAN_MAX_CONCURRENT_ASKS`）是 #8 的问答开关与并发闸门，必须可见。
"""

from __future__ import annotations

from fastapi import APIRouter

from mc_librarian.auth.deps import CurrentAdmin, SettingsDep
from mc_librarian.problems import problem_response
from mc_librarian.schemas import AskSettings, IndexSettings, SecuritySettings, Settings

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/settings",
    summary="读取设置",
    description=(
        "只读：值从运行时配置（`MC_LIBRARIAN_*`）派生，未实现的块整块省略。"
        "持久化与 `PUT` 随 #11 落地。"
    ),
    operation_id="getSettings",
    responses={403: problem_response("需要管理员权限")},
)
def get_settings(_admin: CurrentAdmin, settings: SettingsDep) -> Settings:
    return Settings(
        index=IndexSettings(
            # `Settings.index.java_xmx` 就是反编译时传给 java 的 `-Xmx`。
            enabled=settings.index_enabled,
            java_xmx=settings.decompile_memory,
        ),
        ask=AskSettings(
            enabled=settings.ask_enabled,
            timeout_s=settings.ask_timeout_s,
        ),
        security=SecuritySettings(max_concurrent_asks=settings.max_concurrent_asks),
    )
