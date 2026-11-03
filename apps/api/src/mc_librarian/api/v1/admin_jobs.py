"""`/v1/admin/jobs*`：索引任务的创建、查询、日志、取消与重试（契约见 docs/openapi.yaml）。

接口只写控制面并立即返回；真正的执行由 `app.state.job_runner` 里的 `JobRunner`
串行消费（并发 1）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from mc_librarian import audit
from mc_librarian.auth.deps import CurrentAdmin, DbConn, client_ip
from mc_librarian.db import transaction
from mc_librarian.jobs import store
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import Job, JobCreate, JobList, JobLogList, JobStatus

router = APIRouter(prefix="/admin", tags=["admin"])

NOT_FOUND_TITLE = "资源不存在"
CONFLICT_TITLE = "冲突"
BAD_REQUEST_TITLE = "请求不合法"

# `options` 可省略：省略即从官方 jar 跑完整五段；给了 `options` 但 `source_dir` 为空属于笔误。
MISSING_SOURCE_DIR_DETAIL = (
    "options.source_dir 不能为空：要么省略 options（从官方 jar 跑 download→decompile→parse→"
    "summarize→finalize 五段），要么给一个非空的源码目录"
)


def _require_job(conn: DbConn, job_id: str) -> Job:
    job = store.find(conn, job_id)

    if job is None:
        raise ProblemException(404, NOT_FOUND_TITLE, f"任务不存在：{job_id}")

    return job


@router.post(
    "/jobs",
    status_code=202,
    summary="创建索引任务",
    description=(
        "立即返回 202，实际执行由 JobRunner 串行消费（并发 1）。"
        "阶段序列由 options.source_dir 决定：给了源目录走 parse → summarize → finalize；"
        "省略 options 则从官方 jar 起跑完整五段 "
        "download → decompile → parse → summarize → finalize；"
        "给了 options 但 source_dir 为空返回 422。"
    ),
    operation_id="createJob",
    responses={
        403: problem_response("需要管理员权限"),
        409: problem_response("已有同类任务在运行"),
        422: problem_response("请求不合法"),
    },
)
def create_job(payload: JobCreate, request: Request, conn: DbConn, admin: CurrentAdmin) -> Job:
    options = payload.options

    with transaction(conn):
        # 校验与入队同一事务。`options` 可省略（走五段）；给了但 `source_dir` 为空则 422。
        if options is not None and not options.source_dir:
            raise ProblemException(422, BAD_REQUEST_TITLE, MISSING_SOURCE_DIR_DETAIL)

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
        403: problem_response("需要管理员权限"),
        404: problem_response("任务不存在"),
        409: problem_response("任务不在可重试的终态"),
    },
)
def retry_job(
    jobId: str, request: Request, conn: DbConn, admin: CurrentAdmin
) -> Job:
    with transaction(conn):
        # 判定与写入同一 `BEGIN IMMEDIATE` 事务，UPDATE 带状态守卫（只认 failed/canceled）。
        job = _require_job(conn, jobId)
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
