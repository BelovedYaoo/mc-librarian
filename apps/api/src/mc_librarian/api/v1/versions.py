"""`/v1/versions`：已索引版本列表（契约见 docs/openapi.yaml）。

tag 是 `meta`，但走全局 security——登录后可读；写入口在 `/v1/admin/versions`。

版本行带摘要覆盖率（已生成 / 类总数，issue #7 规格 §8）：从每个版本的索引库实时读，
不缓存——摘要覆盖率是索引产物的属性，注册表里的 `has_summaries` 只是登记时的快照。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from mc_librarian.auth.deps import CurrentUser, DbConn
from mc_librarian.indexing.summarize import read_summary_coverage
from mc_librarian.jobs import store
from mc_librarian.problems import problem_response
from mc_librarian.schemas import VersionInfo, VersionList

router = APIRouter(tags=["meta"])


@router.get(
    "/versions",
    summary="已索引版本列表",
    operation_id="listVersions",
    responses={401: problem_response("未认证")},
)
def list_versions(conn: DbConn, _user: CurrentUser) -> VersionList:
    paths = store.list_version_db_paths(conn)

    return VersionList(
        versions=[
            _with_coverage(item, paths.get(item.version)) for item in store.list_versions(conn)
        ]
    )


def _with_coverage(version: VersionInfo, db_path: str | None) -> VersionInfo:
    """补上摘要覆盖率与索引文件大小；文件缺失/不可读时保持 `None`，不把列表接口变成 500。"""
    if not db_path:
        return version

    path = Path(db_path)
    size = path.stat().st_size if path.is_file() else None
    coverage = read_summary_coverage(path)

    if coverage is None:
        return version.model_copy(update={"index_size_bytes": size})

    return version.model_copy(
        update={
            "summary_count": coverage.summaries,
            "class_count": coverage.classes,
            "index_size_bytes": size,
        }
    )
