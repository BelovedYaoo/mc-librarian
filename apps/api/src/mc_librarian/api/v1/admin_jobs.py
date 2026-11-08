"""`/v1/admin/jobs*`：索引任务的创建、查询、日志、取消与重试（契约见 docs/openapi.yaml）。

接口只写控制面并立即返回；真正的执行由 `app.state.job_runner` 里的 `JobRunner`
串行消费（并发 1）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from mc_librarian import audit
from mc_librarian.auth.deps import CurrentAdmin, DbConn, SettingsDep, client_ip
from mc_librarian.config import Settings
from mc_librarian.db import transaction
from mc_librarian.jobs import store
from mc_librarian.problems import (
    TITLE_BAD_REQUEST,
    TITLE_NOT_FOUND,
    ProblemException,
    problem_response,
)
from mc_librarian.schemas import Job, JobCreate, JobList, JobLogList, JobStatus

router = APIRouter(prefix="/admin", tags=["admin"])

CONFLICT_TITLE = "冲突"
FORBIDDEN_TITLE = "无权访问"

# `options` 可省略：省略即从官方 jar 跑完整五段；给了 `options` 但既无 `source_dir` 也无
# `sources` 属于笔误（issue #24 把单目录扩成多来源，两者必居其一）。
MISSING_SOURCE_DETAIL = (
    "options 必须给 source_dir 或 sources：要么省略 options（从官方 jar 跑 download→decompile→"
    "parse→summarize→finalize 五段），要么给一个非空的 source_dir / 至少一项 sources"
)

# `source_dir` 与 `sources` 是同一个东西的单/多来源写法，同时给就是语义不清。
AMBIGUOUS_SOURCE_DETAIL = (
    "options.source_dir 与 options.sources 不能同时给："
    "sources 是 source_dir 的多来源形式（source_dir 等价于 [{kind: \"patched\", dir: source_dir}]）"
)

# 同一类来源只能给一个目录（两个 patched 目录该合并成一个，而不是并列）。
DUPLICATE_SOURCE_KIND_DETAIL = (
    "options.sources 的 kind 不能重复：patched / loader / patch 各只能出现一次"
)

# 契约里 `options.obfuscated` 是 `type: boolean`（非 nullable）：显式 null 是笔误，
# 省略字段才是「不声明」（由 `jobs.runner.resolve_obfuscated` 推断）。
NULL_OBFUSCATED_DETAIL = (
    "options.obfuscated 不能显式传 null：省略该字段表示不声明（由服务端按 checkpoint 推断），"
    "要显式声明请传 true 或 false"
)

# `MC_LIBRARIAN_INDEX_ENABLED=false` 时本实例不构建索引（节点内存不够跑反编译）。
INDEX_DISABLED_DETAIL = (
    "本实例不接收索引任务（MC_LIBRARIAN_INDEX_JOBS_ENABLED=false），"
    "请在本地工作站生成索引后用 POST /v1/admin/versions 注册"
)


def _require_disk_headroom(settings: Settings) -> None:
    """索引磁盘配额：数据根已用超过配额时拒绝新任务（0 = 不限制）。"""
    quota_gb = settings.index_disk_quota_gb

    if quota_gb <= 0:
        return

    limit = quota_gb * 1024**3
    used = 0

    # 累加到超限就停：配额只关心「有没有超」，不需要精确的总量（几万文件全量统计很慢）。
    for path in settings.data_dir.rglob("*"):
        if not path.is_file():
            continue

        used += path.stat().st_size

        if used > limit:
            raise ProblemException(
                409,
                CONFLICT_TITLE,
                f"索引磁盘已用超过配额（{quota_gb} GB）：请先删除旧版本，或在设置页调大配额",
            )


def _require_job(conn: DbConn, job_id: str) -> Job:
    job = store.find(conn, job_id)

    if job is None:
        raise ProblemException(404, TITLE_NOT_FOUND, f"任务不存在：{job_id}")

    return job


@router.post(
    "/jobs",
    status_code=202,
    summary="创建索引任务",
    description=(
        "立即返回 202，实际执行由 JobRunner 串行消费（并发 1）。"
        "阶段序列由 options.source_dir / options.sources 决定：给了源目录走 "
        "parse → summarize → finalize；省略 options 则从官方 jar 起跑完整五段 "
        "download → decompile → parse → summarize → finalize；"
        "给了 options 但既无 source_dir 也无 sources 返回 422；两者同时给返回 422。"
    ),
    operation_id="createJob",
    responses={
        403: problem_response("需要管理员权限，或本实例已关闭索引构建"),
        409: problem_response("已有同类任务在运行"),
        422: problem_response("请求不合法"),
    },
)
def create_job(
    payload: JobCreate,
    request: Request,
    conn: DbConn,
    admin: CurrentAdmin,
    settings: SettingsDep,
) -> Job:
    # 实例能力闸门：先于入参校验，关闭时任何索引任务都建不出来（与 payload 无关）。
    if not settings.index_jobs_enabled:
        raise ProblemException(403, FORBIDDEN_TITLE, INDEX_DISABLED_DETAIL)

    _require_disk_headroom(settings)

    options = payload.options

    with transaction(conn):
        # 校验与入队同一事务。`options` 可省略（走五段）；给了但既无 `source_dir` 也无
        # `sources` 则 422；两者同时给 / kinds 重复也是 422（语义不清）。
        if options is not None:
            if options.source_dir and options.sources:
                raise ProblemException(422, TITLE_BAD_REQUEST, AMBIGUOUS_SOURCE_DETAIL)

            if options.sources:
                kinds = [source.kind for source in options.sources]

                if len(set(kinds)) != len(kinds):
                    raise ProblemException(422, TITLE_BAD_REQUEST, DUPLICATE_SOURCE_KIND_DETAIL)

            if not options.source_dir and not options.sources:
                raise ProblemException(422, TITLE_BAD_REQUEST, MISSING_SOURCE_DETAIL)

        # 显式 `obfuscated: null` 与「省略该字段」不同：前者是笔误（契约里非 nullable），回 422。
        if (
            options is not None
            and "obfuscated" in options.model_fields_set
            and options.obfuscated is None
        ):
            raise ProblemException(422, TITLE_BAD_REQUEST, NULL_OBFUSCATED_DETAIL)

        force = options.force if options is not None else False

        # 判定与入队同一事务：同版本并发提交只有一个能成功。
        if not force and store.has_active_job(conn, payload.version):
            raise ProblemException(
                409, CONFLICT_TITLE, f"版本 {payload.version} 已有任务在排队或运行"
            )

        job = store.create(
            conn,
            kind=payload.kind,
            version=payload.version,
            options={} if options is None else options.model_dump(),
            created_by=admin.id,
        )
        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_JOB_CREATED,
            target=job.id,
            detail={"kind": job.kind, "version": job.version},
            ip=client_ip(request),
        )

        return job


@router.get(
    "/jobs",
    summary="任务列表",
    operation_id="listJobs",
    responses={403: problem_response("需要管理员权限")},
)
def list_jobs(
    conn: DbConn,
    _admin: CurrentAdmin,
    status: Annotated[JobStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JobList:
    return JobList(jobs=store.list_jobs(conn, status=status, limit=limit))


@router.get(
    "/jobs/{jobId}",
    summary="任务详情",
    operation_id="getJob",
    responses={
        403: problem_response("需要管理员权限"),
        404: problem_response("任务不存在"),
    },
)
def get_job(jobId: str, conn: DbConn, _admin: CurrentAdmin) -> Job:
    return _require_job(conn, jobId)


@router.get(
    "/jobs/{jobId}/logs",
    summary="任务日志",
    description="按 `?after=<id>` 增量拉取：只返回 id 大于 `after` 的日志。",
    operation_id="getJobLogs",
    responses={
        403: problem_response("需要管理员权限"),
        404: problem_response("任务不存在"),
    },
)
def get_job_logs(
    jobId: str,
    conn: DbConn,
    _admin: CurrentAdmin,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> JobLogList:
    _require_job(conn, jobId)

    return JobLogList(lines=store.list_logs(conn, jobId, after=after, limit=limit))


@router.post(
    "/jobs/{jobId}/cancel",
    status_code=202,
    summary="取消任务",
    description="只对 queued/running 有效；running 任务在当前阶段边界停止。",
    operation_id="cancelJob",
    responses={
        403: problem_response("需要管理员权限"),
        404: problem_response("任务不存在"),
        409: problem_response("任务已结束"),
    },
)
def cancel_job(
    jobId: str, request: Request, conn: DbConn, admin: CurrentAdmin
) -> Job:
    with transaction(conn):
        # 判定与写入同一 `BEGIN IMMEDIATE` 事务，UPDATE 带状态守卫：并发两次 cancel 只有一次生效。
        job = _require_job(conn, jobId)
        canceled = store.request_cancel(conn, jobId)

        if canceled is None:
            raise ProblemException(409, CONFLICT_TITLE, f"任务已结束（{job.status}），无法取消")

        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_JOB_CANCELED,
            target=jobId,
            detail={"from_status": job.status},
            ip=client_ip(request),
        )

        return canceled


@router.post(
    "/jobs/{jobId}/retry",
    status_code=202,
    summary="重试任务",
    description=(
        "只对 failed/canceled 有效：保留 checkpoint 把同一任务重新置为 queued，"
        "从下一个未完成的阶段续跑。"
    ),
    operation_id="retryJob",
    responses={
        403: problem_response("需要管理员权限，或本实例已关闭索引构建"),
        404: problem_response("任务不存在"),
        409: problem_response("任务不在可重试的终态"),
    },
)
def retry_job(
    jobId: str, request: Request, conn: DbConn, admin: CurrentAdmin, settings: SettingsDep
) -> Job:
    with transaction(conn):
        # 先分辨任务是否存在（不存在回 404），再判闸门：否则闸门关闭时对不存在的 id 也会回 403。
        job = _require_job(conn, jobId)

        # 闸门关闭时重排会让 runner 跑反编译，与「本实例不构建索引」的承诺不符，同样回 403。
        if not settings.index_jobs_enabled:
            raise ProblemException(403, FORBIDDEN_TITLE, INDEX_DISABLED_DETAIL)

        # 判定与写入同一 `BEGIN IMMEDIATE` 事务，UPDATE 带状态守卫（只认 failed/canceled）。
        requeued = store.requeue(conn, jobId)

        if requeued is None:
            raise ProblemException(
                409,
                CONFLICT_TITLE,
                f"任务状态为 {job.status}，只有 failed/canceled 可以重试",
            )

        audit.record(
            conn,
            actor_id=admin.id,
            action=audit.ACTION_JOB_RETRIED,
            target=jobId,
            detail={"from_status": job.status},
            ip=client_ip(request),
        )

        return requeued
