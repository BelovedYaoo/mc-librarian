"""下载 + 反编译流水线（issue #21）：清单解析、两个阶段、续跑、CLI 与 JobRunner 接线。

全部用本地构造的假 jar / 假反编译产物，不联网、不跑真反编译；真实链路由
`tests/test_fetch_network.py`（标 `network`，默认跳过）覆盖。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mc_librarian.config import Settings
from mc_librarian.db import Database, transaction
from mc_librarian.fetch import cli as fetch_cli
from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.manifest import HttpManifestResolver, VersionDownloads
from mc_librarian.fetch.pipeline import (
    CHECKPOINT_FILE_NAME,
    FetchContext,
    count_java_files,
    decompile_stage,
    download_stage,
    prune_empty_dirs,
    read_checkpoint,
    run_fetch,
)
from mc_librarian.fetch.tools import (
    HttpDownloader,
    PipelineTools,
    SpecialSourceRemapper,
    VineflowerDecompiler,
)
from mc_librarian.indexing.layout import source_root
from mc_librarian.jobs import runner as runner_module
from mc_librarian.jobs import store
from mc_librarian.jobs.runner import JobContext, JobRunner, StageError, run_stage, stages_for
from mc_librarian.main import create_app

TERMINAL = {"succeeded", "failed", "canceled"}
WAIT_TIMEOUT_S = 10.0

MODERN = VersionDownloads(
    version="26.3",
    client_url="https://cdn.example/26.3/client.jar",
    client_mappings_url=None,
)
OBFUSCATED = VersionDownloads(
    version="1.20.1",
    client_url="https://cdn.example/1.20.1/client.jar",
    client_mappings_url="https://cdn.example/1.20.1/client_mappings.txt",
)

# 假反编译器的产物：能解析出类的合法 `.java`（`test_full_pipeline_index_is_queryable...`
# 靠它验证「五段任务跑完直接用 `/v1/symbol` 查到符号」）。
DECOMPILED_MOB_CATEGORY = """\
package net.minecraft;

public class MobCategory {
  public int maxInstancesPerChunk;

  public int getMaxInstancesPerChunk() {
    return maxInstancesPerChunk;
  }
}
"""
DECOMPILED_LEVEL = """\
package net.minecraft;

public class Level {
  public void tick() {}
}
"""


# ----------------------------------------------------------------------
# 假实现：下载 / remap / 反编译都只在本地落文件
# ----------------------------------------------------------------------


class FakeManifest:
    def __init__(self, downloads: dict[str, VersionDownloads]) -> None:
        self._downloads = downloads
        self.calls: list[str] = []

    def resolve(self, version: str) -> VersionDownloads:
        self.calls.append(version)

        if version not in self._downloads:
            raise FetchError(f"未知版本：{version}")

        return self._downloads[version]


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def download(self, url: str, dest: Path) -> Path:
        self.calls.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"payload:{url}".encode())

        return dest


class FakeRemapper:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, Path]] = []

    def remap(self, jar: Path, mappings: Path, dest: Path) -> Path:
        self.calls.append((jar, mappings, dest))
        dest.write_bytes(jar.read_bytes() + b"\nremapped")

        return dest


class FakeDecompiler:
    """写一棵假源码树：两个含真实类的 `.java` 加一个非 `.java`（不该被计数）。

    `.java` 内容必须是能被 tree-sitter 解析出类的合法源码，否则五段任务跑完 `parse` 也查不到符号。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def decompile(self, jar: Path, dest: Path) -> Path:
        self.calls.append((jar, dest))
        package = dest / "net" / "minecraft"
        package.mkdir(parents=True, exist_ok=True)
        (package / "MobCategory.java").write_text(DECOMPILED_MOB_CATEGORY, encoding="utf-8")
        (package / "Level.java").write_text(DECOMPILED_LEVEL, encoding="utf-8")
        # 非 `.java` 文件与空目录都不该留在产物里。
        meta = dest / "META-INF"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "MANIFEST.MF").write_text("Manifest-Version: 1.0\n", encoding="utf-8")
        (dest / "assets" / "minecraft").mkdir(parents=True, exist_ok=True)

        return dest


def fake_tools(
    downloads: dict[str, VersionDownloads],
) -> tuple[PipelineTools, FakeManifest, FakeDownloader, FakeRemapper, FakeDecompiler]:
    manifest = FakeManifest(downloads)
    downloader = FakeDownloader()
    remapper = FakeRemapper()
    decompiler = FakeDecompiler()
    tools = PipelineTools(
        manifest=manifest,
        downloader=downloader,
        remapper=remapper,
        decompiler=decompiler,
    )

    return tools, manifest, downloader, remapper, decompiler


# ----------------------------------------------------------------------
# 清单解析
# ----------------------------------------------------------------------


def test_manifest_resolver_reads_client_and_mappings() -> None:
    payloads = {
        "https://meta/version_manifest_v2.json": json.dumps(
            {
                "versions": [
                    {"id": "1.20.1", "url": "https://meta/1.20.1.json"},
                    {"id": "26.3", "url": "https://meta/26.3.json"},
                ]
            }
        ).encode(),
        "https://meta/1.20.1.json": json.dumps(
            {
                "downloads": {
                    "client": {"url": "https://cdn/client.jar"},
                    "client_mappings": {"url": "https://cdn/client_mappings.txt"},
                }
            }
        ).encode(),
        "https://meta/26.3.json": json.dumps(
            {"downloads": {"client": {"url": "https://cdn/26.3.jar"}}}
        ).encode(),
    }
    resolver = HttpManifestResolver(
        manifest_url="https://meta/version_manifest_v2.json",
        fetch=payloads.__getitem__,
    )

    obfuscated = resolver.resolve("1.20.1")
    assert obfuscated.needs_remap is True
    assert obfuscated.client_url == "https://cdn/client.jar"
    assert obfuscated.client_mappings_url == "https://cdn/client_mappings.txt"

    # 26.x 的元数据里没有 client_mappings → 直接反编译。
    modern = resolver.resolve("26.3")
    assert modern.needs_remap is False
    assert modern.client_mappings_url is None


def test_manifest_resolver_rejects_unknown_version() -> None:
    resolver = HttpManifestResolver(fetch=lambda url: json.dumps({"versions": []}).encode())

    with pytest.raises(FetchError, match="找不到版本"):
        resolver.resolve("nope")


# ----------------------------------------------------------------------
# 两个阶段
# ----------------------------------------------------------------------


def test_run_fetch_without_mappings_decompiles_directly(tmp_path: Path) -> None:
    tools, _, downloader, remapper, decompiler = fake_tools({"26.3": MODERN})
    source = tmp_path / "src" / "26.3"
    cache = tmp_path / "cache" / "26.3"
    logs: list[str] = []

    result = run_fetch("26.3", source, cache, tools, lambda level, message: logs.append(message))

    assert remapper.calls == []
    assert [url for url, _ in downloader.calls] == [MODERN.client_url]
    assert decompiler.calls[0][0] == cache / "client.jar"
    # 只数 `.java`，`META-INF/MANIFEST.MF` 不计。
    assert result.java_files == 2
    assert result.java_bytes > 0
    assert result.checkpoint["obfuscated"] is False
    assert result.checkpoint["stages"] == ["download", "decompile"]
    assert result.checkpoint["jar_path"] == str(cache / "client.jar")
    assert result.checkpoint["source_dir"] == str(source)
    assert result.checkpoint["mappings_path"] is None
    # `decompile` 用 `java_files`/`java_bytes`，不与 `parse` 的 `files`/`source_size_bytes` 撞名。
    assert result.checkpoint["java_files"] == 2
    assert result.checkpoint["java_bytes"] > 0
    assert "files" not in result.checkpoint
    assert "source_size_bytes" not in result.checkpoint
    assert read_checkpoint(cache / CHECKPOINT_FILE_NAME) == result.checkpoint
    assert any("无官方 mappings" in line for line in logs)
    # Vineflower 会建出空的 `assets/`，应被清理。
    assert not (source / "assets").exists()


def test_run_fetch_with_mappings_remaps_before_decompiling(tmp_path: Path) -> None:
    tools, _, downloader, remapper, decompiler = fake_tools({"1.20.1": OBFUSCATED})
    source = tmp_path / "src" / "1.20.1"
    cache = tmp_path / "cache" / "1.20.1"

    result = run_fetch("1.20.1", source, cache, tools, lambda level, message: None)

    assert [url for url, _ in downloader.calls] == [
        OBFUSCATED.client_url,
        OBFUSCATED.client_mappings_url,
    ]
    assert remapper.calls == [
        (cache / "client.jar", cache / "client_mappings.txt", cache / "client-remapped.jar")
    ]
    # 反编译的是 remap 后的 jar，不是原始混淆 jar。
    assert decompiler.calls[0][0] == cache / "client-remapped.jar"
    assert result.checkpoint["obfuscated"] is True
    assert result.checkpoint["mappings_path"] == str(cache / "client_mappings.txt")
    assert result.checkpoint["remapped_jar_path"] == str(cache / "client-remapped.jar")


def test_download_stage_reuses_cached_jar_and_mappings(tmp_path: Path) -> None:
    tools, _, downloader, remapper, _ = fake_tools({"1.20.1": OBFUSCATED})
    context = FetchContext(
        version="1.20.1",
        cache_dir=tmp_path / "cache",
        source_dir=tmp_path / "src",
        log=lambda level, message: None,
        checkpoint={},
    )

    download_stage(context, tools)
    download_stage(context, tools)

    # jar 与 mappings 各下载一次，remap 也只跑一次。
    assert len(downloader.calls) == 2
    assert len(remapper.calls) == 1


def test_decompile_stage_requires_download_first(tmp_path: Path) -> None:
    tools, *_ = fake_tools({})
    context = FetchContext(
        version="26.3",
        cache_dir=tmp_path / "cache",
        source_dir=tmp_path / "src",
        log=lambda level, message: None,
        checkpoint={},
    )

    with pytest.raises(FetchError, match="jar_path"):
        decompile_stage(context, tools)


def test_count_java_files_ignores_non_java(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "net").mkdir(parents=True)
    (root / "net" / "A.java").write_text("a", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "x.png").write_bytes(b"png")

    assert count_java_files(root) == (1, 1)


def test_prune_empty_dirs_removes_assets_and_data(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "net" / "minecraft").mkdir(parents=True)
    (root / "net" / "minecraft" / "A.java").write_text("a", encoding="utf-8")
    (root / "assets" / "minecraft").mkdir(parents=True)
    (root / "data" / "minecraft").mkdir(parents=True)
    (root / "META-INF").mkdir()

    assert prune_empty_dirs(root) == 5
    assert not (root / "assets").exists()
    assert not (root / "data").exists()
    assert not (root / "META-INF").exists()
    assert (root / "net" / "minecraft" / "A.java").is_file()


# ----------------------------------------------------------------------
# 续跑
# ----------------------------------------------------------------------


def test_run_fetch_resumes_from_checkpoint(tmp_path: Path) -> None:
    tools, manifest, downloader, _, decompiler = fake_tools({"26.3": MODERN})
    source = tmp_path / "src" / "26.3"
    cache = tmp_path / "cache" / "26.3"

    run_fetch("26.3", source, cache, tools, lambda level, message: None)
    assert manifest.calls == ["26.3"]

    again = run_fetch("26.3", source, cache, tools, lambda level, message: None)

    # 两个阶段都已完成：第二次不重复下载、不重复反编译。
    assert manifest.calls == ["26.3"]
    assert len(downloader.calls) == 1
    assert len(decompiler.calls) == 1
    assert again.java_files == 2


def test_run_fetch_resumes_after_partial_run(tmp_path: Path) -> None:
    tools, manifest, _, _, decompiler = fake_tools({"1.20.1": OBFUSCATED})
    source = tmp_path / "src" / "1.20.1"
    cache = tmp_path / "cache" / "1.20.1"
    cache.mkdir(parents=True)

    # 模拟「download 已完成、decompile 还没跑」：checkpoint 里只有 download 与 jar 路径。
    (cache / "client-remapped.jar").write_bytes(b"jar")
    checkpoint = {
        "stages": ["download"],
        "jar_path": str(cache / "client-remapped.jar"),
    }

    result = run_fetch(
        "1.20.1", source, cache, tools, lambda level, message: None, checkpoint=checkpoint
    )

    assert manifest.calls == []  # download 阶段被跳过
    assert len(decompiler.calls) == 1
    assert result.checkpoint["stages"] == ["download", "decompile"]


def test_read_checkpoint_tolerates_missing_and_broken(tmp_path: Path) -> None:
    assert read_checkpoint(tmp_path / "nope.json") == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_checkpoint(broken) == {}


def test_settings_rejects_invalid_decompile_memory(tmp_path: Path) -> None:
    # `-Xmx` 值会被拼进 java 命令行，必须限形如 4G / 512M。
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path / "data", decompile_memory="4GB")

    assert Settings(data_dir=tmp_path / "data", decompile_memory="512M").decompile_memory == "512M"


def test_settings_reads_toolchain_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MC_LIBRARIAN_JAVA", "/opt/jdk/bin/java")
    monkeypatch.setenv("MC_LIBRARIAN_VINEFLOWER_JAR", str(tmp_path / "vineflower.jar"))
    monkeypatch.setenv("MC_LIBRARIAN_DECOMPILE_MEMORY", "2G")

    settings = Settings(data_dir=tmp_path / "data")

    assert settings.java == "/opt/jdk/bin/java"
    assert settings.vineflower_jar == tmp_path / "vineflower.jar"
    assert settings.decompile_memory == "2G"


# ----------------------------------------------------------------------
# 外部命令封装
# ----------------------------------------------------------------------


def test_vineflower_command_uses_configured_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(command)
        Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mc_librarian.fetch.tools.subprocess.run", fake_run)

    jar = tmp_path / "vineflower.jar"
    jar.write_bytes(b"jar")
    source_jar = tmp_path / "client.jar"
    source_jar.write_bytes(b"jar")
    dest = tmp_path / "src"

    VineflowerDecompiler(jar_path=jar, java="java", memory="2G").decompile(source_jar, dest)

    command = recorded[0]
    assert command[0] == "java"
    assert "-Xmx2G" in command
    assert "--folder" in command
    assert "--skip-extra-files" in command
    assert command[-1] == str(dest)


def test_specialsource_command_applies_mappings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mc_librarian.fetch.tools.subprocess.run", fake_run)

    jar = tmp_path / "specialsource.jar"
    jar.write_bytes(b"jar")
    client = tmp_path / "client.jar"
    client.write_bytes(b"jar")
    mappings = tmp_path / "client_mappings.txt"
    mappings.write_text("", encoding="utf-8")

    SpecialSourceRemapper(jar_path=jar, java="java").remap(
        client, mappings, tmp_path / "remapped.jar"
    )

    command = recorded[0]
    assert command[:3] == ["java", "-jar", str(jar)]
    assert command[command.index("-i") + 1] == str(client)
    assert command[command.index("-m") + 1] == str(mappings)


def test_vineflower_requires_configured_jar(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="MC_LIBRARIAN_VINEFLOWER_JAR"):
        VineflowerDecompiler(jar_path=None).decompile(tmp_path / "a.jar", tmp_path / "src")


def test_http_downloader_writes_file_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    dest = tmp_path / "nested" / "client.jar"

    HttpDownloader().download(source.as_uri(), dest)

    assert dest.read_bytes() == b"hello"
    assert not (dest.with_name("client.jar.part")).exists()


# ----------------------------------------------------------------------
# CLI 接线
# ----------------------------------------------------------------------


def test_cli_runs_pipeline_and_reports_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools, *_ = fake_tools({"26.3": MODERN})
    monkeypatch.setattr(fetch_cli, "default_tools", lambda *args, **kwargs: tools)
    monkeypatch.setattr(fetch_cli, "get_settings", lambda: Settings(data_dir=tmp_path / "data"))

    code = fetch_cli.main(
        [
            "--version",
            "26.3",
            "--out",
            str(tmp_path / "src" / "26.3"),
            "--cache",
            str(tmp_path / "cache" / "26.3"),
        ]
    )

    assert code == 0
    assert "2 个 .java" in capsys.readouterr().out


def test_cli_reports_fetch_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools, *_ = fake_tools({})  # 没有 26.3
    monkeypatch.setattr(fetch_cli, "default_tools", lambda *args, **kwargs: tools)
    monkeypatch.setattr(fetch_cli, "get_settings", lambda: Settings(data_dir=tmp_path / "data"))

    code = fetch_cli.main(
        ["--version", "26.3", "--out", str(tmp_path / "src"), "--cache", str(tmp_path / "cache")]
    )

    assert code == 1
    assert "fetch 失败" in capsys.readouterr().err


# ----------------------------------------------------------------------
# JobRunner 接线
# ----------------------------------------------------------------------


def test_stages_for_selects_sequence_by_source_dir() -> None:
    assert stages_for({"source_dir": "/tmp/x"}) == ("parse", "summarize", "finalize")
    # 没给源目录：从官方 jar 起跑完整五段，不需要人工衔接三步。
    assert stages_for({}) == ("download", "decompile", "parse", "summarize", "finalize")
    assert stages_for({"source_dir": ""}) == (
        "download",
        "decompile",
        "parse",
        "summarize",
        "finalize",
    )


def test_run_stage_rejects_unknown_stage(settings: Settings) -> None:
    """未知阶段直接报错，而不是静默返回空产物（新增阶段漏改时不会产出残缺索引）。"""
    context = JobContext(
        version="26.3",
        options={},
        data_dir=settings.data_dir,
        log=lambda level, message: None,
    )

    with pytest.raises(StageError, match="未知阶段"):
        run_stage("bogus", context)


def test_runner_runs_full_pipeline_without_source_dir(settings: Settings) -> None:
    """没给 source_dir：一次任务从官方 jar 跑完五段，产出可检索索引并登记版本。"""
    database = Database.for_data_dir(settings.data_dir)
    database.initialize()

    with database.connect() as conn, transaction(conn):
        job = store.create(conn, kind="index", version="26.3", options={}, created_by=None)

    tools, _, _, _, decompiler = fake_tools({"26.3": MODERN})
    runner = JobRunner(settings, database, poll_interval=0.01, tools=tools)

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

            raise AssertionError("五段任务未在超时内结束")
        finally:
            await runner.stop()

    asyncio.run(run_until_terminal())

    with database.connect() as conn:
        finished = store.find(conn, job.id)
        row = store.find_row(conn, job.id)
        version = store.find_version(conn, "26.3")
        version_row = conn.execute(
            "SELECT db_path, obfuscated, source_size_bytes FROM versions WHERE version = '26.3'"
        ).fetchone()

    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.stage == "finalize"
    assert row is not None

    checkpoint = store.checkpoint_of(row)
    assert checkpoint["stages"] == ["download", "decompile", "parse", "summarize", "finalize"]
    assert checkpoint["source_dir"] == str(source_root(settings.data_dir, "26.3"))
    # decompile 的 .java 计数与 parse 的索引文件数各写各的，不互相覆盖。
    assert checkpoint["java_files"] == 2
    assert checkpoint["files"] == 2
    assert len(decompiler.calls) == 1
    # 跑过 parse：`db/<version>.sqlite` 产出且版本已登记。
    assert (settings.data_dir / "db" / "26.3.sqlite").is_file()
    assert version is not None
    assert version.files == 2

    # 版本行把产出索引、是否 remap、源码字节数都写上：`db_path` 指向真实文件，
    # 后两项与 checkpoint 一致（否则 `/v1/versions` 会列出该版本、检索端点却回 404）。
    assert version_row is not None
    assert version_row["db_path"] == str(settings.data_dir / "db" / "26.3.sqlite")
    assert Path(version_row["db_path"]).is_file()
    assert version_row["obfuscated"] == int(checkpoint["obfuscated"])
    assert version_row["source_size_bytes"] == checkpoint["source_size_bytes"]


def test_full_pipeline_index_is_queryable_without_manual_registration(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#21 验收：一次不带 `options` 的任务跑完五段后，直接能用 `/v1/symbol` 查到符号。

    这里用需要 remap 的 1.20.1（≤ 1.21.11 路径），顺带断言版本行记的是 `obfuscated=True`
    （前端「已 remap」标记靠它）。以前任务只登记 `files`/`tokens`，`db_path` 恒为 NULL，
    任务「成功」后四个检索端点全部 404。
    """
    tools, *_ = fake_tools({"1.20.1": OBFUSCATED})
    monkeypatch.setattr(runner_module, "default_tools", lambda settings: tools)

    with TestClient(create_app(settings)) as client:
        setup = client.post(
            "/v1/setup", json={"username": "admin", "password": "admin-password-1"}
        )
        assert setup.status_code == 201, setup.text

        created = client.post("/v1/admin/jobs", json={"kind": "index", "version": "1.20.1"})
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]

        deadline = time.monotonic() + WAIT_TIMEOUT_S
        job: dict[str, Any] = {}

        while time.monotonic() < deadline:
            job = client.get(f"/v1/admin/jobs/{job_id}").json()

            if job["status"] in TERMINAL:
                break

            time.sleep(0.05)
        else:
            raise AssertionError(f"五段任务未在超时内结束：{job}")

        assert job["status"] == "succeeded"

        # 不再需要手工注册：任务一成功，索引端点就能查到刚反编译出来的类。
        response = client.get(
            "/v1/symbol", params={"version": "1.20.1", "name": "MobCategory"}
        )

        assert response.status_code == 200, response.text
        matches = response.json()["matches"]
        assert any(item["fqn"] == "net.minecraft.MobCategory" for item in matches)

        versions = client.get("/v1/versions").json()["versions"]
        registered = next(item for item in versions if item["version"] == "1.20.1")
        assert registered["obfuscated"] is True

    with Database.for_data_dir(settings.data_dir).connect() as conn:
        row = conn.execute(
            "SELECT db_path, obfuscated, source_size_bytes FROM versions WHERE version = '1.20.1'"
        ).fetchone()
        job_row = store.find_row(conn, job_id)

    assert row is not None
    assert job_row is not None

    checkpoint = store.checkpoint_of(job_row)
    assert row["db_path"] == str(settings.data_dir / "db" / "1.20.1.sqlite")
    assert Path(row["db_path"]).is_file()
    assert row["obfuscated"] == int(checkpoint["obfuscated"]) == 1
    assert row["source_size_bytes"] == checkpoint["source_size_bytes"]


def test_create_job_without_options_runs_full_pipeline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口层放开 `options`：省略 options 的 26.3 任务一次跑完五段并登记版本。

    用假工具链替换 `JobRunner` 的 `default_tools`，避免真去联网下载与反编译。
    """
    tools, *_ = fake_tools({"26.3": MODERN})
    monkeypatch.setattr(runner_module, "default_tools", lambda settings: tools)

    with TestClient(create_app(settings)) as client:
        setup = client.post(
            "/v1/setup", json={"username": "admin", "password": "admin-password-1"}
        )
        assert setup.status_code == 201, setup.text

        created = client.post("/v1/admin/jobs", json={"kind": "index", "version": "26.3"})
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]

        deadline = time.monotonic() + WAIT_TIMEOUT_S

        while time.monotonic() < deadline:
            job = client.get(f"/v1/admin/jobs/{job_id}").json()

            if job["status"] in TERMINAL:
                break

            time.sleep(0.05)
        else:
            raise AssertionError(f"五段任务未在超时内结束：{job}")

        assert job["status"] == "succeeded"
        assert job["stage"] == "finalize"
        versions = client.get("/v1/versions").json()["versions"]
        assert [item["version"] for item in versions] == ["26.3"]
