"""`JobRunner`：随 FastAPI lifespan 启动、并发 1 消费 `jobs` 队列的 asyncio 任务。

设计（见 `docs/00-overview.md` §4、D13）：

- 进程内单任务串行消费，没有独立 worker；一次只跑一个任务（并发 1）。
- 阶段序列固定为 `parse → summarize → finalize`（`#4`；`#13` 会在前面补 `download`/`decompile`）。
  每完成一个阶段就把阶段名与产物写进 `jobs.checkpoint`；认领任务时跳过已完成的阶段，
  失败后重排即可续跑。
- `cancel` 只在**阶段边界**生效：阶段内部不中断，跑完当前阶段后检查标志再收尾；终态写入
  与取消标志在同一条带条件的 UPDATE 里判定，finalize 期间到达的取消也不会被吞掉。
- 启动时把上一次进程遗留的 `running` 任务收回 `queued`（已请求取消的直接置 `canceled`），
  否则它们永远不会再被认领。
- 阻塞的文件扫描与 sqlite 读写都丢进 `asyncio.to_thread`，不占事件循环。

`#4` 只真正实现 `parse`（统计文件数与 token）；`summarize`/`finalize` 是占位，见 #13。
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

from mc_librarian import audit
from mc_librarian.config import Settings
from mc_librarian.db import Database, transaction
from mc_librarian.jobs import store
from mc_librarian.schemas import LogLevel

logger = logging.getLogger("mc_librarian.jobs")

# 阶段顺序即进度分母；`checkpoint.stages` 记已完成的阶段名。
# `#4` 只支持带 `options.source_dir` 的任务（接口层缺它就 422），所以阶段序列固定这三段；
# `#13` 落地真实版本流水线时会在前面补 `download`/`decompile`。
STAGES: Final[tuple[str, ...]] = ("parse", "summarize", "finalize")
# 收尾阶段，取消时用来描述停在哪。
FINAL_STAGE: Final = "finalize"

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
    """一个阶段的输入：任务参数 + 数据根 + 日志回调。"""

    options: dict[str, Any]
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


def tokens_from_chars(chars: int) -> int:
    """按 `CHARS_PER_TOKEN` 估算 `chars` 个字符的 token 数。"""
    return int(chars / CHARS_PER_TOKEN)


def iter_source_files(root: Path) -> Iterator[Path]:
    """索引范围内的文件，白名单只有两类：`*.java`（递归）与 `data/` 下全量（D11）。

    白名单之外一律不索引，因此 `assets/` 这类目录（不在白名单内）天然被排除；
    先 yield 的 `.java` 用 `seen` 去重，`data/` 下的同名文件不会重复产出。
    """
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

    `#4` 只实现 `parse`（统计文件数与 token 估算）；`summarize`/`finalize` 待 #13 接真正的
    流水线，这里只标记完成。
    """
    if stage == "parse":
        return _parse_stage(context)

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
        options = store.options_of(row)
        checkpoint = store.checkpoint_of(row)
        done = {str(item) for item in checkpoint.get("stages", [])}
        total = len(STAGES)
        # 当前停在的阶段（与 `jobs.stage` 列一致）；取消时用它写 `stage` 与 `message`。
        current_stage: str | None = None

        for index, stage in enumerate(STAGES):
            if stage in done:
                current_stage = stage
                continue

            if await asyncio.to_thread(self._is_cancel_requested, job_id):
                await asyncio.to_thread(self._cancel, job_id, current_stage)
                return

            await asyncio.to_thread(self._mark, job_id, stage, index / total)
            current_stage = stage

            context = JobContext(
                options=options,
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

        await asyncio.to_thread(self._finish, job_id, version, checkpoint)

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

    def _finish(self, job_id: str, version: str, checkpoint: dict[str, Any]) -> None:
        """全部阶段完成：终态与取消标志在同一条 UPDATE 里判定，再按实际终态记账。

        `succeeded` 时登记版本行：**仅当版本未注册时插入一行**，已注册版本不动；
        真的插入了才写 `version.recorded` 审计。`canceled` 时不碰版本表。这样 finalize
        执行期间到达的取消不会被吞掉。
        """
        with self._database.connect() as conn, transaction(conn):
            status = store.finish_succeeded_or_canceled(
                conn,
                job_id,
                succeeded_message="索引完成",
                canceled_message=f"已取消（停在 {FINAL_STAGE}）",
            )

            if status == "succeeded":
                inserted = store.insert_version_if_absent(
                    conn,
                    version=version,
                    files=int(checkpoint.get("files", 0)),
                    tokens=int(checkpoint.get("tokens", 0)),
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
                    conn, job_id, level="warn", message=f"在 {FINAL_STAGE} 阶段边界取消"
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
