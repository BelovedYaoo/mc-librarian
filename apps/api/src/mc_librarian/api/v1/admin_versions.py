"""`POST /v1/admin/versions`：注册已上传的索引（契约见 docs/openapi.yaml）。

节点侧不构建索引：管理员把本地生成的 `db/<version>.sqlite` 放进数据根，再调本接口。
服务端校验文件存在且是合法索引（有 `files` 表），然后把文件数 / token 总量 / 摘要标记写进版本
注册表。`source_size_bytes` 写「源码树字节数」（与任务产出一致）：数据根里没有
`<data_dir>/src/<version>/` 时**保留原值**（首次注册写 0），而不是拿索引 sqlite 的大小充数。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request

from mc_librarian import audit
from mc_librarian.auth.deps import CurrentAdmin, DbConn, SettingsDep, client_ip
from mc_librarian.db import transaction
from mc_librarian.indexing.build import source_tree_size_bytes
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store
from mc_librarian.problems import TITLE_BAD_REQUEST, ProblemException, problem_response
from mc_librarian.schemas import RegisterVersionRequest, VersionInfo

router = APIRouter(prefix="/admin", tags=["admin"])



@dataclass(frozen=True)
class IndexStats:
    """从索引 sqlite 里读出的注册信息。"""

    files: int
    tokens: int
    has_summaries: bool


def read_index_stats(path: Path) -> IndexStats | None:
    """只读打开索引并读出统计；不是合法索引时返回 None（调用方回 422 / 跳过回填）。

    `files` 是 `db/<version>.sqlite` 的必备表（`build_index` 无条件创建，§5.2）：0 字节文件
    能被 SQLite 当空库打开，但没有这张表，同样不算有效索引。`summaries` 是 #7 追加的，
    缺表时按 false 处理。
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None

    try:
        if not _has_table(conn, "files"):
            return None

        files = _count(conn, "files")
        tokens = _sum_tokens(conn)
        has_summaries = _has_table(conn, "summaries")
    except sqlite3.DatabaseError:
        # 文件存在但不是 sqlite（或已损坏）：校验失败。
        return None
    finally:
        conn.close()

    return IndexStats(files=files, tokens=tokens, has_summaries=has_summaries)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 AS hit FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()

    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _has_table(conn, table):
        return 0

    row = conn.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()

    return int(row[0]) if row is not None else 0


def _sum_tokens(conn: sqlite3.Connection) -> int:
    if not _has_table(conn, "files"):
        return 0

    row = conn.execute("SELECT COALESCE(SUM(tokens), 0) AS total FROM files").fetchone()

    return int(row[0]) if row is not None else 0


@router.post(
    "/versions",
    status_code=201,
    summary="注册已上传的索引",
    description=(
        "管理员把本地生成的 `db/<version>.sqlite` 放进数据根后调用本接口，"
        "服务端校验文件并写入版本注册表。文件不存在或校验失败返回 422。"
    ),
    operation_id="registerVersion",
    responses={
        403: problem_response("需要管理员权限"),
        422: problem_response("请求不合法"),
    },
)
def register_version(
    payload: RegisterVersionRequest,
    request: Request,
    conn: DbConn,
    admin: CurrentAdmin,
    settings: SettingsDep,
) -> VersionInfo:
    path = index_path(settings.data_dir, payload.version)

    if not path.is_file():
        raise ProblemException(422, TITLE_BAD_REQUEST, f"索引文件不存在：{path}")

    stats = read_index_stats(path)

    if stats is None:
        raise ProblemException(422, TITLE_BAD_REQUEST, f"索引文件不是合法的 sqlite：{path}")

    # `source_size_bytes` 的语义是「源码树字节数」（与任务产出一致），不是索引 sqlite 的大小。
    # 注册时源码树不一定在数据根里（只上传了 db/）：此时传 None，让 upsert 保留原值而不是
    # 把上次算出的非零值清零；首次注册仍写 0。
    source_tree = source_root(settings.data_dir, payload.version)
    source_size_bytes = source_tree_size_bytes(source_tree) if source_tree.is_dir() else None

    with transaction(conn):
        registered = store.register_version(
            conn,
            version=payload.version,
            files=stats.files,
            tokens=stats.tokens,
            obfuscated=payload.obfuscated,
            has_summaries=stats.has_summaries,
            source_size_bytes=source_size_bytes,
            db_path=str(path),
        )
        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_VERSION_REGISTERED,
            target=registered.version,
            detail={
                "files": registered.files,
                "tokens": registered.tokens,
                "obfuscated": bool(registered.obfuscated),
                "has_summaries": bool(registered.has_summaries),
            },
            ip=client_ip(request),
        )

        return registered
