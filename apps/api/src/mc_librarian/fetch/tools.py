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
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.request import urlopen

from mc_librarian.config import Settings
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

    def remap(self, jar: Path, mappings: Path, dest: Path) -> Path:
        jar_path = _require_jar(self.jar_path, "SpecialSource")
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                self.java,
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
    """Vineflower 子进程。`dest` 已存在时先删掉，避免残留上一次的半成品。"""

    jar_path: Path | None = None
    java: str = "java"
    memory: str = "4G"

    def decompile(self, jar: Path, dest: Path) -> Path:
        jar_path = _require_jar(self.jar_path, "Vineflower")

        if dest.exists():
            shutil.rmtree(dest)

        dest.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                self.java,
                f"-Xmx{self.memory}",
                "-jar",
                str(jar_path),
                "--folder",
                "--skip-extra-files",
                str(jar),
                str(dest),
            ],
            label="Vineflower 反编译",
        )

        return dest


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
