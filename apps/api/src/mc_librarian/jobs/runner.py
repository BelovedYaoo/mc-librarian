"""`JobRunner`：随 FastAPI lifespan 启动、并发 1 消费 `jobs` 队列的 asyncio 任务。

设计（见 `docs/00-overview.md` §4、D13）：

- 进程内单任务串行消费，没有独立 worker；一次只跑一个任务（并发 1）。
- 阶段固定为 `download → decompile → parse → summarize → finalize`，每完成一个阶段就把
  阶段名与产物写进 `jobs.checkpoint`；认领任务时跳过已完成的阶段，失败后重排即可续跑。
- `cancel` 只在**阶段边界**生效：阶段内部不中断，跑完当前阶段后检查标志再收尾。
- 启动时把上一次进程遗留的 `running` 任务收回 `queued`，否则它们永远不会再被认领。
- 阻塞的文件扫描与 sqlite 读写都丢进 `asyncio.to_thread`，不占事件循环。

#4 只真正实现 `parse`（统计文件数与 token）；其余阶段是占位，见 #13。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mc_librarian.config import Settings
from mc_librarian.db import Database, transaction
from mc_librarian.jobs import store
from mc_librarian.schemas import LogLevel

logger = logging.getLogger("mc_librarian.jobs")

# 阶段顺序即进度分母；`checkpoint.stages` 记已完成的阶段名。
STAGES: Final[tuple[str, ...]] = ("download", "decompile", "parse", "summarize", "finalize")

# token 估算：不引入 tiktoken 依赖，按「字符数 ÷ 3.5」近似 cl100k 词元数（#13 可换真分词）。
CHARS_PER_TOKEN: Final = 3.5

JAVA_SUFFIX: Final = ".java"
DATA_DIR_NAME: Final = "data"

# 停止 runner 时最多等一个任务收尾多久，超时就取消 asyncio 任务。
STOP_TIMEOUT_S: Final = 5.0


class StageError(RuntimeError):
    """阶段内部可预期的失败；消息会进任务日志与 `error` 字段。"""


@dataclass(frozen=True)
class JobContext:
    """一个阶段的输入：任务参数 + 已完成的 checkpoint + 日志回调。"""

    job_id: str
    kind: str
    version: str
    options: dict[str, Any]
    checkpoint: dict[str, Any]
    data_dir: Path
    log: Callable[[LogLevel, str], None]

    @property
    def source_dir(self) -> Path | None:
        """`options.source_dir` 解析出的绝对路径；相对路径按数据根解析。"""
        raw = self.options.get("source_dir")

        if not isinstance(raw, str) or not raw:
            return None

        path = Path(raw).expanduser()

        return path if path.is_absolute() else (self.data_dir / path)


def estimate_tokens(text: str) -> int:
    """按 `CHARS_PER_TOKEN` 估算一段文本的 token 数。"""
    return tokens_from_chars(len(text))


def tokens_from_chars(chars: int) -> int:
    """按 `CHARS_PER_TOKEN` 估算 `chars` 个字符的 token 数。"""
    return int(chars / CHARS_PER_TOKEN)


def iter_source_files(root: Path) -> Iterator[Path]:
    """索引范围内的文件：`*.java` 递归 + `data/` 全量（D11，排除 `assets/`）。"""
    seen: set[Path] = set()

    for path in sorted(root.rglob(f"*{JAVA_SUFFIX}")):
        if path.is_file():
            seen.add(path)
            yield path

    data_root = root / DATA_DIR_NAME

    if not data_root.is_dir():
        return

    for path in sorted(data_root.rglob("*")):
        if path.is_file() and path not in seen:
            yield path


def run_stage(stage: str, context: JobContext) -> dict[str, Any]:
    """执行一个阶段，返回要并入 checkpoint 的产物。

    #4 只实现 `parse`；其余阶段直接标记完成，留给 #13 接真正的流水线。
    """
    if stage == "parse":
        return _parse_stage(context)

    # TODO(#13)：download 拉取源码、decompile 反编译、summarize 生成中文摘要、
    # finalize 校验并落 `db/<version>.sqlite`。当前一律视为已完成。
    return {}


def _parse_stage(context: JobContext) -> dict[str, Any]:
    """统计 `source_dir` 下 `.java` 与 `data/` 的文件数、token 估算与字节数。"""
    root = context.source_dir

    if root is None:
        raise StageError("index 任务缺少 options.source_dir")

    if not root.is_dir():
        raise StageError(f"source_dir 不存在或不是目录：{root}")

    context.log("info", f"parse：开始扫描 {root}")

    files = 0
    chars = 0
    size_bytes = 0

    for path in iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise StageError(f"读取源码失败：{path}（{error}）") from error

        files += 1
        chars += len(text)
        size_bytes += path.stat().st_size

    tokens = tokens_from_chars(chars)
    context.log("info", f"parse：{files} 个文件、约 {tokens} token、{size_bytes} 字节")

    return {"files": files, "tokens": tokens, "source_size_bytes": size_bytes}


class JobRunner:
    """串行任务执行器。`start()` 起 asyncio 任务，`stop()` 等它收尾。"""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        poll_interval: float = 0.2,
    ) -> None:
        self._settings = settings
        self._database = database
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return

        # 先收上次进程遗留的 running，再开消费循环：否则它们永远不会被认领。
        await asyncio.to_thread(self._recover_interrupted)
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
                await asyncio.to_thread(
                    self._fail, str(row["id"]), "任务执行异常，请查看服务端日志"
                )

    async def _idle(self) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)

    async def _execute(self, row: sqlite3.Row) -> None:
        job_id = str(row["id"])
        version = str(row["version"])
        kind = str(row["kind"])
        options = store.options_of(row)
        checkpoint = store.checkpoint_of(row)
        done = {str(item) for item in checkpoint.get("stages", [])}
        total = len(STAGES)

        for index, stage in enumerate(STAGES):
            if stage in done:
                continue

            if await asyncio.to_thread(self._is_cancel_requested, job_id):
                await asyncio.to_thread(self._cancel, job_id, stage)
                return

            await asyncio.to_thread(self._mark, job_id, stage, index / total)

            context = JobContext(
                job_id=job_id,
                kind=kind,
                version=version,
                options=options,
                checkpoint=checkpoint,
                data_dir=self._settings.data_dir,
                log=lambda level, message: self._log(job_id, level, message),
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

        await asyncio.to_thread(self._succeed_if_not_canceled, job_id, version, checkpoint)

    # ------------------------------------------------------------------
    # 所有落库操作都开一条短连接，并在写事务里完成「写状态 + 追加日志」。
    # ------------------------------------------------------------------

    def _recover_interrupted(self) -> None:
        """启动时把遗留的 `running` 任务重排为 queued，并记一条 info 日志说明来自中断恢复。"""
        with self._database.connect() as conn, transaction(conn):
            job_ids = store.requeue_running(conn)

            for job_id in job_ids:
                store.append_log(
                    conn,
                    job_id,
                    level="info",
                    message="服务重启：任务从上次中断处重新排队",
                )

        if job_ids:
            logger.info("从上次中断恢复：%d 个 running 任务重新排队", len(job_ids))

    def _claim(self) -> sqlite3.Row | None:
        with self._database.connect() as conn, transaction(conn):
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

    def _succeed_if_not_canceled(
        self,
        job_id: str,
        version: str,
        checkpoint: dict[str, Any],
    ) -> None:
        """收尾前再查一次取消：阶段循环只在边界检查，finalize 执行期间到达的取消会漏掉。"""
        if self._is_cancel_requested(job_id):
            self._cancel(job_id, STAGES[-1])
            return

        self._succeed(job_id, version, checkpoint)

    def _succeed(
        self,
        job_id: str,
        version: str,
        checkpoint: dict[str, Any],
    ) -> None:
        """全部阶段完成：登记版本（#13 起改在 finalize 校验 db 产物后再登记）。"""
        with self._database.connect() as conn, transaction(conn):
            store.upsert_version(
                conn,
                version=version,
                files=int(checkpoint.get("files", 0)),
                tokens=int(checkpoint.get("tokens", 0)),
                # #4 的 parse 阶段只统计文件数与 token：不 remap、不生成摘要，
                # 这两个标记待 #7（remap）/#13（summaries）接管后按真实产物写入。
                obfuscated=False,
                has_summaries=False,
                source_size_bytes=int(checkpoint.get("source_size_bytes", 0)),
                db_path=None,
            )
            store.append_log(conn, job_id, level="info", message="任务完成")
            store.finish(conn, job_id, status="succeeded", message="索引完成")

    def _fail(self, job_id: str, error: str) -> None:
        with self._database.connect() as conn, transaction(conn):
            store.append_log(conn, job_id, level="error", message=error)
            store.finish(conn, job_id, status="failed", message="任务失败", error=error)

    def _cancel(self, job_id: str, stage: str) -> None:
        with self._database.connect() as conn, transaction(conn):
            store.append_log(conn, job_id, level="warn", message=f"在 {stage} 阶段边界取消")
            store.finish(conn, job_id, status="canceled", message=f"已取消（停在 {stage}）")
