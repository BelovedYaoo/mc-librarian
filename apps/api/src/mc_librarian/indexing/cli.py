"""`mc-librarian index --version <v> [--source-dir <dir>] [--sources <kind>=<dir> ...] [--force]`：
端到端产出可检索索引（#13）。

与 `python -m mc_librarian index ...` 是同一个入口（见 `__main__.py` 的 `cli`）。
流水线本身不在这里：命令只负责建任务、跑完、汇总，执行复用 `JobRunner` 与 checkpoint。

- 不给 `--source-dir` / `--sources`：完整五段 `download → decompile → parse → summarize`
  `→ finalize`；
  jar 已在缓存里（`cache/<version>/fetch.json` 记着 `download` 完成）时不联网重下，源码树已
  反编译完成时跳过 `decompile`（两个标记都与 `mc-librarian fetch` 共用）；
- 给了 `--source-dir`（单目录，等价于 `--sources patched=<dir>`）或 `--sources`（多来源，
  issue #24）：跳过 download/decompile，从 `parse` 开始；
- 不给 `--force` 且 `db/<version>.sqlite` 已是合法索引：跳过重建（复用已有索引）后继续收尾，
  因此重复执行同一命令不会改动已有产物；
- 跑完由 `_finish` 登记版本行（`db_path` 指向刚产出的索引），服务不需要再手工注册；
- `summarize` 调 LLM 生成中文摘要（#7）：没配 `MC_LIBRARIAN_LLM_*` 时该阶段明确失败，
  不静默跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from mc_librarian.config import Settings, get_settings
from mc_librarian.db import Database, transaction
from mc_librarian.indexing import memory
from mc_librarian.indexing.build import read_index_totals
from mc_librarian.indexing.layout import index_path
from mc_librarian.jobs import store
from mc_librarian.jobs.runner import (
    REUSE_MESSAGES,
    JobRunner,
    StageError,
    reuse_stage_stats,
    stages_for,
)
from mc_librarian.schemas import VERSION_PATTERN, Job, LogLevel, SourceKind

# `--sources <kind>=<dir>` 允许的来源类型（issue #24 规格 §3）。
SOURCE_KINDS: Final[tuple[SourceKind, ...]] = ("patched", "loader", "patch")

# `psutil` 是 dev 依赖：装不上（生产不装）时跳过内存峰值测量，流水线照跑。
PSUTIL_MISSING_MESSAGE: Final = "psutil 不可用，跳过内存峰值测量（pip install psutil）"
# 被 Ctrl-C / 未预期异常中断时的失败原因：CLI 不跑服务启动时的恢复，得自己收尾。
INTERRUPTED_ERROR: Final = "命令被中断（Ctrl-C 或未预期异常），任务已标记为失败"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-librarian index",
        description="把某个 Minecraft 版本变成可直接检索的索引库（db/<version>.sqlite）。",
    )
    parser.add_argument("--version", required=True, help="版本号，例如 26.3 或 1.20.1")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="已就绪的源码树（相对路径按当前目录解析）；给了就跳过 download/decompile",
    )
    parser.add_argument(
        "--sources",
        action="append",
        default=None,
        metavar="KIND=DIR",
        help=(
            "多来源索引（可重复，issue #24）：patched / loader / patch；"
            "例如 --sources patched=<打过补丁的 MC> --sources loader=<加载器自身> "
            "--sources patch=<补丁目录>"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使索引已存在也重建（默认复用已有索引）",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 版本号会进文件路径（`db/<version>.sqlite`、`cache/<version>/`），带 `/` 会跑出数据根。
    if re.fullmatch(VERSION_PATTERN, args.version) is None:
        print(
            f"版本号不合法：{args.version}（只允许字母、数字、点、下划线、连字符）",
            file=sys.stderr,
        )
        return 2

    source_dir = None if args.source_dir is None else args.source_dir.expanduser().resolve()

    if source_dir is not None and not source_dir.is_dir():
        print(f"源码目录不存在或不是目录：{source_dir}", file=sys.stderr)
        return 2

    try:
        sources = parse_sources(args.sources)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    if sources and source_dir is not None:
        print(
            "--source-dir 与 --sources 不能同时给：--source-dir 等价于 --sources patched=<dir>",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    database = Database.for_data_dir(settings.data_dir)
    database.initialize()

    options = build_options(source_dir, sources, force=args.force)

    with database.connect() as conn, transaction(conn):
        job = store.create(
            conn, kind="index", version=args.version, options=options, created_by=None
        )

    # 提前告知哪些阶段会复用已有产物（规格补充 §2）；实际跳过由 `JobRunner` 按同一判据执行。
    try:
        for stage in stages_for(options):
            if reuse_stage_stats(stage, options, settings.data_dir, args.version) is not None:
                print(REUSE_MESSAGES[stage])
    except BaseException:
        # 建完 job 后、`asyncio.run` 之前被 Ctrl-C：任务还没被 runner 认领，得在这里收尾，
        # 否则会留下一条永久 `queued` 的行（`run_job` 的收尾只覆盖它自己的窗口）。
        _fail_job(database, job.id)
        raise

    return asyncio.run(run_job(settings, database, job))


def parse_sources(raw: list[str] | None) -> list[tuple[SourceKind, Path]]:
    """解析 `--sources <kind>=<dir>`（可重复）；不合法时抛 `ValueError`（CLI 翻成退出码 2）。

    目录不存在也当不合法：把不存在的路径交给 `parse` 阶段只会在任务里失败，不如在命令行直接说清。
    """
    parsed: list[tuple[SourceKind, Path]] = []
    seen: set[str] = set()

    for item in raw or []:
        kind, separator, directory = item.partition("=")

        if not separator or not directory:
            raise ValueError(f"--sources 需要 <kind>=<dir> 形式：{item}")

        if kind not in SOURCE_KINDS:
            raise ValueError(
                f"--sources 的 kind 只能是 {'/'.join(SOURCE_KINDS)}：{kind}"
            )

        if kind in seen:
            raise ValueError(f"--sources 的 kind 不能重复：{kind}")

        seen.add(kind)
        path = Path(directory).expanduser().resolve()

        if not path.is_dir():
            raise ValueError(f"--sources 的目录不存在或不是目录（{kind}）：{path}")

        parsed.append((kind, path))

    return parsed


def build_options(
    source_dir: Path | None,
    sources: list[tuple[SourceKind, Path]] | None = None,
    *,
    force: bool,
) -> dict[str, Any]:
    """任务参数。

    - `force`：`--force` 时重建，`reuse_download_stats`/`reuse_source_tree`/`reuse_index_stats`
      都据此绕过复用；
    - `reuse_index`：CLI 的索引幂等语义（未给 `--force` 时复用已有索引）。API 任务不设这个键，
      因此「提交即重建」的既有语义不变；
    - `source_dir` / `sources`：已被 `main` 转成绝对路径，覆盖 `JobContext` 的「相对数据根解析」。
    """
    options: dict[str, Any] = {"force": force, "reuse_index": not force}

    if source_dir is not None:
        options["source_dir"] = str(source_dir)

    if sources:
        options["sources"] = [
            {"kind": kind, "dir": str(path)} for kind, path in sources
        ]

    return options


async def run_job(settings: Settings, database: Database, job: Job) -> int:
    """跑完 `job` 并汇总；返回进程退出码（成功 0，任务失败 1）。"""
    started = time.monotonic()
    sampler = memory.MemorySampler()

    if memory.available():
        sampler.start()
    else:
        # 同时写进任务日志：只在 stderr 上说的话，UI 里看到的是一个没有内存峰值的任务。
        print(PSUTIL_MISSING_MESSAGE, file=sys.stderr)
        _append_log(database, job.id, "warn", PSUTIL_MISSING_MESSAGE)

    runner = JobRunner(settings, database)

    try:
        await runner.run_job(job.id)
    except StageError as error:
        print(f"index 失败：{error}", file=sys.stderr)
        return 1
    except BaseException:
        # Ctrl-C（含取消协程的 `CancelledError`）或未预期异常：CLI 不走服务启动时的
        # `_recover_interrupted`，不在这里收尾的话任务会永久停在 `running`，之后同版本的
        # 任务会被 `has_active_job` 挡住。标记失败后原样抛出，退出码与 traceback 不变。
        _fail_job(database, job.id)
        print(INTERRUPTED_ERROR, file=sys.stderr)
        raise
    finally:
        # 反编译的 java 子进程已经退出，峰值就此定下；先停采样再汇总。
        peak_bytes = sampler.stop()

    elapsed_s = time.monotonic() - started
    version = job.version or ""

    if memory.available():
        _append_log(database, job.id, "info", f"内存峰值：{memory.format_bytes(peak_bytes)}")

    finished = _find_job(database, job.id)

    if finished is None or finished.status != "succeeded":
        error_message = None if finished is None else finished.error
        fallback = None if finished is None else finished.message
        print(f"index 失败：{error_message or fallback or '任务未进入终态'}", file=sys.stderr)
        return 1

    _print_summary(
        settings,
        version,
        elapsed_s,
        peak_bytes if memory.available() else None,
        _find_checkpoint(database, job.id),
    )

    return 0


def _find_job(database: Database, job_id: str) -> Job | None:
    with database.connect() as conn:
        return store.find(conn, job_id)


def _find_checkpoint(database: Database, job_id: str) -> dict[str, Any]:
    """任务的阶段产物（`java_files`/`parsed_files`/`skipped_files` 等）。"""
    with database.connect() as conn:
        row = store.find_row(conn, job_id)

    return {} if row is None else store.checkpoint_of(row)


def _fail_job(database: Database, job_id: str) -> None:
    """被中断的任务写失败终态；带状态守卫，不覆盖已写入的终态。

    覆盖两个窗口：任务已被 runner 认领（`running`），以及刚建好、还没被认领（`queued`）——
    后者是 Ctrl-C 打在 `JobRunner.run_job` 认领之前时留下的行，不处理就会永久停在 `queued`，
    之后同版本的任务都被 `has_active_job` 挡住。

    先写终态、确认真的写进去了才追加日志（与 `JobRunner._fail` 同一取舍）：
    已 `succeeded` 的任务不该多出一条与状态不符的 error 日志。
    """
    with database.connect() as conn, transaction(conn):
        updated = False

        for status in ("running", "queued"):
            written = store.finish(
                conn,
                job_id,
                status="failed",
                message="任务失败",
                error=INTERRUPTED_ERROR,
                expected_status=status,
            )
            updated = updated or bool(written)

        if updated:
            store.append_log(conn, job_id, level="error", message=INTERRUPTED_ERROR)


def _append_log(database: Database, job_id: str, level: LogLevel, message: str) -> None:
    with database.connect() as conn, transaction(conn):
        store.append_log(conn, job_id, level=level, message=message)


def _print_summary(
    settings: Settings,
    version: str,
    elapsed_s: float,
    peak_bytes: int | None,
    checkpoint: Mapping[str, Any],
) -> None:
    """把实测数据打到 stdout：文件数、行数、token、内存峰值、耗时（规格补充 §4）。

    「源文件」取反编译阶段的 `.java` 计数，「索引文件」/「行数」/「token」取索引库里的
    真实内容（单文件解析失败会被跳过，两者不一定相等）。
    """
    path = index_path(settings.data_dir, version)
    totals = read_index_totals(path)
    decompiled = _checkpoint_count(checkpoint, "java_files")
    parsed = _checkpoint_count(checkpoint, "parsed_files")

    print(f"版本 {version}：索引完成")
    print(f"索引：{path}")

    if decompiled is not None:
        # `parse` 被复用时 checkpoint 里没有解析计数，不拿 0 充数。
        detail = (
            ""
            if parsed is None
            else f"（解析 {parsed}、跳过 {_checkpoint_count(checkpoint, 'skipped_files') or 0}）"
        )
        print(f".java 源文件：{decompiled}{detail}")

    if totals is not None:
        print(f"索引文件：{totals.files}")
        print(f"行数：{totals.lines}")
        print(f"token：{totals.tokens}（cl100k 口径）")

    if peak_bytes is not None:
        print(f"内存峰值：{memory.format_bytes(peak_bytes)}")

    print(f"耗时：{format_duration(elapsed_s)}")


def _checkpoint_count(checkpoint: Mapping[str, Any], key: str) -> int | None:
    """从 checkpoint 取整数计数；缺失/类型不对返回 `None`。"""
    value = checkpoint.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        return None

    return value


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}小时{minutes}分{secs}秒"

    if minutes:
        return f"{minutes}分{secs}秒"

    return f"{secs}秒"
