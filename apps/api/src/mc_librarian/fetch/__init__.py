"""下载 + 反编译流水线（issue #21）：官方 jar → 只含 `.java` 的源码树。

- `manifest` —— Mojang 版本清单解析（jar 与 mappings 地址）；
- `tools`    —— 下载 / SpecialSource remap / Vineflower 反编译，可注入；
- `pipeline` —— 两个阶段的编排与 checkpoint 落盘；
- `cli`      —— `mc-librarian fetch` 入口。
"""

from __future__ import annotations

from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.manifest import (
    MANIFEST_URL,
    HttpManifestResolver,
    ManifestResolver,
    VersionDownloads,
)
from mc_librarian.fetch.pipeline import (
    CHECKPOINT_FILE_NAME,
    FETCH_STAGES,
    FetchContext,
    FetchResult,
    cache_dir_for,
    count_java_files,
    decompile_stage,
    download_stage,
    prune_empty_dirs,
    read_checkpoint,
    run_fetch,
    run_fetch_stage,
    write_checkpoint,
)
from mc_librarian.fetch.tools import (
    Decompiler,
    Downloader,
    HttpDownloader,
    PipelineTools,
    Remapper,
    SpecialSourceRemapper,
    VineflowerDecompiler,
    default_tools,
)

__all__ = [
    "CHECKPOINT_FILE_NAME",
    "FETCH_STAGES",
    "MANIFEST_URL",
    "Decompiler",
    "Downloader",
    "FetchContext",
    "FetchError",
    "FetchResult",
    "HttpDownloader",
    "HttpManifestResolver",
    "ManifestResolver",
    "PipelineTools",
    "Remapper",
    "SpecialSourceRemapper",
    "VersionDownloads",
    "VineflowerDecompiler",
    "cache_dir_for",
    "count_java_files",
    "decompile_stage",
    "default_tools",
    "download_stage",
    "prune_empty_dirs",
    "read_checkpoint",
    "run_fetch",
    "run_fetch_stage",
    "write_checkpoint",
]
