"""下载 / remap / 反编译三个外部动作的封装：可注入接口 + 默认实现。

`pipeline` 只依赖这里的协议；测试注入假实现，真实路径由 CLI 与 `JobRunner` 注入默认实现。
默认实现把网络与子进程细节关在这里：

- `HttpDownloader` —— 标准库 urllib，先写 `.part` 再原子改名；
- `SpecialSourceRemapper` —— 把官方 ProGuard mappings 应用到混淆 jar（见 docs/00-overview.md §6）；
- `VineflowerDecompiler` —— 子进程调 Vineflower，`-Xmx` 由 `memory` 控制，
  `--skip-extra-files` 保证产出只含 `.java`（不含 `assets/`、`data/`、`META-INF/`）。
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.request import urlopen

from mc_librarian.config import Settings
from mc_librarian.fetch.chunks import class_names_of, plan_chunks, select_classes
from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.manifest import HttpManifestResolver, ManifestResolver

DOWNLOAD_TIMEOUT_S: Final = 300.0
# 反编译 7.5M token 的版本可能跑十几分钟，给足余量。
COMMAND_TIMEOUT_S: Final = 60 * 60.0
STDERR_TAIL: Final = 2000


class Downloader(Protocol):
    def download(self, url: str, dest: Path) -> Path: ...


class HttpDownloader:
    """urllib 下载到文件；先写 `.part` 再改名，半截文件不会被当成完成品。"""

    def __init__(self, *, timeout: float = DOWNLOAD_TIMEOUT_S) -> None:
        self._timeout = timeout

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")

        try:
            with urlopen(url, timeout=self._timeout) as response, partial.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise FetchError(f"下载失败：{url}（{error}）") from error

        partial.replace(dest)

        return dest


class Remapper(Protocol):
    def remap(self, jar: Path, mappings: Path, dest: Path) -> Path: ...


@dataclass(frozen=True)
class SpecialSourceRemapper:
    """用 SpecialSource shaded jar 应用官方 mappings。

    SpecialSource 读 ProGuard 格式（`named -> obfuscated:`）时会自动把右侧当输入，
    所以直接对混淆 jar 调用即可得到命名后的 jar（实测 1.11.5）。
    """

    jar_path: Path | None = None
    java: str = "java"
    # 实测峰值 ~600 MB：不设上限时 JVM 按物理内存 1/4 预留，节点上没必要。
    memory: str = "512m"

    def remap(self, jar: Path, mappings: Path, dest: Path) -> Path:
        jar_path = _require_jar(self.jar_path, "SpecialSource")
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                self.java,
                f"-Xmx{self.memory}",
                "-jar",
                str(jar_path),
                "-i",
                str(jar),
                "-o",
                str(dest),
                "-m",
                str(mappings),
                "-q",
            ],
            label="SpecialSource remap",
        )

        return dest


class Decompiler(Protocol):
    def decompile(self, jar: Path, dest: Path) -> Path: ...


@dataclass(frozen=True)
class VineflowerDecompiler:
    """Vineflower 子进程，按类名前缀**分块**跑。

    分块的理由见 `fetch.chunks` 的模块注释：内存峰值取决于单次处理的类数量。
    每块输出到临时目录，全部跑完后合并进 `dest`——中途失败不会留下「半个源码树」。

    JVM 参数也是省内存的一部分（实测整包 1.20.1：1906 MB → 1719 MB）：

    - `MaxMetaspaceSize` / `ReservedCodeCacheSize` 给上限，不让 JVM 按「无上限」预留；
    - `ActiveProcessorCount` + `--thread-count` 与节点核数对齐：默认 32 线程每个都要栈和上下文；
    - 不开 SerialGC：实测它把 54s 拖到 477s，省的那点内存不值。
    """

    jar_path: Path | None = None
    java: str = "java"
    memory: str = "1G"
    chunk_classes: int = 1600
    threads: int = 2

    def decompile(self, jar: Path, dest: Path) -> Path:
        jar_path = _require_jar(self.jar_path, "Vineflower")

        if dest.exists():
            shutil.rmtree(dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.mkdir(parents=True, exist_ok=True)

        try:
            names = class_names_of(jar)
        except zipfile.BadZipFile:
            # 不是合法 jar：交给 Vineflower 报原始错误，别在分块阶段吞掉真正的原因。
            names = []

        if self.chunk_classes <= 0 or len(names) <= self.chunk_classes:
            self._run(jar_path, jar, dest, label=f"Vineflower 反编译（{len(names)} 类）")
            return dest

        chunks = plan_chunks(names, max_classes=self.chunk_classes)
        staging = dest.parent / f".{dest.name}.chunks"

        if staging.exists():
            shutil.rmtree(staging)

        try:
            for index, (prefixes, count) in enumerate(chunks, start=1):
                block = staging / f"{index:03d}"
                block.mkdir(parents=True)
                # 抽一个只含本块类的子 jar：Vineflower 1.12 在多个 `--only` 时会往不存在的
                # 目录写文件（`NoSuchFileException`）并退出 1，实测不可用。子 jar 没有这个
                # 问题，还省掉了「前缀匹配会不会漏类」的推理。
                subset = staging / f"{index:03d}.jar"
                _write_subset_jar(jar, select_classes(names, prefixes), subset)

                label = (
                    f"Vineflower 反编译 第 {index}/{len(chunks)} 块"
                    f"（{count} 类：{', '.join(prefixes[:3])}{'…' if len(prefixes) > 3 else ''}）"
                )
                self._run(jar_path, subset, block, label=label)
                subset.unlink()
                # 逐块合并：即使后面某块失败，已完成的块也都在 `dest` 里（阶段会整体重跑）。
                _merge_tree(block, dest)
                shutil.rmtree(block)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        return dest

    def _run(
        self,
        jar_path: Path,
        jar: Path,
        out: Path,
        *,
        label: str,
    ) -> None:
        command = [
            self.java,
            f"-Xmx{self.memory}",
            "-XX:MaxMetaspaceSize=192m",
            "-XX:ReservedCodeCacheSize=64m",
            f"-XX:ActiveProcessorCount={self.threads}",
            "-jar",
            str(jar_path),
            "--folder",
            "--skip-extra-files",
            f"--thread-count={self.threads}",
            str(jar),
            str(out),
        ]
        run_command(command, label=label)


def _write_subset_jar(source: Path, class_names: list[str], target: Path) -> None:
    """从原 jar 抽出指定类，写成子 jar（保持原条目路径与压缩）。"""
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED
    ) as subset:
        for name in class_names:
            entry = f"{name}.class"
            try:
                data = archive.read(entry)
            except KeyError:
                continue

            subset.writestr(entry, data)


def _merge_tree(source: Path, dest: Path) -> None:
    """把一块的产物合并进目标树（同名覆盖，不删已有文件）。"""
    for item in source.iterdir():
        target = dest / item.name

        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


@dataclass(frozen=True)
class PipelineTools:
    """流水线依赖的四个外部动作，整体注入。"""

    manifest: ManifestResolver
    downloader: Downloader
    remapper: Remapper
    decompiler: Decompiler


def default_tools(
    settings: Settings,
    *,
    vineflower_jar: Path | None = None,
    specialsource_jar: Path | None = None,
    java: str | None = None,
    memory: str | None = None,
    chunk_classes: int | None = None,
    threads: int | None = None,
) -> PipelineTools:
    """真实工具链。参数为 `None` 时退回 `Settings`（即 `MC_LIBRARIAN_*` 环境变量）。"""
    java_bin = java or settings.java

    return PipelineTools(
        manifest=HttpManifestResolver(),
        downloader=HttpDownloader(),
        remapper=SpecialSourceRemapper(
            jar_path=specialsource_jar or settings.specialsource_jar,
            java=java_bin,
        ),
        decompiler=VineflowerDecompiler(
            jar_path=vineflower_jar or settings.vineflower_jar,
            java=java_bin,
            memory=memory or settings.decompile_memory,
            chunk_classes=(
                settings.decompile_chunk_classes if chunk_classes is None else chunk_classes
            ),
            threads=settings.decompile_threads if threads is None else threads,
        ),
    )


def run_command(command: list[str], *, label: str) -> None:
    """跑外部命令；非零退出把 stderr 末尾带进 `FetchError`，方便定位。"""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S
        )
    except FileNotFoundError as error:
        raise FetchError(f"{label} 失败：找不到可执行文件 {command[0]}（{error}）") from error
    except subprocess.TimeoutExpired as error:
        raise FetchError(f"{label} 超时（{COMMAND_TIMEOUT_S:.0f}s）") from error

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-STDERR_TAIL:]
        raise FetchError(f"{label} 失败（退出码 {completed.returncode}）：{tail}")


def _require_jar(path: Path | None, name: str) -> Path:
    if path is None or not path.is_file():
        raise FetchError(
            f"未配置 {name} jar：请设置 MC_LIBRARIAN_{name.upper()}_JAR 或传 CLI 参数"
        )

    return path
