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
    source_dir_for,
)
from mc_librarian.fetch.tools import (
    HttpDownloader,
    PipelineTools,
    SpecialSourceRemapper,
    VineflowerDecompiler,
)
from mc_librarian.jobs import store
from mc_librarian.jobs.runner import JobRunner, stages_for

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
    """写一棵假源码树：两个 `.java` 加一个非 `.java`（不该被计数）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def decompile(self, jar: Path, dest: Path) -> Path:
        self.calls.append((jar, dest))
        package = dest / "net" / "minecraft"
        package.mkdir(parents=True, exist_ok=True)
        (package / "MobCategory.java").write_text("package net.minecraft;\n", encoding="utf-8")
        (package / "Level.java").write_text("package net.minecraft;\n", encoding="utf-8")
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
    assert result.files == 2
    assert result.source_size_bytes > 0
    assert result.checkpoint["obfuscated"] is False
    assert result.checkpoint["stages"] == ["download", "decompile"]
    assert result.checkpoint["jar_path"] == str(cache / "client.jar")
    assert result.checkpoint["source_dir"] == str(source)
    assert result.checkpoint["mappings_path"] is None
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
    assert again.files == 2


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
    assert stages_for({}) == ("download", "decompile")


def test_runner_runs_fetch_stages_without_source_dir(settings: Settings) -> None:
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

            raise AssertionError("fetch 任务未在超时内结束")
        finally:
            await runner.stop()

    asyncio.run(run_until_terminal())

    with database.connect() as conn:
        finished = store.find(conn, job.id)
        row = store.find_row(conn, job.id)
        version = store.find_version(conn, "26.3")

    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.stage == "decompile"
    assert row is not None

    checkpoint = store.checkpoint_of(row)
    assert checkpoint["stages"] == ["download", "decompile"]
    assert checkpoint["source_dir"] == str(source_dir_for(settings.data_dir, "26.3"))
    assert checkpoint["files"] == 2
    assert len(decompiler.calls) == 1
    # 纯 download/decompile 的任务产出源码树，不登记版本（登记要等 #13 接上 parse）。
    assert version is None
