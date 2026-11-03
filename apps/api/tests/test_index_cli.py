"""`mc-librarian index`（issue #13）：参数解析、幂等与 `--force`、退出码三态、端到端产出与登记。

端到端用 `tests/fixtures/source` 的假源码树（不联网、不真反编译）；真实 26.3 的跑通不在默认
测试里（太慢），实测数据见 issue #13。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.db import Database
from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.manifest import VersionDownloads
from mc_librarian.fetch.pipeline import cache_dir_for
from mc_librarian.fetch.tools import PipelineTools
from mc_librarian.indexing import cli as index_cli
from mc_librarian.indexing import memory
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import runner as runner_module
from mc_librarian.jobs import store
from mc_librarian.jobs.runner import (
    REUSE_INDEX_MESSAGE,
    REUSE_SOURCE_TREE_MESSAGE,
    StageError,
)
from mc_librarian.main import create_app

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """仓库 fixture 源码树（5 个 `.java` + `data/` 下 1 个 `.json`），复制到临时目录。"""
    root = tmp_path / "source"
    shutil.copytree(FIXTURE_SOURCE, root)

    return root


@pytest.fixture
def configure_cli(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 CLI 的数据根指到临时目录（`main` 内部走 `get_settings()`）。"""
    monkeypatch.setattr(index_cli, "get_settings", lambda: settings)


class UnknownVersionManifest:
    """清单里没有该版本：`download` 阶段应报「找不到版本」。"""

    def resolve(self, version: str) -> VersionDownloads:
        raise FetchError(f"找不到版本：{version}")


class ModernManifest:
    """26.x：有 client 下载项、无官方 mappings（直接反编译）。"""

    def resolve(self, version: str) -> VersionDownloads:
        return VersionDownloads(
            version=version,
            client_url="https://cdn.example/26.3/client.jar",
            client_mappings_url=None,
        )


class UnusedDownloader:
    def download(self, url: str, dest: Path) -> Path:
        raise AssertionError("清单解析失败时不该下载")


class UnusedRemapper:
    def remap(self, jar: Path, mappings: Path, dest: Path) -> Path:
        raise AssertionError("清单解析失败时不该 remap")


class UnusedDecompiler:
    def decompile(self, jar: Path, dest: Path) -> Path:
        raise AssertionError("清单解析失败时不该反编译")


def record_stages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记录 `run_stage` 实际被调用的阶段名（用于断言 parse 是否被跳过）。"""
    calls: list[str] = []
    real = runner_module.run_stage

    def recording(stage: str, context: runner_module.JobContext) -> dict[str, object]:
        calls.append(stage)

        return real(stage, context)

    monkeypatch.setattr(runner_module, "run_stage", recording)

    return calls


def job_logs(settings: Settings) -> list[str]:
    with Database.for_data_dir(settings.data_dir).connect() as conn:
        rows = conn.execute("SELECT message FROM job_logs ORDER BY id").fetchall()

    return [str(row["message"]) for row in rows]


# ----------------------------------------------------------------------
# 参数解析与退出码
# ----------------------------------------------------------------------


def test_parser_reads_version_source_dir_and_force() -> None:
    parser = index_cli.build_parser()

    args = parser.parse_args(["--version", "26.3", "--source-dir", "/tmp/src", "--force"])
    assert args.version == "26.3"
    assert args.source_dir == Path("/tmp/src")
    assert args.force is True

    default = parser.parse_args(["--version", "26.3"])
    assert default.source_dir is None
    assert default.force is False


def test_build_options_carries_force_and_reuse_index(tmp_path: Path) -> None:
    forced = index_cli.build_options(tmp_path, force=True)

    assert forced["force"] is True
    assert forced["reuse_index"] is False
    assert forced["source_dir"] == str(tmp_path)

    # 不给 `--source-dir` 时不设该键：阶段序列由 `stages_for` 按它选。
    assert index_cli.build_options(None, force=False) == {"force": False, "reuse_index": True}


def test_main_rejects_version_with_path_separator(
    configure_cli: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """版本号会进文件路径：带 `/` 的一律当参数错误挡掉，不落到 `db/` 外面。"""
    code = index_cli.main(["--version", "../26.3"])

    assert code == 2
    assert "版本号不合法" in capsys.readouterr().err


def test_main_rejects_missing_source_dir(
    configure_cli: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = index_cli.main(
        ["--version", "26.3", "--source-dir", str(tmp_path / "does-not-exist")]
    )

    assert code == 2
    assert "源码目录不存在" in capsys.readouterr().err


def test_stage_failure_returns_nonzero_and_prints_error(
    configure_cli: None,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """阶段抛 `StageError`：退出码非 0，`error` 打到 stderr（规格补充 §6）。"""

    def failing(stage: str, context: runner_module.JobContext) -> dict[str, object]:
        raise StageError("parse 阶段炸了")

    monkeypatch.setattr(runner_module, "run_stage", failing)

    code = index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)])

    assert code == 1
    assert "parse 阶段炸了" in capsys.readouterr().err


def test_interrupted_run_marks_the_job_failed(
    configure_cli: None,
    settings: Settings,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C（`KeyboardInterrupt`）中断时把任务标成 `failed`，不留下永久 `running` 的行。

    CLI 不走服务启动时的 `_recover_interrupted`：不在这里收尾的话，该 job 会永远停在
    `running`，之后同版本的任何任务都会被 `has_active_job` 挡住。
    """

    def interrupted(stage: str, context: runner_module.JobContext) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "run_stage", interrupted)

    with pytest.raises(KeyboardInterrupt):
        index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)])

    assert index_cli.INTERRUPTED_ERROR in capsys.readouterr().err

    with Database.for_data_dir(settings.data_dir).connect() as conn:
        rows = store.list_jobs(conn, limit=10)

        assert [row.status for row in rows] == ["failed"]
        assert rows[0].error == index_cli.INTERRUPTED_ERROR
        assert store.has_active_job(conn, "26.3") is False

    assert index_cli.INTERRUPTED_ERROR in job_logs(settings)


def test_missing_psutil_is_logged_into_the_job(
    configure_cli: None,
    settings: Settings,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`psutil` 缺失不只打到 stderr，还写进任务日志：否则 UI 上看不到「未测量」。"""
    monkeypatch.setattr(memory, "available", lambda: False)

    code = index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)])

    assert code == 0
    assert index_cli.PSUTIL_MISSING_MESSAGE in capsys.readouterr().err
    assert index_cli.PSUTIL_MISSING_MESSAGE in job_logs(settings)


def test_unknown_version_returns_nonzero_with_manifest_error(
    configure_cli: None,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """不给 `--source-dir` 时从清单起跑：清单里没有该版本 → 任务失败 + 可操作的错误。"""
    tools = PipelineTools(
        manifest=UnknownVersionManifest(),
        downloader=UnusedDownloader(),
        remapper=UnusedRemapper(),
        decompiler=UnusedDecompiler(),
    )
    monkeypatch.setattr(runner_module, "default_tools", lambda settings: tools)

    code = index_cli.main(["--version", "nope"])

    assert code == 1
    assert "找不到版本：nope" in capsys.readouterr().err
    assert not (settings.data_dir / "db" / "nope.sqlite").exists()


# ----------------------------------------------------------------------
# 端到端：产出索引、登记版本、服务可直接查
# ----------------------------------------------------------------------


def test_index_with_source_dir_builds_and_registers_index(
    configure_cli: None,
    settings: Settings,
    source_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`index --source-dir` 跑完即产出索引并登记版本，服务不需要再手工注册（规格补充 §5）。"""
    code = index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)])
    captured = capsys.readouterr()

    assert code == 0, captured.err

    index = index_path(settings.data_dir, "26.3")
    assert index.is_file()

    with Database.for_data_dir(settings.data_dir).connect() as conn:
        row = conn.execute(
            "SELECT db_path, files, tokens, source_size_bytes FROM versions WHERE version = '26.3'"
        ).fetchone()

    assert row is not None
    assert row["db_path"] == str(index)
    # 5 个 .java + 1 个 data/*.json（与 `tests/test_jobs.py` 的 fixture 口径一致）。
    assert row["files"] == 6
    assert row["tokens"] > 0
    assert row["source_size_bytes"] > 0

    # 汇总打到 stdout：文件数 / 行数 / token / 内存峰值 / 耗时（规格补充 §4）。
    assert "索引完成" in captured.out
    assert "索引文件：6" in captured.out
    assert "行数：" in captured.out
    assert "内存峰值：" in captured.out
    assert "耗时：" in captured.out

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/v1/setup", json={"username": "admin", "password": "admin-password-1"}
        )
        assert created.status_code == 201, created.text

        response = client.get("/v1/symbol", params={"version": "26.3", "name": "Mob"})

        assert response.status_code == 200, response.text
        assert any(
            item["fqn"] == "net.minecraft.world.Mob" for item in response.json()["matches"]
        )


def test_second_run_reuses_index_and_keeps_bytes(
    configure_cli: None,
    settings: Settings,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """幂等（规格补充 §2）：第二次执行跳过 `parse`，产物字节与第一次完全一致。"""
    calls = record_stages(monkeypatch)
    index = index_path(settings.data_dir, "26.3")

    assert index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)]) == 0
    assert calls == ["parse", "summarize", "finalize"]
    first = hashlib.sha256(index.read_bytes()).hexdigest()

    calls.clear()
    assert index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)]) == 0

    assert calls == ["summarize", "finalize"], "第二次执行不该再跑 parse"
    assert hashlib.sha256(index.read_bytes()).hexdigest() == first
    assert REUSE_INDEX_MESSAGE in capsys.readouterr().out
    assert any(REUSE_INDEX_MESSAGE in message for message in job_logs(settings))


def test_force_rebuilds_index(
    configure_cli: None,
    settings: Settings,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--force`：即使索引已存在也重新 `parse`（写临时文件后原子替换，产物字节仍确定）。"""
    calls = record_stages(monkeypatch)
    index = index_path(settings.data_dir, "26.3")

    assert index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)]) == 0
    first = hashlib.sha256(index.read_bytes()).hexdigest()

    calls.clear()
    assert (
        index_cli.main(["--version", "26.3", "--source-dir", str(source_dir), "--force"]) == 0
    )

    assert calls == ["parse", "summarize", "finalize"]
    assert hashlib.sha256(index.read_bytes()).hexdigest() == first


def test_reuse_requires_legal_index(
    configure_cli: None,
    settings: Settings,
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引文件存在但不是合法索引（0 字节）时不复用：正常重建，不把坏文件登记成版本。"""
    index = index_path(settings.data_dir, "26.3")
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_bytes(b"")

    calls = record_stages(monkeypatch)

    assert index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)]) == 0
    assert calls == ["parse", "summarize", "finalize"]

    with Database.for_data_dir(settings.data_dir).connect() as conn:
        row = conn.execute("SELECT files FROM versions WHERE version = '26.3'").fetchone()

    assert row is not None
    assert row["files"] == 6


def test_job_row_is_recorded_in_jobs_table(
    configure_cli: None, settings: Settings, source_dir: Path
) -> None:
    """CLI 走的是 `jobs` 队列（复用 `JobRunner`），不是另写一套流水线。"""
    assert index_cli.main(["--version", "26.3", "--source-dir", str(source_dir)]) == 0

    with Database.for_data_dir(settings.data_dir).connect() as conn:
        rows = store.list_jobs(conn, limit=10)

    assert [row.status for row in rows] == ["succeeded"]
    assert rows[0].kind == "index"
    assert rows[0].stage == "finalize"


def test_repeat_run_prints_source_tree_and_index_reuse(
    configure_cli: None,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """重复执行同一条命令：`decompile` 与 `parse` 都跳过，两条提示都打出来（规格补充 §2）。

    反编译用假工具链（源码树与 fetch 完成标记都是预置的，所以 `decompiler` 不该被调用）。
    """
    tools = PipelineTools(
        manifest=ModernManifest(),
        downloader=UnusedDownloader(),
        remapper=UnusedRemapper(),
        decompiler=UnusedDecompiler(),
    )
    monkeypatch.setattr(runner_module, "default_tools", lambda settings: tools)

    cache = cache_dir_for(settings.data_dir, "26.3")
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "client.jar").write_bytes(b"jar")
    (cache / "fetch.json").write_text(
        json.dumps({"stages": ["download", "decompile"], "java_files": 1}), encoding="utf-8"
    )
    package = source_root(settings.data_dir, "26.3") / "net" / "minecraft"
    package.mkdir(parents=True, exist_ok=True)
    (package / "Planted.java").write_text(
        "package net.minecraft;\n\npublic class Planted {}\n", encoding="utf-8"
    )

    assert index_cli.main(["--version", "26.3"]) == 0
    assert REUSE_SOURCE_TREE_MESSAGE in capsys.readouterr().out

    assert index_cli.main(["--version", "26.3"]) == 0
    repeated = capsys.readouterr().out

    assert REUSE_SOURCE_TREE_MESSAGE in repeated
    assert REUSE_INDEX_MESSAGE in repeated
