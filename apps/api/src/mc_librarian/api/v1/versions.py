"""`/v1/versions`：已索引版本列表（契约见 docs/openapi.yaml）。

tag 是 `meta`，但走全局 security——登录后可读；写入口在 `/v1/admin/versions`。
"""

from __future__ import annotations

from fastapi import APIRouter

from mc_librarian.auth.deps import CurrentUser, DbConn
from mc_librarian.jobs import store
from mc_librarian.problems import problem_response
from mc_librarian.schemas import VersionList

router = APIRouter(tags=["meta"])


@router.get(
    "/versions",
    summary="已索引版本列表",
    operation_id="listVersions",
    responses={401: problem_response("未认证")},
)
def list_versions(conn: DbConn, _user: CurrentUser) -> VersionList:
    return VersionList(versions=store.list_versions(conn))
