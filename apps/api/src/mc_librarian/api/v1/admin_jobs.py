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
def create_job(payload: JobCreate, request: Request, conn: DbConn, admin: CurrentAdmin) -> Job:
    options = payload.options or JobOptions()

    # runner 的 parse 阶段无条件要求源目录（summarize 的真实流水线见 #13），所以现在任何
    # kind 缺少 `source_dir` 都会在 parse 阶段必然失败；在入队前就按 kind + source_dir 拒绝。
    if not options.source_dir:
        raise ProblemException(
            422,
            BAD_REQUEST_TITLE,
            f"{payload.kind} 任务必须提供 options.source_dir"
            "（当前所有任务都会执行 parse 阶段扫描源码目录）",
        )

    with transaction(conn):
        # 判定与入队同一事务：同版本并发提交只有一个能成功。
        if not options.force and store.has_active_job(conn, payload.version):
            raise ProblemException(
                409, CONFLICT_TITLE, f"版本 {payload.version} 已有任务在排队或运行"
            )

        job = store.create(
            conn,
            kind=payload.kind,
            version=payload.version,
            options=options.model_dump(),
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
def cancel_job(jobId: str, conn: DbConn, _admin: CurrentAdmin) -> Job:
    job = _require_job(conn, jobId)

    if job.status not in store.ACTIVE_STATUSES:
        raise ProblemException(409, CONFLICT_TITLE, f"任务已结束（{job.status}），无法取消")

    with transaction(conn):
        return store.request_cancel(conn, jobId)


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
def retry_job(jobId: str, conn: DbConn, _admin: CurrentAdmin) -> Job:
    job = _require_job(conn, jobId)

    if job.status not in store.RETRYABLE_STATUSES:
        raise ProblemException(
            409, CONFLICT_TITLE, f"任务状态为 {job.status}，只有 failed/canceled 可以重试"
        )

    with transaction(conn):
        return store.requeue(conn, jobId)
