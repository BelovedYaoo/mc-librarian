"""`GET /v1/minecraft-versions`：Mojang 清单里的 release 版本列表（仅 admin）。

版本页的「注册索引」对话框用它做可搜索下拉框的数据源，避免手打版本号打错。
只返回 `type == "release"` 的版本，按发布时间倒序；清单读取与缓存见
`minecraft_versions`（TTL 1 小时，网络失败时退回缓存）。
"""

from __future__ import annotations

from fastapi import APIRouter

from mc_librarian import minecraft_versions
from mc_librarian.auth.deps import CurrentAdmin
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import MinecraftVersion, MinecraftVersionList

router = APIRouter(tags=["admin"])

UPSTREAM_TITLE = "上游不可用"


@router.get(
    "/minecraft-versions",
    summary="可注册的 Minecraft 版本列表",
    description=(
        "从 Mojang 版本清单拉取，只返回 `type == \"release\"` 的版本，按发布时间倒序。"
        "进程内缓存 1 小时；网络失败时若缓存可用就返回缓存，否则返回 502。"
    ),
    operation_id="listMinecraftVersions",
    responses={
        403: problem_response("需要管理员权限"),
        502: problem_response("拉取 Mojang 版本清单失败，且没有可用缓存"),
    },
)
def list_minecraft_versions(_admin: CurrentAdmin) -> MinecraftVersionList:
    try:
        releases = minecraft_versions.catalog.releases()
    except minecraft_versions.ManifestError as error:
        raise ProblemException(
            502, UPSTREAM_TITLE, f"拉取 Mojang 版本清单失败：{error}"
        ) from error

    return MinecraftVersionList(
        versions=[
            MinecraftVersion(id=item.id, released_at=item.released_at) for item in releases
        ]
    )
