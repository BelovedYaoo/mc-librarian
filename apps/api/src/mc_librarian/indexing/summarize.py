"""中文摘要层（issue #7）：类级三行摘要的生成、幂等、存储与 FTS5 索引。

流程（规格补充 §3–§6）：

1. 从索引库读出每个类的「内容指纹」：FQN + 父类 + 接口 + 字段签名 + public 方法签名；
2. 算 `content_hash`，与 `summaries` 里已有的比：哈希未变的类**不重新生成**（幂等，不重复计费）；
3. 剩下的按 **package** 分批（每批 `summary_batch_size` 个，建议 30–50），并发
   `summary_concurrency`（asyncio 信号量）调用 LLM，要求返回 JSON `{"<fqn>": "<三行摘要>"}`；
4. 单类失败只记该类的失败原因，同批其余类照常入库；阶段结束时若仍有失败，由调用方
   （`jobs.runner`）把任务标成 `failed`，但**已生成的摘要保留**；
5. 入库前用 `jieba` 把 `text_zh` 切成空格分隔的词写进 `summaries_fts`（FTS5，`unicode61`）。

**为什么预分词**：`trigram` 对两字词（「刷怪」）不友好，而 `unicode61` 又不切中文，两者单独用
都会漏掉两字词；所以入库前 jieba 切好、查询时对 query 做同样切分。

LLM 配置只从环境变量读（`#11` 未落地），未配置时明确报错（见 `config.resolve_llm_config`），
不静默跳过——静默跳过会让索引看起来「有摘要」但实际为空。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jieba

from mc_librarian.config import LlmConfig
from mc_librarian.db import utc_now
from mc_librarian.indexing.text import count_tokens
from mc_librarian.schemas import LogLevel

logger = logging.getLogger("mc_librarian.indexing.summarize")

# 摘要固定 3 行（规格 §1）。
SUMMARY_LINE_COUNT: Final = 3
# 摘要长度上限（字符）：超长响应多半是模型跑偏，截断入库。
SUMMARY_MAX_CHARS: Final = 2000
# 失败原因里最多列几个类，避免一条日志刷屏。
FAILURE_SAMPLE: Final = 5

SYSTEM_PROMPT: Final = (
    "你是 Minecraft 源码知识库的中文摘要写手。为每个类写恰好 3 行中文摘要，格式固定：\n"
    "<职责>：一句话说明这个类在游戏里干什么（不是复述代码）\n"
    "机制：它怎么做到的，或它约束了什么\n"
    "关键成员：英文标识符，逗号分隔\n"
    "必须带上游戏机制词（刷怪、掉落、刻、区块、生成、附魔、红石……），中文在前、英文标识符在后。\n"
    '只输出一个 JSON 对象：{"<类的全限定名>": "<三行摘要>"}，不要输出任何其它文字。'
)

# `summaries` 是普通表（幂等要读 `content_hash`），`summaries_fts` 是独立的 FTS5 表，
# 存 jieba 预分词后的 `text_zh`（规格 §6）。查询走 `summaries_fts`，展示回 `summaries`。
SUMMARIES_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS summaries (
      id           INTEGER PRIMARY KEY,
      class_id     INTEGER REFERENCES classes (id),
      fqn          TEXT NOT NULL UNIQUE,
      text_zh      TEXT NOT NULL,
      keywords     TEXT NOT NULL DEFAULT '',
      content_hash TEXT NOT NULL,
      tokens       INTEGER NOT NULL DEFAULT 0,
      created_at   TEXT NOT NULL
    )
    """,
    "CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts"
    " USING fts5(fqn, text_zh, tokenize = 'unicode61')",
)


class SummaryError(RuntimeError):
    """摘要层可预期的失败；`jobs.runner` 会把它翻成 `StageError`。"""


@dataclass(frozen=True)
class ClassDigest:
    """一个类送进 LLM 的最小信息 + 幂等用的内容指纹（规格 §3/§4）。

    `field_signatures`/`method_signatures` 参与 `content_hash`；`field_names` 是送进 prompt 的
    「字段名」（规格 §3 只说字段名，不算类型）。
    """

    class_id: int
    fqn: str
    package: str | None
    superclass: str | None
    interfaces: tuple[str, ...]
    field_names: tuple[str, ...]
    field_signatures: tuple[str, ...]
    method_signatures: tuple[str, ...]
    content_hash: str


@dataclass(frozen=True)
class SummaryRow:
    """一条待入库的摘要。"""

    class_id: int
    fqn: str
    text_zh: str
    content_hash: str


@dataclass(frozen=True)
class StoredSummary:
    """已入库摘要的一行；重建索引时按 fqn 搬运（`class_id` 在新索引里重映射）。"""

    fqn: str
    text_zh: str
    content_hash: str
    tokens: int
    created_at: str


@dataclass(frozen=True)
class SummaryStats:
    """一次摘要生成的汇总；`generated + reused + failed == classes`。"""

    classes: int
    generated: int
    reused: int
    failed: int
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class SummaryCoverage:
    """摘要覆盖率（已生成 / 类总数），供 `/v1/versions` 与任务日志展示（规格 §8）。"""

    summaries: int
    classes: int


class SummaryClient(Protocol):
    """摘要层的 LLM 客户端接口；测试注入假实现（返回原始响应文本）。

    只负责「一批类 → 原始响应文本」；解析、失败归类、入库都在本模块，解析失败因此能按
    单类失败处理（规格 §3）。
    """

    async def summarize_batch(self, digests: Sequence[ClassDigest]) -> str: ...


# ----------------------------------------------------------------------
# 内容指纹与分词
# ----------------------------------------------------------------------


def compute_content_hash(
    fqn: str,
    superclass: str | None,
    interfaces: Sequence[str],
    field_signatures: Sequence[str],
    method_signatures: Sequence[str],
) -> str:
    """`sha256(FQN + 父类 + 接口 + 字段签名 + public 方法签名)`（规格 §4）。"""
    parts = [fqn, superclass or "", *interfaces, *field_signatures, *method_signatures]

    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def segment(text: str) -> str:
    """jieba 预分词：切成空格分隔的词，供 FTS5 的 `unicode61` 分词器逐词匹配。"""
    jieba.setLogLevel(logging.WARNING)

    return " ".join(jieba.cut(text))


# ----------------------------------------------------------------------
# 从索引库读类内容
# ----------------------------------------------------------------------


def read_class_digests(db_path: Path) -> list[ClassDigest]:
    """读出索引库里全部类的「内容指纹」，按 FQN 排序（顺序确定，便于分批与断言）。"""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            "SELECT c.id AS id, c.fqn AS fqn, c.kind AS kind, c.superclass AS superclass,"
            " c.interfaces AS interfaces, f.pkg AS pkg"
            " FROM classes c JOIN files f ON f.id = c.file_id ORDER BY c.fqn"
        ).fetchall()
        fields = _member_rows(connection, "fields")
        methods = _member_rows(connection, "methods")
    finally:
        connection.close()

    digests: list[ClassDigest] = []

    for row in rows:
        class_id = int(row["id"])
        field_names, field_signatures = _field_parts(fields.get(class_id, []))
        method_signatures = _public_method_signatures(methods.get(class_id, []), str(row["kind"]))
        interfaces = tuple(_load_list(row["interfaces"]))
        superclass = row["superclass"]
        superclass = None if superclass is None else str(superclass)
        fqn = str(row["fqn"])
        digests.append(
            ClassDigest(
                class_id=class_id,
                fqn=fqn,
                package=None if row["pkg"] is None else str(row["pkg"]),
                superclass=superclass,
                interfaces=interfaces,
                field_names=field_names,
                field_signatures=field_signatures,
                method_signatures=method_signatures,
                content_hash=compute_content_hash(
                    fqn, superclass, interfaces, field_signatures, method_signatures
                ),
            )
        )

    return digests


def _member_rows(
    connection: sqlite3.Connection, table: str
) -> dict[int, list[sqlite3.Row]]:
    """把 `fields`/`methods` 按 `class_id` 分组，避免逐类查询（大树上类数以万计）。"""
    if table == "fields":
        sql = (
            "SELECT class_id, name, type, modifiers FROM fields"
            " ORDER BY class_id, start_line, name"
        )
    else:
        sql = (
            "SELECT class_id, name, signature, modifiers FROM methods"
            " ORDER BY class_id, start_line, name"
        )

    grouped: dict[int, list[sqlite3.Row]] = {}

    for row in connection.execute(sql):
        grouped.setdefault(int(row["class_id"]), []).append(row)

    return grouped


def _field_parts(rows: Iterable[sqlite3.Row]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    names: list[str] = []
    signatures: list[str] = []

    for row in rows:
        name = str(row["name"])
        names.append(name)
        # 「字段签名」= 修饰符 + 类型 + 名字；参与 content_hash。
        modifiers = " ".join(_load_list(row["modifiers"]))
        field_type = "" if row["type"] is None else str(row["type"])
        signatures.append(" ".join(part for part in (modifiers, field_type, name) if part))

    return tuple(names), tuple(signatures)


def _public_method_signatures(
    rows: Iterable[sqlite3.Row], class_kind: str
) -> tuple[str, ...]:
    """public 方法签名（不含方法体）；接口与注解类的成员隐式 public。"""
    implicit_public = class_kind in ("interface", "annotation")
    signatures: list[str] = []

    for row in rows:
        modifiers = _load_list(row["modifiers"])

        if not implicit_public and "public" not in modifiers:
            continue

        signature = row["signature"]

        if signature is None or not str(signature).strip():
            continue

        signatures.append(str(signature))

    return tuple(signatures)


def load_content_hashes(db_path: Path) -> dict[str, str]:
    """`summaries` 里已有的 `fqn → content_hash`；表不存在时返回空（首次生成）。"""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        if not _has_table(connection, "summaries"):
            return {}

        return {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT fqn, content_hash FROM summaries")
        }
    except sqlite3.DatabaseError:
        return {}
    finally:
        connection.close()


def read_summary_coverage(db_path: Path) -> SummaryCoverage | None:
    """索引库的摘要覆盖率；不是合法索引（打不开 / 缺 `classes` 表）时返回 `None`。"""
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None

    try:
        if not _has_table(connection, "classes"):
            return None

        classes = _count(connection, "classes")
        summaries = _count(connection, "summaries")
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()

    return SummaryCoverage(summaries=summaries, classes=classes)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    if not _has_table(connection, table):
        return 0

    row = connection.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()

    return int(row[0]) if row is not None else 0


# ----------------------------------------------------------------------
# 分批与提示词
# ----------------------------------------------------------------------


def group_by_package(
    digests: Sequence[ClassDigest], batch_size: int
) -> list[list[ClassDigest]]:
    """按 package 分批：同一 package 的类切成每批至多 `batch_size` 个，不跨 package 混批。"""
    grouped: dict[str, list[ClassDigest]] = {}

    for digest in digests:
        grouped.setdefault(digest.package or "", []).append(digest)

    batches: list[list[ClassDigest]] = []

    for package in sorted(grouped):
        items = grouped[package]

        for start in range(0, len(items), batch_size):
            batches.append(items[start : start + batch_size])

    return batches


def build_prompt(digests: Sequence[ClassDigest]) -> str:
    """一批类的提示词；每个类以 `### <fqn>` 起头，便于模型对号入座。"""
    blocks = [
        f"请为下面 {len(digests)} 个类各写一条三行中文摘要，按 JSON 对象返回。"
    ]

    for digest in digests:
        blocks.append(f"### {digest.fqn}")
        blocks.append(f"父类：{digest.superclass or '无'}")
        blocks.append(f"接口：{', '.join(digest.interfaces) or '无'}")
        blocks.append(f"字段：{', '.join(digest.field_names) or '无'}")
        blocks.append(f"public 方法：{'; '.join(digest.method_signatures) or '无'}")

    return "\n".join(blocks)


_CODE_FENCE: Final = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_batch_response(
    raw: str, digests: Sequence[ClassDigest]
) -> tuple[dict[str, str], dict[str, str]]:
    """解析一批的响应 → (fqn → 摘要, fqn → 失败原因)。

    整体不是合法 JSON（或不是对象）时，**该批全部按单类失败**；JSON 合法但缺某个 fqn、
    或值不是非空字符串时，只有那一个类失败（规格 §3）。
    """
    text = _CODE_FENCE.sub("", raw.strip())

    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        reason = f"响应不是合法 JSON：{error}"
        return {}, {digest.fqn: reason for digest in digests}

    if not isinstance(loaded, dict):
        reason = "响应不是 JSON 对象"
        return {}, {digest.fqn: reason for digest in digests}

    summaries: dict[str, str] = {}
    failures: dict[str, str] = {}

    for digest in digests:
        value = loaded.get(digest.fqn)

        if not isinstance(value, str) or not value.strip():
            failures[digest.fqn] = "响应缺少该类的摘要"
            continue

        summaries[digest.fqn] = value.strip()[:SUMMARY_MAX_CHARS]

    return summaries, failures


# ----------------------------------------------------------------------
# LLM 客户端（OpenAI 兼容端点）
# ----------------------------------------------------------------------


class OpenAiCompatClient:
    """OpenAI 兼容的 `POST {base_url}/chat/completions` 客户端。

    用标准库 `urllib` 发请求（与 `fetch` 流水线同一取舍，不引入额外的 HTTP 运行时依赖），
    在 `asyncio.to_thread` 里执行，因此信号量限制的并发是真正的并发。
    """

    def __init__(self, config: LlmConfig, *, timeout: float) -> None:
        self._config = config
        self._timeout = timeout

    async def summarize_batch(self, digests: Sequence[ClassDigest]) -> str:
        payload = {
            "model": self._config.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(digests)},
            ],
        }

        return await asyncio.to_thread(
            post_chat_completion,
            f"{self._config.base_url}/chat/completions",
            payload,
            self._config.api_key,
            self._timeout,
        )


def post_chat_completion(
    url: str, payload: dict[str, object], api_key: str, timeout: float
) -> str:
    """发一次 chat completion 请求，返回首个 choice 的文本内容。

    失败（网络错误、非 2xx、响应结构不符）统一抛 `SummaryError`，由调用方按批次失败处理。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        # 端点由配置给出（环境变量），不是用户输入拼进来的。
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise SummaryError(f"LLM 返回 HTTP {error.code}：{detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise SummaryError(f"LLM 请求失败：{error}") from error

    try:
        loaded = json.loads(raw)
        content = loaded["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise SummaryError(f"LLM 响应结构不符：{raw[:500]}") from error

    if not isinstance(content, str):
        raise SummaryError("LLM 响应的 message.content 不是字符串")

    return content


# ----------------------------------------------------------------------
# 生成与入库
# ----------------------------------------------------------------------


async def summarize_index(
    db_path: Path,
    *,
    client_factory: Callable[[], SummaryClient],
    batch_size: int,
    concurrency: int,
    log: Callable[[LogLevel, str], None],
) -> SummaryStats:
    """为索引库生成缺失的摘要并入库，返回汇总统计。

    `client_factory` 只在**确有类需要生成时**才调用：全部命中哈希（重跑同一个版本）时
    不碰 LLM，也就不需要 LLM 配置（幂等、不重复计费）。
    """
    digests = read_class_digests(db_path)
    existing = load_content_hashes(db_path)
    pending = [digest for digest in digests if existing.get(digest.fqn) != digest.content_hash]
    reused = len(digests) - len(pending)

    if not pending:
        return SummaryStats(classes=len(digests), generated=0, reused=reused, failed=0)

    client = client_factory()
    batches = group_by_package(pending, batch_size)
    log("info", f"摘要：{len(pending)} 个类待生成，分 {len(batches)} 批，并发 {concurrency}")

    results = await _run_batches(client, batches, concurrency)
    summaries: dict[str, str] = {}
    failures: dict[str, str] = {}

    for summaries_part, failures_part in results:
        summaries.update(summaries_part)
        failures.update(failures_part)

    rows = [
        SummaryRow(
            class_id=digest.class_id,
            fqn=digest.fqn,
            text_zh=summaries[digest.fqn],
            content_hash=digest.content_hash,
        )
        for digest in pending
        if digest.fqn in summaries
    ]

    if rows:
        write_summaries(db_path, rows)

    return SummaryStats(
        classes=len(digests),
        generated=len(rows),
        reused=reused,
        failed=len(failures),
        failures=tuple(f"{fqn}：{reason}" for fqn, reason in sorted(failures.items())),
    )


async def _run_batches(
    client: SummaryClient, batches: Sequence[Sequence[ClassDigest]], concurrency: int
) -> list[tuple[dict[str, str], dict[str, str]]]:
    """并发跑各批（信号量限制并发数），返回每批的 (摘要, 失败)。"""
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(
        batch: Sequence[ClassDigest],
    ) -> tuple[dict[str, str], dict[str, str]]:
        async with semaphore:
            try:
                raw = await client.summarize_batch(batch)
            except Exception as error:  # 单批失败不能拖垮其余批次（规格 §5）
                reason = f"LLM 调用失败：{error}"
                return {}, {digest.fqn: reason for digest in batch}

            return parse_batch_response(raw, batch)

    return list(await asyncio.gather(*(run_one(batch) for batch in batches)))


def write_summaries(db_path: Path, rows: Sequence[SummaryRow]) -> None:
    """把摘要写入索引库：`summaries`（普通表）+ `summaries_fts`（jieba 预分词的 FTS5）。

    单事务提交：失败时整批回滚，不留「一半新一半旧」的摘要。
    """
    connection = sqlite3.connect(db_path)

    try:
        for statement in SUMMARIES_DDL:
            connection.execute(statement)

        for row in rows:
            _insert_summary(
                connection,
                class_id=row.class_id,
                fqn=row.fqn,
                text_zh=row.text_zh,
                content_hash=row.content_hash,
                tokens=count_tokens(row.text_zh),
                created_at=utc_now(),
            )

        connection.commit()
    finally:
        connection.close()


def export_summaries(db_path: Path) -> list[StoredSummary]:
    """读出现有摘要，供重建索引时搬过去（`parse` 原子替换整库，不搬就等于全丢）。

    表不存在、文件不存在或不是合法索引时返回空——重建路径不该因为「上一份索引坏」而失败。
    """
    if not db_path.is_file():
        return []

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []

    try:
        if not _has_table(connection, "summaries"):
            return []

        return [
            StoredSummary(
                fqn=str(row[0]),
                text_zh=str(row[1]),
                content_hash=str(row[2]),
                tokens=int(row[3]),
                created_at=str(row[4]),
            )
            for row in connection.execute(
                "SELECT fqn, text_zh, content_hash, tokens, created_at FROM summaries"
            )
        ]
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()


def restore_summaries(db_path: Path, records: Sequence[StoredSummary]) -> int:
    """把搬过来的摘要写进（刚重建的）索引，返回实际写回的行数。

    `class_id` 按 fqn 在新索引里重映射；类已不存在的摘要直接丢弃（不留下指向错误类的行）。
    """
    if not records:
        return 0

    connection = sqlite3.connect(db_path)

    try:
        for statement in SUMMARIES_DDL:
            connection.execute(statement)

        class_ids = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT fqn, id FROM classes")
        }
        restored = 0

        for record in records:
            class_id = class_ids.get(record.fqn)

            if class_id is None:
                continue

            _insert_summary(
                connection,
                class_id=class_id,
                fqn=record.fqn,
                text_zh=record.text_zh,
                content_hash=record.content_hash,
                tokens=record.tokens,
                created_at=record.created_at,
            )
            restored += 1

        connection.commit()

        return restored
    finally:
        connection.close()


def _insert_summary(
    connection: sqlite3.Connection,
    *,
    class_id: int,
    fqn: str,
    text_zh: str,
    content_hash: str,
    tokens: int,
    created_at: str,
) -> None:
    """写入一行摘要并同步 FTS 表（`summaries.fqn` 是 UNIQUE，重写即覆盖）。"""
    connection.execute(
        "INSERT INTO summaries"
        " (class_id, fqn, text_zh, keywords, content_hash, tokens, created_at)"
        " VALUES (?, ?, ?, '', ?, ?, ?)"
        " ON CONFLICT(fqn) DO UPDATE SET"
        " class_id = excluded.class_id, text_zh = excluded.text_zh,"
        " content_hash = excluded.content_hash, tokens = excluded.tokens,"
        " created_at = excluded.created_at",
        (class_id, fqn, text_zh, content_hash, tokens, created_at),
    )
    connection.execute("DELETE FROM summaries_fts WHERE fqn = ?", (fqn,))
    connection.execute(
        "INSERT INTO summaries_fts (fqn, text_zh) VALUES (?, ?)",
        (fqn, segment(text_zh)),
    )


def _load_list(raw: object) -> list[str]:
    """JSON 文本列 → 字符串列表；坏数据退回空列表。"""
    if not isinstance(raw, str) or not raw:
        return []

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(loaded, list):
        return []

    return [str(item) for item in loaded]
