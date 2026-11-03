"""`/v1/admin/jobs*`：索引任务的创建、查询、日志与取消（契约见 docs/openapi.yaml）。

接口只写控制面并立即返回；真正的执行由 `app.state.job_runner` 里的 `JobRunner`
串行消费（并发 1）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from mc_librarian.auth.deps import CurrentAdmin, DbConn
from mc_librarian.db import transaction
from mc_librarian.jobs import store
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.schemas import Job, JobCreate, JobList, JobLogList, JobOptions, JobStatus

router = APIRouter(prefix="/admin", tags=["admin"])

NOT_FOUND_TITLE = "资源不存在"
CONFLICT_TITLE = "冲突"
BAD_REQUEST_TITLE = "请求不合法"


def _require_job(conn: DbConn, job_id: str) -> Job:
    job = store.find(conn, job_id)

    if job is None:
        raise ProblemException(404, NOT_FOUND_TITLE, f"任务不存在：{job_id}")

    return job


@router.post(
    "/jobs",
    status_code=202,
    summary="创建索引任务",
    description="立即返回 202，实际执行由 JobRunner 串行消费（并发 1）。",
    operation_id="createJob",
    responses={
        403: problem_response("需要管理员权限"),
        409: problem_response("已有同类任务在运行"),
        422: problem_response("请求不合法"),
    },
)
def create_job(payload: JobCreate, conn: DbConn, _admin: CurrentAdmin) -> Job:
    options = payload.options or JobOptions()

    # `index`/`reindex` 要扫源码目录，没有 source_dir 没法开工；`summarize` 不需要。
    if payload.kind in ("index", "reindex") and not options.source_dir:
        raise ProblemException(
            422, BAD_REQUEST_TITLE, "index/reindex 任务必须提供 options.source_dir"
        )

    with transaction(conn):
        # 判定与入队同一事务：同版本并发提交只有一个能成功。
        if not options.force and store.has_active_job(conn, payload.version):
            raise ProblemException(
                409, CONFLICT_TITLE, f"版本 {payload.version} 已有任务在排队或运行"
            )

        return store.create(
            conn,
            kind=payload.kind,
            version=payload.version,
            options=options.model_dump(),
            created_by=_admin.id,
        )


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
    description="支持 `Accept: text/event-stream` 增量跟随；默认返回分页文本。",
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
def cancel_job(jobId: str, conn: DbConn, _admin: CurrentAdmin) -> Job:
    job = _require_job(conn, jobId)

    if job.status not in store.ACTIVE_STATUSES:
        raise ProblemException(409, CONFLICT_TITLE, f"任务已结束（{job.status}），无法取消")

    with transaction(conn):
        return store.request_cancel(conn, jobId)
