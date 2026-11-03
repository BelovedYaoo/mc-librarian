"""下载 + 反编译流水线：把某个版本的官方 jar 变成一棵只含 `.java` 的源码树。

两个阶段的产物（写进 job 的 checkpoint，供续跑）：

- `download`  —— 解析清单、下载 client jar；有官方 mappings 时再下载并 remap；
- `decompile` —— 调 Vineflower 反编译，统计源码树的文件数与字节数。

`run_fetch` 是 CLI 的入口，`run_fetch_stage` 是 `JobRunner` 的入口，两者共用同一套阶段实现。
两个阶段都幂等：文件已在缓存目录里就不重复下载 / remap，checkpoint 里已完成的阶段直接跳过。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.tools import PipelineTools
from mc_librarian.schemas import LogLevel

CACHE_DIR_NAME: Final = "cache"
SOURCES_DIR_NAME: Final = "src"
CHECKPOINT_FILE_NAME: Final = "fetch.json"

CLIENT_JAR_NAME: Final = "client.jar"
MAPPINGS_NAME: Final = "client_mappings.txt"
REMAPPED_JAR_NAME: Final = "client-remapped.jar"

FETCH_STAGES: Final[tuple[str, ...]] = ("download", "decompile")


@dataclass(frozen=True)
class FetchContext:
    """一个 fetch 阶段的输入。"""

    version: str
    cache_dir: Path
    source_dir: Path
    log: Callable[[LogLevel, str], None]
    checkpoint: Mapping[str, Any]


@dataclass(frozen=True)
class FetchResult:
    source_dir: Path
    files: int
    source_size_bytes: int
    checkpoint: dict[str, Any]


def cache_dir_for(data_dir: Path, version: str) -> Path:
    """jar / mappings / checkpoint 的缓存目录。"""
    return data_dir / CACHE_DIR_NAME / version


def source_dir_for(data_dir: Path, version: str) -> Path:
    """反编译产出的默认源码树目录（`--out` 可覆盖）。"""
    return data_dir / SOURCES_DIR_NAME / version


def read_checkpoint(path: Path) -> dict[str, Any]:
    """读 checkpoint；文件缺失或损坏时退回空 dict，不阻断重跑。"""
    if not path.is_file():
        return {}

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return loaded if isinstance(loaded, dict) else {}


def write_checkpoint(path: Path, checkpoint: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def run_fetch_stage(stage: str, context: FetchContext, tools: PipelineTools) -> dict[str, Any]:
    if stage == "download":
        return download_stage(context, tools)

    if stage == "decompile":
        return decompile_stage(context, tools)

    raise FetchError(f"未知的 fetch 阶段：{stage}")


def download_stage(context: FetchContext, tools: PipelineTools) -> dict[str, Any]:
    """解析清单并下载；有官方 mappings 时 remap，返回 jar 路径等产物。"""
    downloads = tools.manifest.resolve(context.version)
    context.log("info", f"download：{context.version} → {downloads.client_url}")

    cache = context.cache_dir
    cache.mkdir(parents=True, exist_ok=True)

    client_jar = cache / CLIENT_JAR_NAME

    if client_jar.is_file():
        context.log("info", f"download：复用已有 client jar {client_jar}")
    else:
        tools.downloader.download(downloads.client_url, client_jar)
        context.log("info", f"download：client jar {client_jar.stat().st_size} 字节")

    jar_path = client_jar
    mappings_path: Path | None = None
    remapped_jar: Path | None = None

    if downloads.needs_remap:
        mappings_path = cache / MAPPINGS_NAME

        if mappings_path.is_file():
            context.log("info", f"download：复用已有 mappings {mappings_path}")
        else:
            assert downloads.client_mappings_url is not None, "needs_remap 为真时必有地址"
            tools.downloader.download(downloads.client_mappings_url, mappings_path)
            context.log("info", f"download：mappings {mappings_path.stat().st_size} 字节")

        remapped_jar = cache / REMAPPED_JAR_NAME

        if remapped_jar.is_file():
            context.log("info", f"download：复用已有 remapped jar {remapped_jar}")
        else:
            context.log("info", "download：SpecialSource remap")
            tools.remapper.remap(client_jar, mappings_path, remapped_jar)

        jar_path = remapped_jar
    else:
        context.log("info", "download：该版本无官方 mappings，直接反编译")

    return {
        "client_jar_path": str(client_jar),
        "client_jar_bytes": client_jar.stat().st_size,
        "jar_path": str(jar_path),
        "jar_bytes": jar_path.stat().st_size,
        "mappings_path": None if mappings_path is None else str(mappings_path),
        "remapped_jar_path": None if remapped_jar is None else str(remapped_jar),
        "obfuscated": downloads.needs_remap,
    }


def decompile_stage(context: FetchContext, tools: PipelineTools) -> dict[str, Any]:
    """反编译 `download` 产出的 jar，返回源码树路径、文件数与字节数。"""
    raw = context.checkpoint.get("jar_path")

    if not isinstance(raw, str) or not raw:
        raise FetchError("decompile：checkpoint 里没有 jar_path，请先执行 download 阶段")

    jar = Path(raw)

    if not jar.is_file():
        raise FetchError(f"decompile：jar 不存在：{jar}")

    context.log("info", f"decompile：{jar} → {context.source_dir}")
    tools.decompiler.decompile(jar, context.source_dir)
    prune_empty_dirs(context.source_dir)
    files, size_bytes = count_java_files(context.source_dir)

    if files == 0:
        raise FetchError(f"decompile：没有产出任何 .java 文件（{context.source_dir}）")

    context.log("info", f"decompile：{files} 个 .java、{size_bytes} 字节")

    return {
        "source_dir": str(context.source_dir),
        "files": files,
        "source_size_bytes": size_bytes,
    }


def prune_empty_dirs(root: Path) -> int:
    """删掉空目录，返回删除个数。

    Vineflower 的 `--skip-extra-files` 不拷非 class 文件，但仍会按 jar 里的目录条目建出
    空的 `assets/`、`data/`、`META-INF/`。这些空目录必须清掉，产物才真的「只含 `.java`」。
    """
    removed = 0

    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            removed += 1

    return removed


def count_java_files(root: Path) -> tuple[int, int]:
    """统计 `root` 下 `.java` 的文件数与总字节数（非 `.java` 一律不计）。"""
    files = 0
    size_bytes = 0

    for path in root.rglob("*.java"):
        if path.is_file():
            files += 1
            size_bytes += path.stat().st_size

    return files, size_bytes


def run_fetch(
    version: str,
    source_dir: Path,
    cache_dir: Path,
    tools: PipelineTools,
    log: Callable[[LogLevel, str], None],
    *,
    checkpoint: Mapping[str, Any] | None = None,
) -> FetchResult:
    """跑完两个阶段并逐步落盘 checkpoint；已完成的阶段直接跳过（续跑）。

    不传 `checkpoint` 时从 `<cache>/fetch.json` 读（CLI 的正常路径）；传了就按传入的跑。
    """
    if checkpoint is None:
        checkpoint = read_checkpoint(cache_dir / CHECKPOINT_FILE_NAME)

    state = dict(checkpoint)
    done = {str(name) for name in state.get("stages", [])}

    for stage in FETCH_STAGES:
        if stage in done:
            log("info", f"{stage}：已在 checkpoint 中完成，跳过")
            continue

        context = FetchContext(
            version=version,
            cache_dir=cache_dir,
            source_dir=source_dir,
            log=log,
            checkpoint=state,
        )
        stats = run_fetch_stage(stage, context, tools)
        done.add(stage)
        state = {**state, "stages": [name for name in FETCH_STAGES if name in done], **stats}
        write_checkpoint(cache_dir / CHECKPOINT_FILE_NAME, state)

    return FetchResult(
        source_dir=source_dir,
        files=int(state.get("files", 0)),
        source_size_bytes=int(state.get("source_size_bytes", 0)),
        checkpoint=state,
    )
