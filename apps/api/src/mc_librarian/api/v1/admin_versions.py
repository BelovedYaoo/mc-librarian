"""`POST /v1/admin/versions`：注册已上传的索引（契约见 docs/openapi.yaml）。

节点侧不构建索引：管理员把本地生成的 `db/<version>.sqlite` 放进数据根，再调本接口。
服务端校验文件存在且是合法 sqlite，然后把文件数 / token 总量 / 摘要标记写进版本注册表。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter

from mc_librarian.auth.deps import AdminUser, DbConn, SettingsDep
from mc_librarian.db import transaction
from mc_librarian.jobs import store
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import RegisterVersionRequest, VersionInfo

router = APIRouter(prefix="/admin", tags=["admin"])

BAD_REQUEST_TITLE = "请求不合法"

# 索引 sqlite 放在数据根的 `db/` 下（见 docs/00-overview.md §4、§5.2）。
DB_DIR_NAME = "db"


@dataclass(frozen=True)
class IndexStats:
    """从索引 sqlite 里读出的注册信息。"""

    files: int
    tokens: int
    has_summaries: bool


def index_path(data_dir: Path, version: str) -> Path:
    return data_dir / DB_DIR_NAME / f"{version}.sqlite"


def read_index_stats(path: Path) -> IndexStats | None:
    """只读打开索引并读出统计；不是合法 sqlite 时返回 None（调用方回 422）。

    `files` / `summaries` 是 `db/<version>.sqlite` 的既定表名（§5.2）；缺表时按 0/false 处理。
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None

    try:
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
    conn: DbConn,
    _admin: AdminUser,
    settings: SettingsDep,
) -> VersionInfo:
    path = index_path(settings.data_dir, payload.version)

    if not path.is_file():
        raise ProblemException(422, BAD_REQUEST_TITLE, f"索引文件不存在：{path}")

    stats = read_index_stats(path)

    if stats is None:
        raise ProblemException(422, BAD_REQUEST_TITLE, f"索引文件不是合法的 sqlite：{path}")

    with transaction(conn):
        return store.upsert_version(
            conn,
            version=payload.version,
            files=stats.files,
            tokens=stats.tokens,
            obfuscated=payload.obfuscated,
            has_summaries=stats.has_summaries,
            source_size_bytes=path.stat().st_size,
            db_path=str(path),
        )
