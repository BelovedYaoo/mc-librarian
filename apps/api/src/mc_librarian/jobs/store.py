"""`jobs` / `job_logs` / `versions` 三张表的读写。

与 `auth/users.py` 一样，写函数**不自己开事务**：调用方用 `db.transaction()` 包住
「判定 + 写入」，例如「同一版本已有任务在跑就 409」才是原子的。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from mc_librarian.db import utc_now
from mc_librarian.schemas import Job, JobLog, LogLevel, VersionInfo

# 任务是否还在占用队列（未进入终态）。
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")
# 终态；进入后 cancel 不再有意义。
TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "canceled")

_JOB_COLUMNS = (
    "id, kind, version, status, stage, progress, message, error,"
    " checkpoint, options, cancel_requested, created_at, started_at, finished_at, created_by"
)

_VERSION_COLUMNS = (
    "version, files, tokens, indexed_at, obfuscated, has_summaries, source_size_bytes, db_path"
)


def _loads(raw: object) -> dict[str, Any]:
    """把 JSON 文本列读成 dict；脏数据/空值退回空 dict，不让一行坏数据打断整个队列。"""
    if not isinstance(raw, str) or not raw:
        return {}

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return loaded if isinstance(loaded, dict) else {}


def row_to_job(row: sqlite3.Row) -> Job:
    """`jobs` 行 → API 模型。"""
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        version=row["version"],
        stage=row["stage"],
        progress=float(row["progress"]),
        message=row["message"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_by=row["created_by"],
    )


def checkpoint_of(row: sqlite3.Row) -> dict[str, Any]:
    return _loads(row["checkpoint"])


def options_of(row: sqlite3.Row) -> dict[str, Any]:
    return _loads(row["options"])


# ----------------------------------------------------------------------
# jobs
# ----------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    kind: str,
    version: str,
    options: dict[str, Any],
    created_by: str | None,
) -> Job:
    """入队一个新任务（status=queued，尚未认领）。"""
    job_id = uuid.uuid4().hex

    conn.execute(
        """
        INSERT INTO jobs (
          id, kind, version, status, progress, checkpoint, options, created_at, created_by
        )
        VALUES (?, ?, ?, 'queued', 0, '{}', ?, ?, ?)
        """,
        (job_id, kind, version, json.dumps(options, ensure_ascii=False), utc_now(), created_by),
    )

    created = find(conn, job_id)

    assert created is not None, "刚插入的任务必须能查回来"

    return created


def find(conn: sqlite3.Connection, job_id: str) -> Job | None:
    row = find_row(conn, job_id)

    return None if row is None else row_to_job(row)


def find_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()

    return row


def list_jobs(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 20
) -> list[Job]:
    if status is None:
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (status, limit),
        ).fetchall()

    return [row_to_job(row) for row in rows]


def has_active_job(conn: sqlite3.Connection, version: str) -> bool:
    """该版本是否已有排队/运行中的任务（同版本重复提交 → 409）。"""
    row = conn.execute(
        "SELECT 1 AS hit FROM jobs WHERE version = ? AND status IN ('queued', 'running') LIMIT 1",
        (version,),
    ).fetchone()

    return row is not None


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """原子认领队首任务并置为 running；没有可跑任务返回 None。

    调用方用 `db.transaction()` 包住：`BEGIN IMMEDIATE` 先拿写锁，即使将来多进程
    同时跑 runner，也只有一个能认领同一个任务。已请求取消的 queued 任务直接跳过
    （cancel 端点已把它置为 canceled）。
    """
    row = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = 'queued' AND cancel_requested = 0"
        " ORDER BY created_at, id LIMIT 1"
    ).fetchone()

    if row is None:
        return None

    conn.execute(
        "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?), message = ?"
        " WHERE id = ?",
        (utc_now(), "任务已开始", row["id"]),
    )

    return find_row(conn, str(row["id"]))


def request_cancel(conn: sqlite3.Connection, job_id: str) -> Job:
    """请求取消：queued 直接转 canceled，running 置标志由 runner 在阶段边界收尾。"""
    row = find_row(conn, job_id)

    assert row is not None, "调用方应先确认任务存在"

    if row["status"] == "queued":
        conn.execute(
            "UPDATE jobs SET status = 'canceled', cancel_requested = 1, finished_at = ?,"
            " message = ? WHERE id = ?",
            (utc_now(), "已取消（尚未开始）", job_id),
        )
    else:
        conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))

    updated = find(conn, job_id)

    assert updated is not None, "更新过的任务必须存在"

    return updated


def is_cancel_requested(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()

    return row is not None and bool(row["cancel_requested"])


def mark_stage(
    conn: sqlite3.Connection, job_id: str, *, stage: str, progress: float, message: str
) -> None:
    conn.execute(
        "UPDATE jobs SET stage = ?, progress = ?, message = ? WHERE id = ?",
        (stage, progress, message, job_id),
    )


def save_checkpoint(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    checkpoint: dict[str, Any],
    progress: float,
    message: str,
) -> None:
    conn.execute(
        "UPDATE jobs SET checkpoint = ?, progress = ?, message = ? WHERE id = ?",
        (json.dumps(checkpoint, ensure_ascii=False), progress, message, job_id),
    )


def finish(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    message: str,
    error: str | None = None,
) -> None:
    """写入终态。`canceled`/`failed` 保留 `stage` 与 `progress`，让人知道停在哪里。"""
    if status == "succeeded":
        conn.execute(
            "UPDATE jobs SET status = ?, message = ?, error = ?, finished_at = ?, progress = 1"
            " WHERE id = ?",
            (status, message, error, utc_now(), job_id),
        )
        return

    conn.execute(
        "UPDATE jobs SET status = ?, message = ?, error = ?, finished_at = ? WHERE id = ?",
        (status, message, error, utc_now(), job_id),
    )


# ----------------------------------------------------------------------
# job_logs
# ----------------------------------------------------------------------


def append_log(
    conn: sqlite3.Connection, job_id: str, *, level: LogLevel, message: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO job_logs (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
        (job_id, utc_now(), level, message),
    )

    return int(cursor.lastrowid or 0)


def list_logs(
    conn: sqlite3.Connection, job_id: str, *, after: int = 0, limit: int = 500
) -> list[JobLog]:
    """按 id 升序返回 `id > after` 的日志，供前端增量轮询。"""
    rows = conn.execute(
        "SELECT id, ts, level, message FROM job_logs WHERE job_id = ? AND id > ?"
        " ORDER BY id LIMIT ?",
        (job_id, after, limit),
    ).fetchall()

    return [
        JobLog(id=row["id"], ts=row["ts"], level=row["level"], message=row["message"])
        for row in rows
    ]


# ----------------------------------------------------------------------
# versions
# ----------------------------------------------------------------------


def row_to_version(row: sqlite3.Row) -> VersionInfo:
    return VersionInfo(
        version=row["version"],
        files=int(row["files"]),
        tokens=int(row["tokens"]),
        indexed_at=row["indexed_at"],
        obfuscated=bool(row["obfuscated"]),
        has_summaries=bool(row["has_summaries"]),
        source_size_bytes=int(row["source_size_bytes"]),
    )


def upsert_version(
    conn: sqlite3.Connection,
    *,
    version: str,
    files: int,
    tokens: int,
    obfuscated: bool,
    has_summaries: bool,
    source_size_bytes: int,
    db_path: str | None,
) -> VersionInfo:
    """登记/覆盖一个版本；`indexed_at` 每次写入都刷新为当前时间。"""
    conn.execute(
        """
        INSERT INTO versions (
          version, files, tokens, indexed_at, obfuscated, has_summaries, source_size_bytes, db_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (version) DO UPDATE SET
          files = excluded.files,
          tokens = excluded.tokens,
          indexed_at = excluded.indexed_at,
          obfuscated = excluded.obfuscated,
          has_summaries = excluded.has_summaries,
          source_size_bytes = excluded.source_size_bytes,
          db_path = excluded.db_path
        """,
        (
            version,
            files,
            tokens,
            utc_now(),
            1 if obfuscated else 0,
            1 if has_summaries else 0,
            source_size_bytes,
            db_path,
        ),
    )

    registered = find_version(conn, version)

    assert registered is not None, "刚登记的版本必须能查回来"

    return registered


def find_version(conn: sqlite3.Connection, version: str) -> VersionInfo | None:
    row = conn.execute(
        f"SELECT {_VERSION_COLUMNS} FROM versions WHERE version = ?", (version,)
    ).fetchone()

    return None if row is None else row_to_version(row)


def list_versions(conn: sqlite3.Connection) -> list[VersionInfo]:
    rows = conn.execute(
        f"SELECT {_VERSION_COLUMNS} FROM versions ORDER BY indexed_at DESC, version DESC"
    ).fetchall()

    return [row_to_version(row) for row in rows]
