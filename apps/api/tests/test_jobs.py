"""索引任务框架：迁移、`JobRunner` 生命周期、取消、日志、版本注册与权限。

任务全部落在 `tmp_path` 下的临时数据根；源码目录也是临时 fixture，不碰仓库里的真实数据。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.db import LATEST_VERSION, Database, transaction
from mc_librarian.jobs import runner as runner_module
from mc_librarian.jobs import store
from mc_librarian.jobs.runner import JobRunner

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"

TERMINAL = {"succeeded", "failed", "canceled"}

WAIT_TIMEOUT_S = 10.0


# ----------------------------------------------------------------------
# helpers / fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """仓库 fixture 源码树：5 个 `.java` + `data/` 下的 1 个 `.json`（issue #5 扩充）。"""
    root = tmp_path / "src" / "26.3"
    shutil.copytree(FIXTURE_SOURCE, root)

    return root


def submit(
    client: TestClient,
    version: str,
    source_dir: Path | str,
    *,
    kind: str = "index",
    force: bool = False,
) -> Any:
    return client.post(
        "/v1/admin/jobs",
        json={
            "kind": kind,
            "version": version,
            "options": {"source_dir": str(source_dir), "force": force},
        },
    )


def wait_for_terminal(
    client: TestClient, job_id: str, timeout: float = WAIT_TIMEOUT_S
) -> dict[str, Any]:
    """轮询任务直到进入终态；超时即失败（附最后一次状态，便于定位卡在哪一阶段）。"""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        last = client.get(f"/v1/admin/jobs/{job_id}").json()

        if last["status"] in TERMINAL:
            return last

        time.sleep(0.05)

    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内结束：{last}")


def blocking_stage(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> tuple[threading.Event, threading.Event]:
    """让指定阶段阻塞，直到测试放行：用来稳定观察 running 与 queued 状态。"""
    started = threading.Event()
    release = threading.Event()
    real = runner_module.run_stage

    def wrapped(name: str, context: runner_module.JobContext) -> dict[str, Any]:
        if name == stage:
            started.set()
            release.wait(timeout=WAIT_TIMEOUT_S)
        return real(name, context)

    monkeypatch.setattr(runner_module, "run_stage", wrapped)

    return started, release


def recording_stage(
    monkeypatch: pytest.MonkeyPatch, *, block: str | None = None
) -> tuple[list[str], threading.Event, threading.Event]:
    """记录实际执行的阶段名；`block` 指定时该阶段阻塞，直到测试放行。"""
    calls: list[str] = []
    started = threading.Event()
    release = threading.Event()
    real = runner_module.run_stage

    def wrapped(name: str, context: runner_module.JobContext) -> dict[str, Any]:
        calls.append(name)

        if name == block:
            started.set()
            release.wait(timeout=WAIT_TIMEOUT_S)

        return real(name, context)

    monkeypatch.setattr(runner_module, "run_stage", wrapped)

    return calls, started, release


# ----------------------------------------------------------------------
# 迁移
# ----------------------------------------------------------------------


def hold_cancel_check(
    monkeypatch: pytest.MonkeyPatch, job_id: str
) -> tuple[threading.Event, threading.Event]:
    """把指定任务的取消检查卡住：runner 已认领，但还没进入本次执行的任何阶段。

    返回 (reached, release)。`reached` 置位后测试再发 cancel 请求，就能稳定复现
    「重排后立即取消」这个原本只持续几毫秒的窗口。
    """
    reached = threading.Event()
    release = threading.Event()
    real = JobRunner._is_cancel_requested

    def wrapped(self: JobRunner, current: str) -> bool:
        if current == job_id:
            reached.set()
            release.wait(timeout=WAIT_TIMEOUT_S)

        return real(self, current)

    monkeypatch.setattr(JobRunner, "_is_cancel_requested", wrapped)

    return reached, release


# ----------------------------------------------------------------------
# 迁移
# ----------------------------------------------------------------------


def insert_job_row(
    conn: sqlite3.Connection, *, job_id: str, kind: str, stage: str | None
) -> None:
    """绕开接口层直接往 `jobs` 插一行，专门用来验证存储层的 CHECK。"""
    conn.execute(
        "INSERT INTO jobs (id, kind, version, status, stage, created_at)"
        " VALUES (?, ?, '26.3', 'queued', ?, '2026-11-03T00:00:00Z')",
        (job_id, kind, stage),
    )


def test_migration_creates_job_tables(tmp_path: Path) -> None:
    database = Database.for_data_dir(tmp_path / "data")

    assert database.initialize() == LATEST_VERSION == 4

    with database.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {"jobs", "job_logs", "versions"} <= tables


def test_jobs_check_rejects_retired_kind_and_stage(tmp_path: Path) -> None:
    """`jobs` 的 CHECK 与接口层同域：`reindex` 与 `download` 这类值直接插不进去。"""
    database = Database.for_data_dir(tmp_path / "data")
    database.initialize()

    with database.connect() as conn:
        # 合法取值照常可写：`kind='index'`，`stage` 为 NULL（尚未进入阶段）或三段之一。
        for index, stage in enumerate((None, "parse", "summarize", "finalize")):
            insert_job_row(conn, job_id=f"ok-{index}", kind="index", stage=stage)

        with pytest.raises(sqlite3.IntegrityError):
            insert_job_row(conn, job_id="bad-kind", kind="reindex", stage=None)

        with pytest.raises(sqlite3.IntegrityError):
            insert_job_row(conn, job_id="bad-stage", kind="index", stage="download")

        count = conn.execute("SELECT count(*) AS count FROM jobs").fetchone()["count"]

    # 被拒绝的两行没有落库。
    assert count == 4


# ----------------------------------------------------------------------
# 创建 / 生命周期
# ----------------------------------------------------------------------


def test_create_job_rejects_retired_kinds(admin_client: TestClient) -> None:
    """`kind` 只保留 `index`：`reindex` / `summarize` 待 #7/#13 引入，现在直接 422。"""
    for rejected in ("reindex", "summarize"):
        response = admin_client.post(
            "/v1/admin/jobs",
            json={
                "kind": rejected,
                "version": "26.3",
                "options": {"source_dir": "/tmp/whatever"},
            },
        )

        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")


def test_create_job_requires_source_dir(admin_client: TestClient) -> None:
    """`#4` 只支持带源目录的任务：缺 `options.source_dir` 一律 422，不承诺五段路径。"""
    missing = admin_client.post("/v1/admin/jobs", json={"kind": "index", "version": "26.3"})

    assert missing.status_code == 422
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert "#13" in missing.json()["detail"]

    empty = admin_client.post(
        "/v1/admin/jobs",
        json={"kind": "index", "version": "26.3", "options": {}},
    )

    assert empty.status_code == 422
    assert empty.headers["content-type"].startswith("application/problem+json")


def test_source_dir_job_starts_at_parse(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """给了 `options.source_dir`：阶段序列 `parse → summarize → finalize`，从 parse 开始。"""
    calls, started, release = recording_stage(monkeypatch, block="parse")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]

    assert started.wait(timeout=WAIT_TIMEOUT_S), "runner 应从 parse 阶段开始"
    assert admin_client.get(f"/v1/admin/jobs/{job_id}").json()["stage"] == "parse"

    release.set()
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"
    assert calls == ["parse", "summarize", "finalize"]


def test_index_job_lifecycle(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """queued → running → succeeded，完成后版本出现在列表里并带上统计。"""
    started, release = blocking_stage(monkeypatch, "parse")

    created = submit(admin_client, "26.3", source_dir)

    assert created.status_code == 202, created.text
    body = created.json()
    assert body["status"] == "queued"
    assert body["kind"] == "index"
    assert body["progress"] == 0

    job_id = body["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S), "runner 应该已经认领任务"

    running = admin_client.get(f"/v1/admin/jobs/{job_id}").json()
    assert running["status"] == "running"
    assert running["stage"] == "parse"

    release.set()
    finished = wait_for_terminal(admin_client, job_id)

    assert finished["status"] == "succeeded"
    assert finished["progress"] == 1
    assert finished["stage"] == "finalize"
    assert finished["finished_at"] is not None

    versions = admin_client.get("/v1/versions").json()["versions"]
    registered = next(item for item in versions if item["version"] == "26.3")

    # 5 个 .java + 1 个 data JSON 都进 files 表（`data/` 只进 files，不解析符号）。
    assert registered["files"] == 6
    assert registered["tokens"] > 0
    # #4 的 parse 阶段只统计文件数与 token：不 remap、不生成摘要（#7/#13 接管后才可能为 True）。
    assert registered["obfuscated"] is False
    assert registered["has_summaries"] is False

    logs = admin_client.get(f"/v1/admin/jobs/{job_id}/logs").json()["lines"]
    assert any("parse" in line["message"] for line in logs)


def test_parse_stage_places_source_and_writes_index(
    admin_client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """`parse` 把源码树就位到 `<data_dir>/src/<version>/` 并写出索引库（issue #5 / 规格 §14）。"""
    outside = tmp_path / "elsewhere"
    shutil.copytree(FIXTURE_SOURCE, outside)

    job_id = submit(admin_client, "26.3", outside).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    placed = settings.data_dir / "src" / "26.3"
    assert (placed / "net" / "minecraft" / "world" / "Mob.java").is_file()
    assert (settings.data_dir / "db" / "26.3.sqlite").is_file()

    logs = admin_client.get(f"/v1/admin/jobs/{job_id}/logs").json()["lines"]
    assert any("就位" in line["message"] for line in logs)


def test_job_failure_records_error_summary(admin_client: TestClient, tmp_path: Path) -> None:
    response = submit(admin_client, "26.3", tmp_path / "does-not-exist")

    assert response.status_code == 202
    job = wait_for_terminal(admin_client, response.json()["id"])

    assert job["status"] == "failed"
    assert "source_dir" in job["error"]

    logs = admin_client.get(f"/v1/admin/jobs/{job['id']}/logs").json()["lines"]
    assert any(line["level"] == "error" for line in logs)


def test_retry_resumes_from_failed_stage(
    admin_client: TestClient,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败后经 retry 端点重排，从 checkpoint 续跑：已完成的阶段不再执行。"""
    calls: list[str] = []
    real = runner_module.run_stage
    state = {"failed_once": False}

    def flaky(stage: str, context: runner_module.JobContext) -> dict[str, Any]:
        calls.append(stage)

        if stage == "parse" and not state["failed_once"]:
            state["failed_once"] = True
            raise runner_module.StageError("parse 首次失败")

        return real(stage, context)

    monkeypatch.setattr(runner_module, "run_stage", flaky)

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]

    failed = wait_for_terminal(admin_client, job_id)
    assert failed["status"] == "failed"
    assert calls == ["parse"]

    retry = admin_client.post(f"/v1/admin/jobs/{job_id}/retry")

    assert retry.status_code == 202, retry.text
    assert retry.json()["status"] == "queued"
    assert retry.json()["error"] is None
    calls.clear()

    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"
    assert calls == ["parse", "summarize", "finalize"]


def test_retry_of_canceled_job_resumes_from_checkpoint(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """canceled 也能重试：取消时保留的 checkpoint 决定从哪里接着跑。"""
    started, release = blocking_stage(monkeypatch, "parse")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/cancel").status_code == 202

    release.set()
    assert wait_for_terminal(admin_client, job_id)["status"] == "canceled"

    retry = admin_client.post(f"/v1/admin/jobs/{job_id}/retry")

    assert retry.status_code == 202, retry.text
    assert retry.json()["status"] == "queued"
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"


def test_retry_rejects_non_terminal_states(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有 failed/canceled 能重试：succeeded 与 running 都回 409。"""
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    conflict = admin_client.post(f"/v1/admin/jobs/{job_id}/retry")

    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")

    started, release = blocking_stage(monkeypatch, "parse")
    running_id = submit(admin_client, "1.20.1", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    assert admin_client.post(f"/v1/admin/jobs/{running_id}/retry").status_code == 409

    release.set()
    assert wait_for_terminal(admin_client, running_id)["status"] == "succeeded"


def test_same_version_conflicts_unless_forced(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, release = blocking_stage(monkeypatch, "parse")

    first = submit(admin_client, "26.3", source_dir)
    assert first.status_code == 202

    duplicate = submit(admin_client, "26.3", source_dir)
    assert duplicate.status_code == 409
    assert duplicate.headers["content-type"].startswith("application/problem+json")

    forced = submit(admin_client, "26.3", source_dir, force=True)
    assert forced.status_code == 202

    release.set()
    wait_for_terminal(admin_client, first.json()["id"])


# ----------------------------------------------------------------------
# 取消
# ----------------------------------------------------------------------


def test_cancel_running_job_stops_at_stage_boundary(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = blocking_stage(monkeypatch, "parse")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    cancel = admin_client.post(f"/v1/admin/jobs/{job_id}/cancel")

    assert cancel.status_code == 202
    assert cancel.json()["status"] == "running"

    release.set()
    job = wait_for_terminal(admin_client, job_id)

    assert job["status"] == "canceled"
    # 停在当前阶段边界：parse 已完成，summarize 没有开始；`message` 与 `stage` 一致。
    assert job["stage"] == "parse"
    assert "parse" in (job["message"] or "")

    logs = admin_client.get(f"/v1/admin/jobs/{job_id}/logs").json()["lines"]
    assert any(line["level"] == "warn" for line in logs)


def test_cancel_right_after_retry_keeps_stage_consistent(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重排清空 `stage` 后立即取消：`stage` 列要补回当前阶段，与 `message` 对得上。"""
    started, release = blocking_stage(monkeypatch, "parse")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/cancel").status_code == 202
    release.set()
    assert wait_for_terminal(admin_client, job_id)["status"] == "canceled"

    # 重排把 `stage` 清成 NULL，checkpoint 里仍留着已完成的 parse。
    reached, gate_release = hold_cancel_check(monkeypatch, job_id)

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/retry").status_code == 202
    assert reached.wait(timeout=WAIT_TIMEOUT_S), "runner 应该已认领重排后的任务"

    claimed = admin_client.get(f"/v1/admin/jobs/{job_id}").json()
    assert claimed["status"] == "running"
    assert claimed["stage"] is None

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/cancel").status_code == 202
    gate_release.set()

    job = wait_for_terminal(admin_client, job_id)

    assert job["status"] == "canceled"
    assert job["stage"] == "parse"
    assert "parse" in (job["message"] or "")


def test_cancel_during_finalize_is_not_succeeded(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize 执行期间到达的取消不能漏掉：终态与取消标志在同一条 UPDATE 里判定。"""
    started, release = blocking_stage(monkeypatch, "finalize")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/cancel").status_code == 202

    release.set()
    job = wait_for_terminal(admin_client, job_id)

    assert job["status"] == "canceled"
    assert job["stage"] == "finalize"
    # 没写 succeeded，版本不应被登记为已索引。
    versions = admin_client.get("/v1/versions").json()["versions"]
    assert all(item["version"] != "26.3" for item in versions)


def test_cancel_queued_job(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发 1：第一个任务占着 runner 时，第二个排队，取消立即生效。"""
    _, release = blocking_stage(monkeypatch, "parse")

    first = submit(admin_client, "26.3", source_dir)
    assert first.status_code == 202

    second = submit(admin_client, "1.20.1", source_dir)
    assert second.status_code == 202
    assert second.json()["status"] == "queued"

    cancel = admin_client.post(f"/v1/admin/jobs/{second.json()['id']}/cancel")

    assert cancel.status_code == 202
    assert cancel.json()["status"] == "canceled"
    assert cancel.json()["finished_at"] is not None

    release.set()
    assert wait_for_terminal(admin_client, first.json()["id"])["status"] == "succeeded"


def test_cancel_finished_job_is_conflict(admin_client: TestClient, source_dir: Path) -> None:
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    cancel = admin_client.post(f"/v1/admin/jobs/{job_id}/cancel")

    assert cancel.status_code == 409


def test_cancel_twice_second_is_conflict(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判定与 UPDATE 在同一事务：第二次 cancel 命中状态守卫，回 409。"""
    _, release = blocking_stage(monkeypatch, "parse")

    running_id = submit(admin_client, "26.3", source_dir).json()["id"]
    queued_id = submit(admin_client, "1.20.1", source_dir).json()["id"]

    assert admin_client.post(f"/v1/admin/jobs/{queued_id}/cancel").status_code == 202

    again = admin_client.post(f"/v1/admin/jobs/{queued_id}/cancel")

    assert again.status_code == 409
    assert again.headers["content-type"].startswith("application/problem+json")

    release.set()
    assert wait_for_terminal(admin_client, running_id)["status"] == "succeeded"


def test_cancel_after_succeeded_does_not_set_flag(
    admin_client: TestClient, source_dir: Path, conn: sqlite3.Connection
) -> None:
    """已 succeeded 的任务 cancel 必须 409，且不能写下 cancel_requested。"""
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/cancel").status_code == 409

    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["cancel_requested"] == 0


def test_concurrent_cancel_only_one_wins(settings: Settings) -> None:
    """两个线程同时 cancel：状态守卫保证只有一个生效（BEGIN IMMEDIATE + rowcount）。"""
    database = Database.for_data_dir(settings.data_dir)
    database.initialize()

    with database.connect() as conn, transaction(conn):
        job = store.create(conn, kind="index", version="26.3", options={}, created_by=None)

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait(timeout=WAIT_TIMEOUT_S)

        with database.connect() as conn, transaction(conn):
            results.append(store.request_cancel(conn, job.id) is not None)

    threads = [threading.Thread(target=worker) for _ in range(2)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=WAIT_TIMEOUT_S)

    assert sorted(results) == [False, True]


# ----------------------------------------------------------------------
# 日志与查询
# ----------------------------------------------------------------------


def test_job_logs_pagination(admin_client: TestClient, source_dir: Path) -> None:
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    everything = admin_client.get(f"/v1/admin/jobs/{job_id}/logs").json()["lines"]
    assert len(everything) >= 3

    first_page = admin_client.get(
        f"/v1/admin/jobs/{job_id}/logs", params={"limit": 1}
    ).json()["lines"]
    assert len(first_page) == 1
    assert first_page[0]["id"] == everything[0]["id"]

    rest = admin_client.get(
        f"/v1/admin/jobs/{job_id}/logs",
        params={"after": first_page[0]["id"], "limit": 1},
    ).json()["lines"]
    assert len(rest) == 1
    assert rest[0]["id"] == everything[1]["id"]

    tail = admin_client.get(
        f"/v1/admin/jobs/{job_id}/logs", params={"after": everything[-1]["id"]}
    ).json()["lines"]
    assert tail == []


def test_unknown_job_is_404(admin_client: TestClient) -> None:
    assert admin_client.get("/v1/admin/jobs/nope").status_code == 404
    assert admin_client.get("/v1/admin/jobs/nope/logs").status_code == 404
    assert admin_client.post("/v1/admin/jobs/nope/cancel").status_code == 404
    assert admin_client.post("/v1/admin/jobs/nope/retry").status_code == 404


def test_job_list_filters_by_status(admin_client: TestClient, source_dir: Path) -> None:
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    succeeded = admin_client.get("/v1/admin/jobs", params={"status": "succeeded"}).json()["jobs"]
    assert [job["id"] for job in succeeded] == [job_id]

    queued = admin_client.get("/v1/admin/jobs", params={"status": "queued"}).json()["jobs"]
    assert queued == []


# ----------------------------------------------------------------------
# 崩溃恢复 / 审计
# ----------------------------------------------------------------------


def test_runner_requeues_interrupted_running_jobs(settings: Settings, source_dir: Path) -> None:
    """进程崩溃留下的 running 行在下次启动时回到 queued，并从 checkpoint 续跑。"""
    database = Database.for_data_dir(settings.data_dir)
    database.initialize()

    with database.connect() as conn, transaction(conn):
        job = store.create(
            conn,
            kind="index",
            version="26.3",
            options={"source_dir": str(source_dir)},
            created_by=None,
        )
        # 模拟「上一次进程在 parse 完成后被杀」：status 停在 running，checkpoint 已落盘。
        # 给了 source_dir，阶段序列从 parse 开始，所以 checkpoint 里只有 parse。
        conn.execute(
            "UPDATE jobs SET status = 'running', checkpoint = ? WHERE id = ?",
            (json.dumps({"stages": ["parse"]}), job.id),
        )

    runner = JobRunner(settings, database, poll_interval=0.01)

    async def run_until_terminal() -> None:
        await runner.start()

        try:
            deadline = time.monotonic() + WAIT_TIMEOUT_S
            while time.monotonic() < deadline:
                with database.connect() as conn:
                    current = store.find(conn, job.id)

                if current is not None and current.status in TERMINAL:
                    return

                await asyncio.sleep(0.02)

            raise AssertionError("重排后的任务未在超时内跑完")
        finally:
            await runner.stop()

    asyncio.run(run_until_terminal())

    with database.connect() as conn:
        finished = store.find(conn, job.id)
        logs = store.list_logs(conn, job.id)

    assert finished is not None
    assert finished.status == "succeeded"
    assert any("中断" in line.message for line in logs)


def test_runner_cancels_interrupted_jobs_with_cancel_request(
    settings: Settings, source_dir: Path
) -> None:
    """启动恢复不丢取消意图：遗留 running 且 cancel_requested=1 的直接置 canceled，不重跑。"""
    database = Database.for_data_dir(settings.data_dir)
    database.initialize()

    with database.connect() as conn, transaction(conn):
        canceled_job = store.create(
            conn,
            kind="index",
            version="1.20.1",
            options={"source_dir": str(source_dir)},
            created_by=None,
        )
        conn.execute(
            "UPDATE jobs SET status = 'running', cancel_requested = 1 WHERE id = ?",
            (canceled_job.id,),
        )
        resumed_job = store.create(
            conn,
            kind="index",
            version="26.3",
            options={"source_dir": str(source_dir)},
            created_by=None,
        )
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (resumed_job.id,))

    runner = JobRunner(settings, database, poll_interval=0.01)

    async def run_until_terminal() -> None:
        await runner.start()

        try:
            deadline = time.monotonic() + WAIT_TIMEOUT_S
            while time.monotonic() < deadline:
                with database.connect() as conn:
                    current = store.find(conn, resumed_job.id)

                if current is not None and current.status in TERMINAL:
                    return

                await asyncio.sleep(0.02)

            raise AssertionError("重排后的任务未在超时内跑完")
        finally:
            await runner.stop()

    asyncio.run(run_until_terminal())

    with database.connect() as conn:
        canceled = store.find(conn, canceled_job.id)
        resumed = store.find(conn, resumed_job.id)

    assert canceled is not None
    assert canceled.status == "canceled"
    # 取消意图保留，不会被 claim_next（只认 cancel_requested=0）重新认领。
    assert resumed is not None
    assert resumed.status == "succeeded"


def test_fallback_failure_does_not_overwrite_succeeded(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收尾后抛异常：兜底 `_fail` 带状态守卫，不能把 succeeded 覆盖成 failed。"""
    real_finish = JobRunner._finish
    real_fail = JobRunner._fail
    fail_called = threading.Event()

    def finish_then_boom(
        self: JobRunner, job_id: str, version: str, checkpoint: dict[str, Any]
    ) -> None:
        real_finish(self, job_id, version, checkpoint)
        raise RuntimeError("模拟收尾后异常")

    def fail_spy(self: JobRunner, job_id: str, error: str) -> None:
        real_fail(self, job_id, error)
        fail_called.set()

    monkeypatch.setattr(JobRunner, "_finish", finish_then_boom)
    monkeypatch.setattr(JobRunner, "_fail", fail_spy)

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]

    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"
    assert fail_called.wait(timeout=WAIT_TIMEOUT_S), "兜底失败路径应该被触发"

    assert admin_client.get(f"/v1/admin/jobs/{job_id}").json()["status"] == "succeeded"


def test_create_job_records_audit(
    admin_client: TestClient, source_dir: Path, conn: sqlite3.Connection
) -> None:
    admin = admin_client.get("/v1/auth/me").json()
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    rows = conn.execute("SELECT actor_id, action, target, detail_json FROM audit").fetchall()
    created = next(row for row in rows if row["action"] == "job.created")

    assert created["actor_id"] == admin["id"]
    assert created["target"] == job_id
    assert json.loads(created["detail_json"]) == {"kind": "index", "version": "26.3"}


def test_successful_job_records_version_audit_with_null_actor(
    admin_client: TestClient, source_dir: Path
) -> None:
    """任务产出/更新 versions 行也要记账；系统动作没有 actor，必须返回 null。"""
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    entries = admin_client.get("/v1/admin/audit").json()["entries"]
    recorded = next(entry for entry in entries if entry["action"] == "version.recorded")

    assert recorded["target"] == "26.3"
    assert recorded["detail"]["files"] == 6
    assert recorded["actor"] is None


def test_cancel_and_retry_record_audit(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = blocking_stage(monkeypatch, "parse")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/cancel").status_code == 202
    release.set()
    assert wait_for_terminal(admin_client, job_id)["status"] == "canceled"

    assert admin_client.post(f"/v1/admin/jobs/{job_id}/retry").status_code == 202
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    actions = {
        entry["action"]: entry
        for entry in admin_client.get("/v1/admin/audit").json()["entries"]
    }

    assert actions["job.canceled"]["target"] == job_id
    assert actions["job.retried"]["target"] == job_id


# ----------------------------------------------------------------------
# 版本注册
# ----------------------------------------------------------------------


def write_index(path: Path, *, tokens: int, summaries: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, tokens INTEGER)")
    conn.execute("INSERT INTO files (path, tokens) VALUES ('net/minecraft/A.java', ?)", (tokens,))

    if summaries:
        conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY, text TEXT)")

    conn.commit()
    conn.close()


def test_job_does_not_clobber_registered_version(
    admin_client: TestClient,
    settings: Settings,
    source_dir: Path,
    conn: sqlite3.Connection,
) -> None:
    """先注册版本再跑同版本任务：已注册版本的字段不被任务产出改写。"""
    path = settings.data_dir / "db" / "26.3.sqlite"
    write_index(path, tokens=4096, summaries=True)

    registered = admin_client.post("/v1/admin/versions", json={"version": "26.3"})

    assert registered.status_code == 201, registered.text
    assert registered.json()["has_summaries"] is True

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    row = conn.execute(
        "SELECT db_path, has_summaries, files, tokens FROM versions WHERE version = '26.3'"
    ).fetchone()

    assert row["db_path"] == str(path)
    assert row["has_summaries"] == 1
    # 版本已注册：任务产出一个字段都不动（注册时 files=1、tokens=4096，不是任务算出的 6）。
    assert row["files"] == 1
    assert row["tokens"] == 4096


def test_registered_version_row_is_unchanged_after_job(
    admin_client: TestClient,
    settings: Settings,
    source_dir: Path,
    conn: sqlite3.Connection,
) -> None:
    """已注册版本跑任务后所有字段不变，也不该记 `version.recorded` 审计。"""
    path = settings.data_dir / "db" / "26.3.sqlite"
    write_index(path, tokens=4096, summaries=True)

    assert admin_client.post("/v1/admin/versions", json={"version": "26.3"}).status_code == 201

    def snapshot() -> dict[str, Any]:
        row = conn.execute(
            "SELECT version, files, tokens, indexed_at, obfuscated, has_summaries,"
            " source_size_bytes, db_path FROM versions WHERE version = '26.3'"
        ).fetchone()

        return dict(row)

    before = snapshot()
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]

    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"
    assert snapshot() == before

    actions = [
        entry["action"] for entry in admin_client.get("/v1/admin/audit").json()["entries"]
    ]
    assert "version.recorded" not in actions


def test_register_version_requires_existing_sqlite(
    admin_client: TestClient, settings: Settings
) -> None:
    missing = admin_client.post("/v1/admin/versions", json={"version": "1.20.1"})
    assert missing.status_code == 422
    assert "不存在" in missing.json()["detail"]

    write_index(settings.data_dir / "db" / "1.20.1.sqlite", tokens=4096)

    created = admin_client.post("/v1/admin/versions", json={"version": "1.20.1"})

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == "1.20.1"
    assert body["files"] == 1
    assert body["tokens"] == 4096
    assert body["obfuscated"] is True

    versions = admin_client.get("/v1/versions").json()["versions"]
    assert [item["version"] for item in versions] == ["1.20.1"]


def test_register_version_records_audit(admin_client: TestClient, settings: Settings) -> None:
    write_index(settings.data_dir / "db" / "1.20.1.sqlite", tokens=4096)

    assert admin_client.post("/v1/admin/versions", json={"version": "1.20.1"}).status_code == 201

    entry = admin_client.get("/v1/admin/audit").json()["entries"][0]

    assert entry["action"] == "version.registered"
    assert entry["target"] == "1.20.1"
    assert entry["detail"]["files"] == 1
    assert entry["detail"]["tokens"] == 4096


def test_register_version_rejects_non_sqlite(admin_client: TestClient, settings: Settings) -> None:
    path = settings.data_dir / "db" / "26.3.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a database", encoding="utf-8")

    response = admin_client.post("/v1/admin/versions", json={"version": "26.3"})

    assert response.status_code == 422


# ----------------------------------------------------------------------
# 权限
# ----------------------------------------------------------------------


def test_admin_endpoints_require_session(client: TestClient) -> None:
    assert client.get("/v1/admin/jobs").status_code == 401
    assert client.get("/v1/versions").status_code == 401

    created = client.post("/v1/admin/jobs", json={"kind": "index", "version": "26.3"})
    assert created.status_code == 401
    assert client.post("/v1/admin/versions", json={"version": "26.3"}).status_code == 401


def test_admin_endpoints_reject_non_admin(user_client: TestClient, source_dir: Path) -> None:
    assert user_client.get("/v1/admin/jobs").status_code == 403
    assert submit(user_client, "26.3", source_dir).status_code == 403
    assert user_client.post("/v1/admin/versions", json={"version": "26.3"}).status_code == 403

    # 版本列表对所有登录用户可见，写入口才需要 admin。
    assert user_client.get("/v1/versions").status_code == 200
