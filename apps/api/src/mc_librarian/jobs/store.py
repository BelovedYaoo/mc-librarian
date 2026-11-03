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

# 可以重试的终态：保留 checkpoint 重新入队，从下一个未完成阶段续跑。
RETRYABLE_STATUSES: tuple[str, ...] = ("failed", "canceled")

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


def request_cancel(conn: sqlite3.Connection, job_id: str) -> Job | None:
    """请求取消：queued 直接转 canceled，running 置标志由 runner 在阶段边界收尾。

    UPDATE 带状态守卫并在调用方事务（`BEGIN IMMEDIATE`）里执行，按 rowcount 判定：
    任务已进入终态时返回 `None`（调用方回 409），不会在「判定与写入」之间被并发改写。
    """
    queued = conn.execute(
        "UPDATE jobs SET status = 'canceled', cancel_requested = 1, finished_at = ?,"
        " message = ? WHERE id = ? AND status = 'queued'",
        (utc_now(), "已取消（尚未开始）", job_id),
    )

    if queued.rowcount == 0:
        running = conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status = 'running'",
            (job_id,),
        )

        if running.rowcount == 0:
            return None

    return find(conn, job_id)


def is_cancel_requested(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()

    return row is not None and bool(row["cancel_requested"])


def requeue(conn: sqlite3.Connection, job_id: str) -> Job | None:
    """把 failed/canceled 任务重新放回队列，返回重排后的任务；其他状态返回 `None`。

    UPDATE 带状态守卫并在调用方事务里执行，按 rowcount 判定（TOCTOU 安全）。
    `checkpoint` 原样保留，runner 认领后跳过已完成的阶段；`cancel_requested` 一并清零，
    否则 `claim_next`（只认 `cancel_requested = 0`）永远不会再认领它；`started_at` 也清空，
    让下一次认领写入新的开始时间。`stage`/`progress`/`message`/`error` 是上一轮执行的
    残留，一并清空，避免排队中的任务显示旧阶段与部分进度。
    """
    placeholders = ", ".join("?" for _ in RETRYABLE_STATUSES)
    cursor = conn.execute(
        "UPDATE jobs SET status = 'queued', cancel_requested = 0, stage = NULL, progress = 0,"
        " message = NULL, error = NULL, started_at = NULL, finished_at = NULL"
        f" WHERE id = ? AND status IN ({placeholders})",
        (job_id, *RETRYABLE_STATUSES),
    )

    if cursor.rowcount == 0:
        return None

    return find(conn, job_id)


def requeue_running(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """启动恢复：收回遗留的 `running` 任务，返回 (重新排队的 id, 直接取消的 id)。

    runner 并发 1 且只在进程内，`running` 只可能来自上一次进程生命周期；而 `claim_next`
    只认 `queued`，所以不收回来这些任务永远不会再跑。**不能丢取消意图**：
    `cancel_requested=1` 的行说明用户已请求取消，直接置为 `canceled`（而不是重新排队后又
    跑起来）；其余行才收回 `queued`，`started_at` 是「当前这次执行」的瞬态状态，一并清空。

    「进程启动即收回全部 running」依赖单进程假设（runner 并发 1）；多 worker 需要心跳 /
    租约才能区分「本进程还在跑」与「上个进程死了」，见技术债 #19。
    """
    requeued = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM jobs WHERE status = 'running' AND cancel_requested = 0"
            " ORDER BY created_at, id"
        ).fetchall()
    ]
    canceled = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM jobs WHERE status = 'running' AND cancel_requested = 1"
            " ORDER BY created_at, id"
        ).fetchall()
    ]

    if requeued:
        conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL, message = ?"
            " WHERE status = 'running' AND cancel_requested = 0",
            ("服务重启，任务重新排队",),
        )

    if canceled:
        conn.execute(
            "UPDATE jobs SET status = 'canceled', finished_at = ?, message = ?"
            " WHERE status = 'running' AND cancel_requested = 1",
            (utc_now(), "服务重启：任务在取消请求后终止"),
        )

    return requeued, canceled


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
    expected_status: str | None = None,
) -> int:
    """写入终态，返回实际更新的行数。

    `canceled`/`failed` 保留 `stage` 与 `progress`，让人知道停在哪里。`expected_status`
    给定时只有当前状态匹配才写（状态守卫）：兜底失败路径只覆盖 `running`，避免把已经
    `succeeded` 的任务改回 `failed`。
    """
    progress = ", progress = 1" if status == "succeeded" else ""
    guard = " AND status = ?" if expected_status is not None else ""
    params: tuple[Any, ...] = (status, message, error, utc_now(), job_id)

    if expected_status is not None:
        params = (*params, expected_status)

    cursor = conn.execute(
        f"UPDATE jobs SET status = ?, message = ?, error = ?, finished_at = ?{progress}"
        f" WHERE id = ?{guard}",
        params,
    )

    return int(cursor.rowcount)


def finish_succeeded_or_canceled(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    succeeded_message: str,
    canceled_message: str,
) -> str | None:
    """任务跑完所有阶段后的终态写入：取消标志与终态在同一条 UPDATE 里判定。

    `cancel_requested=0` → `succeeded`（`progress=1`），否则 → `canceled`；按 rowcount 判定
    实际写入的终态并返回，`None` 表示任务已不在 `running`（防御性，正常不会发生）。这样
    finalize 执行期间到达的取消不会被「先查标志再写终态」的两次连接吞掉。
    """
    succeeded = conn.execute(
        "UPDATE jobs SET status = 'succeeded', message = ?, error = NULL, finished_at = ?,"
        " progress = 1 WHERE id = ? AND status = 'running' AND cancel_requested = 0",
        (succeeded_message, utc_now(), job_id),
    )

    if succeeded.rowcount:
        return "succeeded"

    canceled = conn.execute(
        "UPDATE jobs SET status = 'canceled', message = ?, finished_at = ?"
        " WHERE id = ? AND status = 'running' AND cancel_requested = 1",
        (canceled_message, utc_now(), job_id),
    )

    return "canceled" if canceled.rowcount else None


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


def register_version(
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
    """管理员注册：写全量字段（含 `db_path`/`has_summaries`），`indexed_at` 刷新为当前时间。"""
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


def insert_version_if_absent(
    conn: sqlite3.Connection,
    *,
    version: str,
    files: int,
    tokens: int,
) -> VersionInfo | None:
    """任务产出登记版本：仅在版本**尚未注册**时插入一行，已存在则什么都不写。

    版本不存在时插入一行，其余字段用「未 remap、无摘要」的保守默认值
    （`obfuscated=0`、`has_summaries=0`、`source_size_bytes=0`、`db_path=NULL`）。
    版本已存在（管理员注册过）时**一个字段都不动**，返回 `None` 让调用方知道没写；
    任务产出不是权威索引，想替换已注册版本请重新注册（docs/01-api.md §7）。
    """
    cursor = conn.execute(
        """
        INSERT INTO versions (
          version, files, tokens, indexed_at, obfuscated, has_summaries, source_size_bytes, db_path
        ) VALUES (?, ?, ?, ?, 0, 0, 0, NULL)
        ON CONFLICT (version) DO NOTHING
        """,
        (version, files, tokens, utc_now()),
    )

    if cursor.rowcount == 0:
        return None

    inserted = find_version(conn, version)

    assert inserted is not None, "刚写入的版本必须能查回来"

    return inserted


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
