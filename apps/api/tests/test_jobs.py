"""索引任务框架：迁移、`JobRunner` 生命周期、取消、日志、版本注册与权限。

任务全部落在 `tmp_path` 下的临时数据根；源码目录也是临时 fixture，不碰仓库里的真实数据。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.db import LATEST_VERSION, Database
from mc_librarian.jobs import runner as runner_module

TERMINAL = {"succeeded", "failed", "canceled"}
SOURCE_JAVA = "package net.minecraft;\n\npublic class MobCategory {}\n"

WAIT_TIMEOUT_S = 10.0


# ----------------------------------------------------------------------
# helpers / fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """一个最小源码根：1 个 `.java` + `data/` 下的 1 个文件（D11 的索引范围）。"""
    root = tmp_path / "src" / "26.3"
    java = root / "net" / "minecraft"
    java.mkdir(parents=True)
    (java / "MobCategory.java").write_text(SOURCE_JAVA, encoding="utf-8")

    data = root / "data" / "minecraft" / "tags"
    data.mkdir(parents=True)
    (data / "mob_category.json").write_text('{"values": []}', encoding="utf-8")

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


# ----------------------------------------------------------------------
# 迁移
# ----------------------------------------------------------------------


def test_migration_creates_job_tables(tmp_path: Path) -> None:
    database = Database.for_data_dir(tmp_path / "data")

    assert database.initialize() == LATEST_VERSION == 4

    with database.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {"jobs", "job_logs", "versions"} <= tables


# ----------------------------------------------------------------------
# 创建 / 生命周期
# ----------------------------------------------------------------------


def test_create_job_requires_source_dir(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/v1/admin/jobs",
        json={"kind": "index", "version": "26.3", "options": {}},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "source_dir" in response.json()["detail"]


def test_index_job_lifecycle(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """queued → running → succeeded，完成后版本出现在列表里并带上统计。"""
    started, release = blocking_stage(monkeypatch, "download")

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
    assert running["stage"] == "download"

    release.set()
    finished = wait_for_terminal(admin_client, job_id)

    assert finished["status"] == "succeeded"
    assert finished["progress"] == 1
    assert finished["stage"] == "finalize"
    assert finished["finished_at"] is not None

    versions = admin_client.get("/v1/versions").json()["versions"]
    registered = next(item for item in versions if item["version"] == "26.3")

    assert registered["files"] == 2
    assert registered["tokens"] > 0
    assert registered["has_summaries"] is True

    logs = admin_client.get(f"/v1/admin/jobs/{job_id}/logs").json()["lines"]
    assert any("parse" in line["message"] for line in logs)


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
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败重排后从 checkpoint 续跑：已完成的 download/decompile 不再执行。"""
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

    assert wait_for_terminal(admin_client, job_id)["status"] == "failed"
    assert calls == ["download", "decompile", "parse"]

    # 没有 retry 端点（#4 只做框架）：直接改库把任务放回队列。
    conn.execute("UPDATE jobs SET status = 'queued', cancel_requested = 0 WHERE id = ?", (job_id,))
    conn.commit()
    calls.clear()

    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"
    assert calls == ["parse", "summarize", "finalize"]


def test_same_version_conflicts_unless_forced(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, release = blocking_stage(monkeypatch, "download")

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
    started, release = blocking_stage(monkeypatch, "decompile")

    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert started.wait(timeout=WAIT_TIMEOUT_S)

    cancel = admin_client.post(f"/v1/admin/jobs/{job_id}/cancel")

    assert cancel.status_code == 202
    assert cancel.json()["status"] == "running"

    release.set()
    job = wait_for_terminal(admin_client, job_id)

    assert job["status"] == "canceled"
    # 停在当前阶段边界：decompile 已完成，parse 没有开始。
    assert job["stage"] == "decompile"

    logs = admin_client.get(f"/v1/admin/jobs/{job_id}/logs").json()["lines"]
    assert any(line["level"] == "warn" for line in logs)


def test_cancel_queued_job(
    admin_client: TestClient, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发 1：第一个任务占着 runner 时，第二个排队，取消立即生效。"""
    _, release = blocking_stage(monkeypatch, "download")

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


def test_job_list_filters_by_status(admin_client: TestClient, source_dir: Path) -> None:
    job_id = submit(admin_client, "26.3", source_dir).json()["id"]
    assert wait_for_terminal(admin_client, job_id)["status"] == "succeeded"

    succeeded = admin_client.get("/v1/admin/jobs", params={"status": "succeeded"}).json()["jobs"]
    assert [job["id"] for job in succeeded] == [job_id]

    queued = admin_client.get("/v1/admin/jobs", params={"status": "queued"}).json()["jobs"]
    assert queued == []


# ----------------------------------------------------------------------
# 版本注册
# ----------------------------------------------------------------------


def write_index(path: Path, *, tokens: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, tokens INTEGER)")
    conn.execute("INSERT INTO files (path, tokens) VALUES ('net/minecraft/A.java', ?)", (tokens,))
    conn.commit()
    conn.close()


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
