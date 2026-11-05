"""`JobRunner`：随 FastAPI lifespan 启动、并发 1 消费 `jobs` 队列的 asyncio 任务。

设计（见 `docs/00-overview.md` §4、D13）：

- 进程内单任务串行消费，没有独立 worker；一次只跑一个任务（并发 1）。
- 阶段序列由 `options.source_dir` 决定：给了源目录走 `#4` 的 `parse → summarize → finalize`；
  没给则从官方 jar 起跑完整五段 `download → decompile → parse → summarize → finalize`
  （`#21` 的下载+反编译产物就是 `parse` 消费的源码树）。
  每完成一个阶段就把阶段名与产物写进 `jobs.checkpoint`；认领任务时跳过已完成的阶段，
  失败后重排即可续跑。
- `cancel` 只在**阶段边界**生效：阶段内部不中断，跑完当前阶段后检查标志再收尾；终态写入
  与取消标志在同一条带条件的 UPDATE 里判定，finalize 期间到达的取消也不会被吞掉。
- 启动时把上一次进程遗留的 `running` 任务收回 `queued`（已请求取消的直接置 `canceled`），
  否则它们永远不会再被认领。
- 阻塞的文件扫描与 sqlite 读写都丢进 `asyncio.to_thread`，不占事件循环。
- `options.reuse_index` 为真且 `db/<version>.sqlite` 已是合法索引时，跳过 `parse`（不重建），
  直接复用已有索引的统计：`mc-librarian index` 未给 `--force` 时的幂等语义（见
  `reuse_index_stats`）。
- `cache/<version>/fetch.json`（与 `mc-librarian fetch` 共用）记着 `download` 已完成、jar 还在
  缓存里（需要 remap 的版本还要 mappings 也还在）、又没给 `--force` 时，跳过 `download`（不再
  解析清单、不发 HTTP，见 `reuse_download_stats`）；记着 `decompile` 已完成且源码树还在时，
  跳过 `decompile`（见 `reuse_source_tree`）；跑完 `download`/`decompile` 会把完成状态写回该
  文件（见 `persist_fetch_stage`）。
- `run_job(job_id)` 是 CLI 的入口：只认领并跑完指定的那一个任务，不走常驻队列循环。

`#4`/`#5` 实现 `parse`：就位源码树、tree-sitter 解析、原子写出 `db/<version>.sqlite`；
`#21` 补上 `download`/`decompile` 两段（实现在 `mc_librarian.fetch.pipeline`，这里只做编排）；
`#7` 把 `summarize` 接上 LLM（实现在 `mc_librarian.indexing.summarize`）：按类内容哈希幂等、
按 package 分批并发生成中文摘要；`finalize` 仍是收尾占位。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from mc_librarian import audit, settings_store
from mc_librarian.config import LlmConfigError, Settings, resolve_llm_config
from mc_librarian.db import Database, transaction
from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.pipeline import (
    CHECKPOINT_FILE_NAME,
    CLIENT_JAR_NAME,
    FETCH_STAGES,
    FetchContext,
    cache_dir_for,
    count_java_files,
    mappings_path_of,
    read_checkpoint,
    run_fetch_stage,
    write_checkpoint,
)
from mc_librarian.fetch.tools import PipelineTools, default_tools
from mc_librarian.indexing.build import (
    BuildError,
    SourceSpec,
    build_index_from_sources,
    place_source_tree,
    read_index_totals,
    source_tree_size_bytes,
)
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.indexing.summarize import (
    FAILURE_SAMPLE,
    OpenAiCompatClient,
    SummaryClient,
    SummaryError,
    export_summaries,
    restore_summaries,
    summarize_index,
)
from mc_librarian.jobs import store
from mc_librarian.schemas import LogLevel

logger = logging.getLogger("mc_librarian.jobs")

# 全部阶段的规范顺序；`stages_for` 按任务参数选子集。
# 给了 `options.source_dir` 的任务从 `parse` 开始（`SOURCE_STAGES`）；不给则从官方 jar 起跑
# 完整五段（`STAGES`）。
STAGES: Final[tuple[str, ...]] = ("download", "decompile", "parse", "summarize", "finalize")
SOURCE_STAGES: Final[tuple[str, ...]] = STAGES[2:]
# 收尾阶段，取消时用来描述停在哪（五段全跑时）。
FINAL_STAGE: Final = "finalize"

# 停止 runner 时最多等一个任务收尾多久，超时就取消 asyncio 任务。
STOP_TIMEOUT_S: Final = 5.0

# 闸门关闭时给遗留排队任务的失败原因（与 `POST /v1/admin/jobs` 的 403 detail 同义）。
INDEX_DISABLED_ERROR: Final = (
    "本实例已关闭索引构建（MC_LIBRARIAN_INDEX_ENABLED=false）："
    "请在本地工作站生成索引后用 POST /v1/admin/versions 注册"
)

# 索引已存在且未要求重建时，写进任务日志与命令输出的提示（规格补充 §2）。
REUSE_INDEX_MESSAGE: Final = "索引已存在，跳过重建（用 --force 重建）"
# 源码树已反编译完成时的同类提示。
REUSE_SOURCE_TREE_MESSAGE: Final = "源码树已存在，跳过反编译（用 --force 重建）"
# jar 已在缓存里、`download` 无需再解析清单（联网）时的同类提示。
REUSE_DOWNLOAD_MESSAGE: Final = "jar 已在缓存里，跳过联网（用 --force 重下）"
REUSE_MESSAGES: Final[dict[str, str]] = {
    "download": REUSE_DOWNLOAD_MESSAGE,
    "parse": REUSE_INDEX_MESSAGE,
    "decompile": REUSE_SOURCE_TREE_MESSAGE,
}


class StageError(RuntimeError):
    """阶段内部可预期的失败；消息会进任务日志与 `error` 字段。"""


@dataclass(frozen=True)
class JobContext:
    """一个阶段的输入：任务版本 + 参数 + 数据根 + 日志回调 + checkpoint/工具链/运行时配置。

    `checkpoint` 与 `tools` 供 `download`/`decompile` 两段使用；`settings` 供 `summarize` 读
    LLM 配置与分批参数（`#11` 之后是 `JobRunner` 在任务开始时解析出的**有效配置**：
    数据库覆盖 + 环境配置）；`parse` 只需要 `version`/`options`/`data_dir`/`log`，
    parse-only 任务不传 `tools`。
    """

    version: str
    options: dict[str, Any]
    data_dir: Path
    log: Callable[[LogLevel, str], None]
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    tools: PipelineTools | None = None
    settings: Settings | None = None

    @property
    def source_dir(self) -> Path | None:
        """`options.source_dir` 解析出的绝对路径；相对路径按数据根解析。"""
        raw = self.options.get("source_dir")

        if not isinstance(raw, str) or not raw:
            return None

        path = Path(raw).expanduser()

        return path if path.is_absolute() else (self.data_dir / path)

    @property
    def sources(self) -> list[SourceSpec]:
        """任务要索引的来源（issue #24 规格 §3）：`options.sources`，否则 `source_dir` 单来源。

        `source_dir` 等价于 `[{kind: "patched", dir: source_dir}]`（向后兼容）；两者都没有时返回
        空列表，由 `_parse_stage` 回退到 `decompile` 就位好的源码根。相对路径按数据根解析。
        """
        raw = self.options.get("sources")

        if isinstance(raw, list):
            specs: list[SourceSpec] = []

            for item in raw:
                if not isinstance(item, dict):
                    continue

                kind = item.get("kind")
                directory = item.get("dir")

                if kind not in ("patched", "loader", "patch") or not isinstance(directory, str):
                    continue

                path = Path(directory).expanduser()
                specs.append(
                    SourceSpec(
                        kind=kind,
                        root=path if path.is_absolute() else (self.data_dir / path),
                    )
                )

            if specs:
                return specs

        single = self.source_dir

        return [] if single is None else [SourceSpec(kind="patched", root=single)]


def stages_for(options: Mapping[str, Any]) -> tuple[str, ...]:
    """按任务参数选阶段序列：给了源目录 / 多来源走索引三段，否则从官方 jar 起跑完整五段。"""
    if options.get("source_dir") or options.get("sources"):
        return SOURCE_STAGES

    return STAGES


def resolve_obfuscated(options: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> bool:
    """任务产出登记版本时决定 `obfuscated`（该版本的源码是否经官方映射 remap）。

    `options.obfuscated` 显式声明优先；未声明时跑过 `download` 的用 checkpoint 里的真值
    （等于 `downloads.needs_remap`），parse-only 任务缺省按 `True` 记录：它的典型来源是本地
    `fetch` 出来、**已经 remap 过**的源码树，未 remap 才是少数。26.x 这类本来就不混淆的版本
    请显式传 `false`。
    """
    declared = options.get("obfuscated")

    if isinstance(declared, bool):
        return declared

    return bool(checkpoint.get("obfuscated", True))


def reuse_index_stats(
    options: Mapping[str, Any], data_dir: Path, version: str
) -> dict[str, Any] | None:
    """索引已存在且未要求重建时，返回复用索引要并入 checkpoint 的统计；否则 `None`。

    `options.reuse_index` 为真才复用——只有 `mc-librarian index` 会设置它（未给 `--force`
    时）；API 任务不设，保持「提交即重建」的既有语义。索引文件存在但不是合法索引
    （打不开/缺 `files` 表）时返回 `None`，交回正常 `parse` 重建，不把坏文件登记成版本。
    """
    if options.get("reuse_index") is not True:
        return None

    totals = read_index_totals(index_path(data_dir, version))

    if totals is None:
        return None

    return {
        "files": totals.files,
        "tokens": totals.tokens,
        # 与 `parse` 同口径（索引范围内的源码字节数），不拿索引 sqlite 的大小充数。
        "source_size_bytes": source_tree_size_bytes(source_root(data_dir, version)),
    }


def done_stages(checkpoint: Mapping[str, Any]) -> set[str]:
    """checkpoint 里已完成的阶段名；脏数据（`stages` 不是列表）按「没有已完成阶段」处理。"""
    raw = checkpoint.get("stages")

    if not isinstance(raw, list):
        return set()

    return {str(name) for name in raw}


def fetch_checkpoint_path(data_dir: Path, version: str) -> Path:
    """`<data_dir>/cache/<version>/fetch.json`：`download`/`decompile` 的完成状态。"""
    return cache_dir_for(data_dir, version) / CHECKPOINT_FILE_NAME


def reuse_source_tree(
    options: Mapping[str, Any], data_dir: Path, version: str
) -> dict[str, Any] | None:
    """`decompile` 已完成且源码树还在时，返回复用要并入 checkpoint 的统计；否则 `None`。

    完成状态以 `<data_dir>/cache/<version>/fetch.json` 为准——`mc-librarian fetch` 与
    `JobRunner` 写的是同一份（见 `persist_fetch_stage`），两个入口因此共用同一个「源码树已就绪」
    的事实来源。刻意**不**用「目录里有 `.java` 就算完成」这种弱判据：被强杀的半成品树会被静默
    复用、产出残缺索引。`options.force` 为真时一律重建；标记为真但源码树缺失/为空时也重跑。

    `java_files`/`java_bytes` 取标记里记录的值（那是「这次反编译已完成」的权威产物大小），
    标记里缺这两项时才退回数目录。
    """
    if options.get("force") is True:
        return None

    persisted = read_checkpoint(fetch_checkpoint_path(data_dir, version))

    if "decompile" not in done_stages(persisted):
        return None

    source_dir = source_root(data_dir, version)
    files, size_bytes = count_java_files(source_dir)

    if files == 0:
        return None

    return {
        "source_dir": str(source_dir),
        "java_files": checkpoint_int(persisted, "java_files") or files,
        "java_bytes": checkpoint_int(persisted, "java_bytes") or size_bytes,
    }


def reuse_download_stats(
    options: Mapping[str, Any], data_dir: Path, version: str
) -> dict[str, Any] | None:
    """`download` 已完成且 jar 还在缓存里时，返回复用要并入 checkpoint 的产物；否则 `None`。

    判据与 `reuse_source_tree` 同源（`<data_dir>/cache/<version>/fetch.json`），再加 jar 存在性。
    满足时**不再调 `manifest.resolve`**，所以断网也能重跑「源码树与索引都已在」的版本——
    以前未给 `--source-dir` 的重复执行会在 `download` 阶段发 HTTP，断网直接 exit 1。
    `options.force` 为真一律重跑（`--force` 的语义是「重下重编」）。

    标记里缺 `jar_path`/`obfuscated`（`decompile` 与版本登记都要用）、jar 已被删，或记着用过
    官方 mappings 而那份 mappings 已不在时，也重跑：半份标记不能当完成证据（与
    `reuse_source_tree` 不用「目录里有 `.java`」同一取舍）。重跑后 mappings 能重新取到就继续，
    取不到由 `download_stage` 明确报错——绝不退回拿混淆 jar 反编译（#14 §3）。
    """
    if options.get("force") is True:
        return None

    persisted = read_checkpoint(fetch_checkpoint_path(data_dir, version))

    if "download" not in done_stages(persisted):
        return None

    raw_jar = persisted.get("jar_path")
    obfuscated = persisted.get("obfuscated")
    client_jar = cache_dir_for(data_dir, version) / CLIENT_JAR_NAME

    if not isinstance(raw_jar, str) or not raw_jar:
        return None

    if not isinstance(obfuscated, bool) or not client_jar.is_file():
        return None

    jar = Path(raw_jar)

    if not jar.is_file():
        return None

    mappings = mappings_path_of(persisted)

    if mappings is not None and not mappings.is_file():
        return None

    return {
        "client_jar_path": str(client_jar),
        "client_jar_bytes": client_jar.stat().st_size,
        "jar_path": str(jar),
        "jar_bytes": jar.stat().st_size,
        "mappings_path": persisted.get("mappings_path"),
        "remapped_jar_path": persisted.get("remapped_jar_path"),
        "obfuscated": obfuscated,
    }


def reuse_stage_stats(
    stage: str, options: Mapping[str, Any], data_dir: Path, version: str
) -> dict[str, Any] | None:
    """该阶段能否复用已有产物；能就返回要并入 checkpoint 的统计，否则 `None`（规格补充 §2）。

    - `parse`：`options.reuse_index` 为真且 `db/<version>.sqlite` 已是合法索引——只有
      `mc-librarian index` 会设这个键（API 任务不设，保持「提交即重建」的既有语义）；
    - `download`：按版本的 fetch checkpoint 记着该阶段已完成、jar 还在缓存里，且没给
      `--force`——跳过联网解析清单；
    - `decompile`：同上，标记记着该阶段已完成且源码树还在（API 任务同样受益）。
    """
    if stage == "parse":
        return reuse_index_stats(options, data_dir, version)

    if stage == "download":
        return reuse_download_stats(options, data_dir, version)

    if stage == "decompile":
        return reuse_source_tree(options, data_dir, version)

    return None


def persist_fetch_stage(cache_dir: Path, stage: str, stats: Mapping[str, Any]) -> None:
    """把完成的 fetch 阶段并进 `<cache>/fetch.json`（与 `run_fetch` 同一份格式）。

    `JobRunner` 只跑单段（`run_fetch_stage`），因此这一步得自己做：否则只有 `mc-librarian
    fetch` 跑过的版本才留有完成标记，`index` 自己跑的版本第二次还会重新反编译。
    """
    path = cache_dir / CHECKPOINT_FILE_NAME
    persisted = read_checkpoint(path)
    done = done_stages(persisted)
    done.add(stage)

    write_checkpoint(
        path,
        {
            **persisted,
            "stages": [name for name in FETCH_STAGES if name in done],
            **stats,
        },
    )


def checkpoint_int(checkpoint: Mapping[str, Any], key: str) -> int:
    """从 checkpoint 取整数统计（`files`/`tokens`/`source_size_bytes`）。

    checkpoint 是阶段写入的 JSON，正常是 int；旧版本残留或外部改动可能给出别的类型或
    缺失键。非 int 一律退回 0，而不是 `int(...)` 直接抛——那个异常会把整个 `_finish` 事务
    回滚，把任务留在 `running`。
    """
    value = checkpoint.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        return 0

    return value


def run_stage(stage: str, context: JobContext) -> dict[str, Any]:
    """执行一个阶段，返回要并入 checkpoint 的产物。

    `parse` 由 `mc_librarian.indexing` 实现（就位源码树 → 解析 → 写 `db/<version>.sqlite`）；
    `download`/`decompile` 交给 `mc_librarian.fetch.pipeline`；`summarize` 调 LLM 生成中文摘要
    （`mc_librarian.indexing.summarize`）；`finalize` 待后续 ticket，这里只标记完成。
    未知阶段直接报错，而不是静默返回空产物——否则新增阶段时漏改会产出残缺索引。
    """
    if stage == "parse":
        return _parse_stage(context)

    if stage in FETCH_STAGES:
        return _fetch_stage(stage, context)

    if stage == "summarize":
        return _summarize_stage(context)

    if stage == "finalize":
        return {}

    raise StageError(f"未知阶段：{stage}")


def _fetch_stage(stage: str, context: JobContext) -> dict[str, Any]:
    """`download` / `decompile` 两段：转调 fetch 流水线，失败统一转成 `StageError`。

    跑完把阶段与产物写进**按版本**的 `<data_dir>/cache/<version>/fetch.json`（`mc-librarian
    fetch` 写的是同一份）：两个入口共用同一个「源码树已就绪」的事实来源，谁先跑完另一个都能跳过
    （见 `reuse_source_tree`）。
    """
    if context.tools is None:
        raise StageError(f"{stage} 阶段缺少工具链（PipelineTools）")

    cache_dir = cache_dir_for(context.data_dir, context.version)
    fetch_context = FetchContext(
        version=context.version,
        cache_dir=cache_dir,
        source_dir=source_root(context.data_dir, context.version),
        log=context.log,
        checkpoint=context.checkpoint,
    )

    try:
        stats = run_fetch_stage(stage, fetch_context, context.tools)
    except FetchError as error:
        raise StageError(str(error)) from error

    persist_fetch_stage(cache_dir, stage, stats)

    return stats


def _parse_stage(context: JobContext) -> dict[str, Any]:
    """就位源码树并把它解析成 `db/<version>.sqlite`（规格补充 §1/§3/§4/§14）。

    源码根固定为 `<data_dir>/src/<version>/`：`options.source_dir` / `options.sources` 里的
    Java 目录（`patched`/`loader`）都就位到这里（相对路径不变，`files.path` 据此可读）；
    `patch` 目录不复制——补丁切片从 `patches.context` 直接取，不进源码树。没给来源时直接用
    `dest`——完整五段里 `decompile` 已经把源码树写在那里。

    `build_index` 是原子替换整库（#7 的 `summaries` 表也一起没了），所以重建前先把已有摘要
    搬出来、重建后按 fqn 搬回去：否则「重跑同一个版本」每次都会把摘要清掉重生成，内容哈希
    幂等就白做了。类被删掉的摘要直接丢弃（不留下指向错误类的行）。
    """
    dest = source_root(context.data_dir, context.version)
    sources = context.sources or [SourceSpec(kind="patched", root=dest)]

    for source in sources:
        if not source.root.is_dir():
            raise StageError(
                f"来源目录不存在或不是目录（{source.kind}，来自 options.source_dir/sources）："
                f"{source.root}"
            )

    index = index_path(context.data_dir, context.version)
    carried = export_summaries(index)

    try:
        for source in sources:
            if source.kind == "patch":
                continue

            if source.root.resolve() != dest.resolve():
                context.log("info", f"parse：{source.kind} 源码树就位到 {dest}")

            place_source_tree(source.root, dest)

        result = build_index_from_sources(sources, index, log=context.log)
    except BuildError as error:
        raise StageError(str(error)) from error

    if carried:
        restored = restore_summaries(index, carried)
        context.log(
            "info",
            f"parse：保留 {restored}/{len(carried)} 条已有摘要（类未变时 summarize 不重新生成）",
        )

    context.log(
        "info",
        f"parse：{result.files} 个文件、约 {result.tokens} token、{result.source_size_bytes} 字节；"
        f"解析 {result.parsed} 个 .java、跳过 {result.skipped} 个；"
        f"补丁 {result.patch_files} 个文件、{result.patch_hunks} 个 hunk",
    )

    return {
        "files": result.files,
        "tokens": result.tokens,
        "source_size_bytes": result.source_size_bytes,
        "parsed_files": result.parsed,
        "skipped_files": result.skipped,
        "patch_files": result.patch_files,
        "patch_hunks": result.patch_hunks,
    }


def _summarize_stage(context: JobContext) -> dict[str, Any]:
    """生成中文摘要并写回索引库（issue #7）。

    幂等：内容哈希未变的类不重新调 LLM；因此重跑同一个版本（或重试失败任务）只重跑失败的类，
    不重复计费。类有失败时抛 `StageError`，让任务标 `failed`——但已生成的摘要已经落库，
    不会被回滚（规格 §5）。LLM 未配置同样在这里明确失败，不静默跳过（规格 §2）。
    """
    settings = context.settings

    if settings is None:
        raise StageError("summarize 阶段缺少运行时配置（Settings）")

    path = index_path(context.data_dir, context.version)

    if not path.is_file():
        raise StageError(f"索引文件不存在：{path}")

    def client_factory() -> SummaryClient:
        # 只在确有类要生成时才会被调用（见 `summarize_index`）：全量复用时不要求 LLM 配置。
        return OpenAiCompatClient(
            resolve_llm_config(settings), timeout=settings.llm_timeout_s
        )

    try:
        stats = asyncio.run(
            summarize_index(
                path,
                client_factory=client_factory,
                batch_size=settings.summary_batch_size,
                concurrency=settings.summary_concurrency,
                log=context.log,
            )
        )
    except (LlmConfigError, SummaryError) as error:
        raise StageError(str(error)) from error
    except sqlite3.Error as error:
        # 索引库不是合法 sqlite（被外部改坏/截断）：当阶段失败，不把异常直接冒到 `_loop`。
        raise StageError(f"索引库不可读：{path}（{error}）") from error

    covered = stats.generated + stats.reused
    patch_covered = stats.patches_generated + stats.patches_reused
    context.log(
        "info",
        f"摘要：生成 {stats.generated}、复用 {stats.reused}、失败 {stats.failed}"
        f"（覆盖 {covered}/{stats.classes}）；"
        f"补丁：生成 {stats.patches_generated}、复用 {stats.patches_reused}、"
        f"失败 {stats.patches_failed}（覆盖 {patch_covered}/{stats.patches}）",
    )

    if stats.failed or stats.patches_failed:
        sample = "；".join(stats.failures[:FAILURE_SAMPLE])
        detail = (
            f"摘要失败 {stats.failed} 个类"
            if not stats.patches_failed
            else f"摘要失败：类 {stats.failed} 个、补丁 {stats.patches_failed} 个"
        )
        raise StageError(f"{detail}（已生成的摘要保留）：{sample}")

    return {
        "summaries": stats.generated,
        "summaries_reused": stats.reused,
        "summary_classes": stats.classes,
        "summary_failed": stats.failed,
        "patch_summaries": stats.patches_generated,
        "patch_summaries_reused": stats.patches_reused,
        "patch_summary_failed": stats.patches_failed,
    }


class JobRunner:
    """串行任务执行器。`start()` 起 asyncio 任务，`stop()` 等它收尾。"""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        poll_interval: float = 0.2,
        tools: PipelineTools | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._poll_interval = poll_interval
        # 下载/remap/反编译的工具链；测试注入假实现，生产每个任务按**当时**的有效配置重建
        # （`index.java_xmx` 可以在设置页改，见 `_effective_settings`）。
        self._tools = tools
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def _effective_settings(self) -> Settings:
        """数据库覆盖 + 环境配置，任务开始时实时读一次（issue #11 的「立即生效」）。

        读失败（库忙 / 主密钥换了）不阻止任务：退回进程配置继续跑，只记一条 warn。
        """
        try:
            with self._database.connect() as conn:
                return settings_store.effective_settings(
                    self._settings, settings_store.read_overrides(conn, self._settings)
                )
        except Exception:
            logger.exception("读取设置覆盖失败，任务改用进程配置")

            return self._settings

    def _tools_for(self, settings: Settings) -> PipelineTools:
        """工具链：测试注入的实例优先，否则按当前有效配置重建（`-Xmx` 取设置页的值）。"""
        return self._tools if self._tools is not None else default_tools(settings)

    async def start(self) -> None:
        if self._task is not None:
            return

        # 先收上次进程遗留的 running，再开消费循环：否则它们永远不会被认领。
        # 恢复失败（如 control 库 busy）不能拖垮启动：只记 error 日志，消费循环照常起来
        # （与 `main.backfill_version_db_paths` 的取舍一致）。
        try:
            await asyncio.to_thread(self._recover_interrupted)
        except Exception:
            logger.exception("启动恢复遗留 running 任务失败，已跳过（不影响启动）")

        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="job-runner")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None

        if task is None:
            return

        try:
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def run_job(self, job_id: str) -> None:
        """认领并跑完指定的一个已入队任务，跑完即返回（`mc-librarian index` 用它）。

        常驻服务走 `start()` 串行消费整个队列；CLI 只关心自己刚建的那一个任务，所以只认领它，
        不碰队列里的其它任务（也不会因 `_claim` 失败而空转）。执行的仍是同一套 `_execute`
        与 checkpoint 机制。任务不存在或不在 `queued` 状态时抛 `StageError`。
        """
        row = await asyncio.to_thread(self._claim_job, job_id)

        if row is None:
            raise StageError(f"任务不存在或不在排队状态：{job_id}")

        await self._execute(row)

    def _claim_job(self, job_id: str) -> sqlite3.Row | None:
        with self._database.connect() as conn, transaction(conn):
            return store.claim(conn, job_id)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            row = await asyncio.to_thread(self._claim)

            if row is None:
                await self._idle()
                continue

            try:
                await self._execute(row)
            except Exception:  # 兜底：单个任务的意外不能杀死整个 runner
                logger.exception("任务 %s 执行时出现未捕获异常", row["id"])

                try:
                    await asyncio.to_thread(
                        self._fail, str(row["id"]), "任务执行异常，请查看服务端日志"
                    )
                except Exception:
                    # 兜底自身不能再失败：异常一旦逃出 `_loop`，asyncio 任务静默死亡，
                    # 整个任务队列永久停摆且没有任何日志。
                    logger.exception("任务 %s 的兜底失败路径自身异常，已跳过", row["id"])

    async def _idle(self) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)

    async def _execute(self, row: sqlite3.Row) -> None:
        job_id = str(row["id"])
        version = str(row["version"])
        options = store.options_of(row)
        checkpoint = store.checkpoint_of(row)
        # 设置页改过的值在任务开始时生效（每个阶段用同一份快照，中途改设置不影响本次任务）。
        settings = self._effective_settings()
        tools = self._tools_for(settings)
        done = {str(item) for item in checkpoint.get("stages", [])}
        stages = stages_for(options)
        total = len(stages)
        # 当前停在的阶段（与 `jobs.stage` 列一致）；取消时用它写 `stage` 与 `message`。
        current_stage: str | None = None

        for index, stage in enumerate(stages):
            if stage in done:
                current_stage = stage
                continue

            if await asyncio.to_thread(self._is_cancel_requested, job_id):
                await asyncio.to_thread(self._cancel, job_id, current_stage)
                return

            # 已有产物可复用时不再重跑该阶段（规格补充 §2）：`decompile` 复用已就绪的源码树，
            # `parse` 复用已有索引。产物字节不变，因此重复执行同一命令是幂等的。
            reused = await asyncio.to_thread(
                reuse_stage_stats, stage, options, settings.data_dir, version
            )

            if reused is not None:
                current_stage = stage
                done.add(stage)
                checkpoint = {
                    **checkpoint,
                    "stages": [name for name in STAGES if name in done],
                    **reused,
                }
                await asyncio.to_thread(
                    self._save,
                    job_id,
                    checkpoint,
                    (index + 1) / total,
                    REUSE_MESSAGES[stage],
                )
                continue

            await asyncio.to_thread(self._mark, job_id, stage, index / total)
            current_stage = stage

            context = JobContext(
                version=version,
                options=options,
                data_dir=settings.data_dir,
                log=lambda level, message: self._log(job_id, level, message),
                checkpoint=checkpoint,
                tools=tools,
                settings=settings,
            )

            try:
                stats = await asyncio.to_thread(run_stage, stage, context)
            except StageError as error:
                await asyncio.to_thread(self._fail, job_id, str(error))
                return
            except Exception as error:  # 阶段异常统一转成失败摘要
                await asyncio.to_thread(self._fail, job_id, f"{stage} 阶段异常：{error}")
                return

            done.add(stage)
            checkpoint = {
                **checkpoint,
                "stages": [name for name in STAGES if name in done],
                **stats,
            }
            await asyncio.to_thread(
                self._save, job_id, checkpoint, (index + 1) / total, f"{stage} 完成"
            )

        await asyncio.to_thread(self._finish, job_id, version, checkpoint, options)

    # ------------------------------------------------------------------
    # 所有落库操作都开一条短连接，并在写事务里完成「写状态 + 追加日志」。
    # ------------------------------------------------------------------

    def _recover_interrupted(self) -> None:
        """启动恢复：重排遗留 `running`，已请求取消的直接置 `canceled`，各记一条日志。"""
        with self._database.connect() as conn, transaction(conn):
            requeued, canceled = store.requeue_running(conn)

            for job_id in requeued:
                store.append_log(
                    conn,
                    job_id,
                    level="info",
                    message="服务重启：任务从上次中断处重新排队",
                )

            for job_id in canceled:
                store.append_log(
                    conn,
                    job_id,
                    level="warn",
                    message="服务重启：任务在取消请求后终止",
                )

        if requeued or canceled:
            logger.info(
                "从上次中断恢复：%d 个重新排队、%d 个取消", len(requeued), len(canceled)
            )

    def _claim(self) -> sqlite3.Row | None:
        with self._database.connect() as conn, transaction(conn):
            # 闸门关闭：认领前先清掉队首排队任务并写清原因，绝不进入下载/反编译。
            if not self._settings.index_enabled:
                job_id = store.fail_next_queued(
                    conn,
                    error=INDEX_DISABLED_ERROR,
                    message="任务失败：本实例已关闭索引构建",
                )

                if job_id is not None:
                    store.append_log(
                        conn, job_id, level="error", message=INDEX_DISABLED_ERROR
                    )

                return None

            return store.claim_next(conn)

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._database.connect() as conn:
            return store.is_cancel_requested(conn, job_id)

    def _log(self, job_id: str, level: LogLevel, message: str) -> None:
        with self._database.connect() as conn, transaction(conn):
            store.append_log(conn, job_id, level=level, message=message)

    def _mark(self, job_id: str, stage: str, progress: float) -> None:
        with self._database.connect() as conn, transaction(conn):
            store.mark_stage(
                conn, job_id, stage=stage, progress=progress, message=f"{stage} 进行中"
            )
            store.append_log(conn, job_id, level="info", message=f"{stage}：开始")

    def _save(
        self,
        job_id: str,
        checkpoint: dict[str, Any],
        progress: float,
        message: str,
    ) -> None:
        with self._database.connect() as conn, transaction(conn):
            store.save_checkpoint(
                conn, job_id, checkpoint=checkpoint, progress=progress, message=message
            )
            store.append_log(conn, job_id, level="info", message=message)

    def _finish(
        self,
        job_id: str,
        version: str,
        checkpoint: dict[str, Any],
        options: Mapping[str, Any],
    ) -> None:
        """全部阶段完成：终态与取消标志在同一条 UPDATE 里判定，再按实际终态记账。

        `succeeded` 时登记版本行：**仅当版本未注册时插入一行**，已注册版本不动；
        真的插入了才写 `version.recorded` 审计。`canceled` 时不碰版本表。这样 finalize
        执行期间到达的取消不会被吞掉。只有跑过 `parse`（索引阶段）的任务才登记：
        没跑 `parse` 的任务产出的是源码树，不是可检索索引。登记时把索引库路径、是否 remap、
        源码字节数一并写入——否则 `/v1/versions` 会列出该版本、`_require_index` 却因
        `db_path` 为空回 404「尚无索引文件」，状态自相矛盾。

        终态描述按 checkpoint 里实际完成的阶段生成：跑了 `parse` 才说「索引完成」，
        否则只说源码树就绪。取消描述用最后一个已完成阶段，而不是写死的 `finalize`。

        跑过 `parse` 但索引库在收尾前消失（被外部删除/清理）时，走正常失败路径写
        `failed` + 「索引文件缺失：<路径>」；**不能**在写事务里抛异常回滚——那会把任务永久
        留在 `running`（`claim_next` 只认 `queued`，本进程内再也不会重跑）。
        """
        completed = [str(name) for name in checkpoint.get("stages", [])]
        final_stage = completed[-1] if completed else FINAL_STAGE
        indexed = "parse" in completed

        if indexed:
            index_file = index_path(self._settings.data_dir, version)

            if not index_file.is_file():
                self._fail(job_id, f"索引文件缺失：{index_file}")
                return

        with self._database.connect() as conn, transaction(conn):
            status = store.finish_succeeded_or_canceled(
                conn,
                job_id,
                succeeded_message="索引完成" if indexed else "源码树就绪",
                canceled_message=f"已取消（停在 {final_stage}）",
            )

            if status == "succeeded":
                if indexed:
                    inserted = store.insert_version_if_absent(
                        conn,
                        version=version,
                        files=checkpoint_int(checkpoint, "files"),
                        tokens=checkpoint_int(checkpoint, "tokens"),
                        # `options.obfuscated` 优先；未声明时跑过 `download` 用 checkpoint 的真值，
                        # parse-only 缺省按「已 remap」记录（见 `resolve_obfuscated`）。
                        obfuscated=resolve_obfuscated(options, checkpoint),
                        source_size_bytes=checkpoint_int(checkpoint, "source_size_bytes"),
                        # 跑过 `summarize` 才可能真的产出了摘要（#7）。
                        has_summaries="summarize" in completed,
                        db_path=str(index_file),
                    )

                    if inserted is not None:
                        audit.record(
                            conn,
                            actor_id=None,
                            action=audit.ACTION_VERSION_RECORDED,
                            target=inserted.version,
                            detail={"files": inserted.files, "tokens": inserted.tokens},
                        )

                store.append_log(conn, job_id, level="info", message="任务完成")
            elif status == "canceled":
                store.append_log(
                    conn, job_id, level="warn", message=f"在 {final_stage} 阶段边界取消"
                )

    def _fail(self, job_id: str, error: str) -> None:
        """兜底失败：先带状态守卫写终态，确认真的写进去了才追加 error 日志。

        已 succeeded 的任务不会被改回 failed（`expected_status` 守卫），也不该多出一条
        与状态不符的 error 日志。
        """
        with self._database.connect() as conn, transaction(conn):
            updated = store.finish(
                conn,
                job_id,
                status="failed",
                message="任务失败",
                error=error,
                expected_status="running",
            )

            if updated:
                store.append_log(conn, job_id, level="error", message=error)

    def _cancel(self, job_id: str, stage: str | None) -> None:
        """在阶段边界取消。

        `stage` 是任务**当前停在**的阶段（与 `jobs.stage` 列一致），不是即将开始的下一段，
        这样 `message` 与 `stage` 不会互相矛盾。重排会清空 `stage`，所以一并把它补写成
        `stage`（列已是 NULL 时才写），否则重排后立即取消会留下「`message` 说停在 parse、
        `stage` 列是 NULL」。先带守卫写终态，写进去了才记日志。
        """
        if stage is None:
            message = "已取消（尚未开始阶段）"
            log_message = "任务在开始阶段前被取消"
        else:
            message = f"已取消（停在 {stage}）"
            log_message = f"在 {stage} 阶段边界取消"

        with self._database.connect() as conn, transaction(conn):
            updated = store.finish(
                conn,
                job_id,
                status="canceled",
                message=message,
                expected_status="running",
                stage=stage,
            )

            if updated:
                store.append_log(conn, job_id, level="warn", message=log_message)
