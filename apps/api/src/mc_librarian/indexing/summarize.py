"""中文摘要层（issue #7）：类级三行摘要的生成、幂等、存储与 FTS5 索引。

流程（规格补充 §3–§6）：

1. 从索引库读出每个类的「内容指纹」：FQN + 父类 + 接口 + 字段签名 + public 方法签名；
2. 算 `content_hash`，与 `summaries` 里已有的比：哈希未变的类**不重新生成**（幂等，不重复计费）；
3. 剩下的按 **package** 分批（每批 `summary_batch_size` 个，建议 30–50），并发
   `summary_concurrency`（asyncio 信号量）调用 LLM，要求返回 JSON `{"<fqn>": "<三行摘要>"}`；
4. 单类失败只记该类的失败原因，同批其余类照常入库；阶段结束时若仍有失败，由调用方
   （`jobs.runner`）把任务标成 `failed`，但**已生成的摘要保留**。摘要还要过 `validate_summary`
   的三行格式闸门（规格 §1）：不合格的按单类失败处理，不进库（`parse_batch_response`）；
5. 入库前用 `jieba` 把 `text_zh` 切成空格分隔的词写进 `summaries_fts`（FTS5，`unicode61`）。
   FTS 行的 `rowid` 钉成 `summaries.id`：重写时按 `rowid` 删旧行，避开 FTS5 上的全表扫。
   这个前提由 `write_summaries` 在写之前校验：旧库（rowid 由 FTS5 计数器自增、删除按 fqn）
   一旦错位，按 `rowid` 删会删掉**另一个类**的 FTS 行，那个类从摘要通道静默消失——
   发现错位就按 `summaries` 整表重建 FTS 索引，而不是要求旧库重跑 `--force`。

**为什么预分词**：`trigram` 对两字词（「刷怪」）不友好，而 `unicode61` 又不切中文，两者单独用
都会漏掉两字词；所以入库前 jieba 切好、查询时对 query 做同样切分。

LLM 配置默认从环境变量读（`MC_LIBRARIAN_LLM_*`），`#11` 落地后 `settings` 表的覆盖优先
（调用方先用 `settings_store.effective_settings()` 叠好再传进来），未配置时明确报错
（见 `config.resolve_llm_config`），不静默跳过——静默跳过会让索引看起来「有摘要」但实际为空。
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

# jieba 首次使用会把「Building prefix dict…」之类的日志打到 stderr：在模块导入时设一次级别，
# 而不是每次分词都设（`segment` 在摘要入库路径上被逐类调用）。
jieba.setLogLevel(logging.WARNING)

# 摘要固定 3 行（规格 §1）。
SUMMARY_LINE_COUNT: Final = 3
# 三行**可选**的字面前缀（规格 §1 的格式建议）：模型有时写、有时不写，两种都收。
# 判定前一律剥掉，只校验实质——真实模型经常写成三行自由文本，把「有没有前缀」当判定依据
# 会把整批摘要全拒掉（issue #23 的实测）。
LINE_PREFIXES: Final[tuple[str, ...]] = ("职责", "机制", "关键成员")
# 第 3 行（关键成员）的实质：至少一个英文标识符。驼峰、下划线、点号路径都算，
# 长度 ≥ 3（首字符 + 两个以上）。这是「关键成员」唯一能可靠校验的特征。
IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# 第 1 行（职责）的最短长度：比这短的多半是「Mob」这类复述类名的垃圾。定得宽松（4 字符）
# ——「管理玩家背包」这种简洁但合格的第 1 行不该被格式闸门拒掉（拒好摘要的闸门比没有更糟）。
SUMMARY_MIN_HEAD_CHARS: Final = 4
# 前两行必须含中日韩字符：否则就是把类名 / 代码复述一遍（只校验前两行——第 3 行是英文标识符）。
CJK_CHAR: Final = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# FTS5 的 `unicode61` 分词器会把纯标点切成零个 token（`\w` 在 `str` 上含汉字）。
FTS_WORD_CHAR: Final = re.compile(r"\w")
# 摘要长度上限（字符）：超长响应多半是模型跑偏，截断入库。
SUMMARY_MAX_CHARS: Final = 2000
# 失败原因里最多列几个类，避免一条日志刷屏。
FAILURE_SAMPLE: Final = 5

SYSTEM_PROMPT: Final = (
    "你是 Minecraft 源码知识库的中文摘要写手，只输出一个 JSON 对象，不要输出任何其它文字。"
)

# 摘要的三行格式要求（规格 §1）+ 一条完整样板。
# 这段要求放在**用户消息**里（`build_prompt`）、紧挨着类清单：实测同一个模型在「要求只写在
# system 消息里」时会忽略格式（输出 markdown 列表或纯中文描述），写在用户消息里则稳定遵守
# （issue #23 的实测：system 消息版本 0/20，用户消息版本 20/20）。
SUMMARY_FORMAT_RULES: Final[tuple[str, ...]] = (
    "每一行都必须带固定前缀：",
    "第 1 行「职责：」+ 这个类在游戏里干什么（不是复述代码）；",
    "第 2 行「机制：」+ 它怎么做到的（带游戏机制词，如刷怪、掉落、刻、区块、生成）；",
    "第 3 行「关键成员：」+ 该类真实的英文字段名 / 方法名，逗号分隔。"
    "第 3 行只许英文标识符，不许写中文描述。",
    "格式样板（类名与内容只是占位，不要照抄）：",
    '{"com.example.ExampleClass": "职责：控制每个区块的刷怪上限与生成节奏\\n'
    "机制：按生物群系权重挑选候选，受 mob cap 与玩家距离约束\\n"
    '关键成员：spawnInterval, getMaxInstancesPerChunk"}',
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
    """jieba 预分词：切成空格分隔的词，供 FTS5 的 `unicode61` 分词器逐词匹配。

    这是**入库**口径（标点留在串里由 `unicode61` 自己丢掉）；查询侧要拼 `MATCH` 串，
    用 `fts_match_query`。
    """
    return " ".join(jieba.cut(text))


def fts_match_query(text: str) -> str:
    """把 query 的分词结果拼成 FTS5 的 `MATCH` 串：每个词加引号，丢掉零 token 的碎片。

    jieba 会把标点单独切出来（「刷怪，掉落」→「刷怪」「，」「掉落」），而 `unicode61` 把纯标点
    切成零个 token：这类碎片留在裸串里只会让整个 `MATCH` 报语法错（该路静默返回空）。
    `"`、`*`、`-` 同理，加引号后按字面处理（串内的 `"` 按 FTS5 的写法翻倍转义）。
    所有词都是标点时返回空串，调用方据此跳过这一路，不去执行注定报错的 SQL。
    """
    tokens = [
        token.replace('"', '""') for token in jieba.cut(text) if FTS_WORD_CHAR.search(token)
    ]

    return " ".join(f'"{token}"' for token in tokens)


def warmup_segmentation() -> str:
    """启动时预热 jieba 词典，返回一句话描述（供 lifespan 记 INFO 日志）。

    `jieba.cut` 首次调用会同步加载词典（本机实测约 0.5 s，49.8 万词条）：不预热的话这 0.5 s
    落在第一次摘要查询上，就是一次无谓的阻塞请求。预热与 `warmup_token_counting` 同口径——
    把「词典真的加载了」写进启动日志，而不是等第一次查询才暴露。

    词典随 jieba 包一起安装（不联网），加载失败说明安装坏了：这里不兜底，让启动直接失败。
    """
    jieba.initialize()

    return f"中文分词：jieba 词典已加载（{len(jieba.dt.FREQ)} 个词）"


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
    """一批类的提示词：格式要求 + 样板（`SUMMARY_FORMAT_RULES`），然后是每个类。

    每个类以 `### <fqn>` 起头，便于模型对号入座。格式要求放用户消息而不是 `SYSTEM_PROMPT`
    的理由见 `SUMMARY_FORMAT_RULES` 的注释。
    """
    blocks = [
        f"为下面 {len(digests)} 个类各写一条三行中文摘要，按 JSON 对象返回，键是类的全限定名。",
        *SUMMARY_FORMAT_RULES,
        "",
    ]

    for digest in digests:
        blocks.append(f"### {digest.fqn}")
        blocks.append(f"父类：{digest.superclass or '无'}")
        blocks.append(f"接口：{', '.join(digest.interfaces) or '无'}")
        blocks.append(f"字段：{', '.join(digest.field_names) or '无'}")
        blocks.append(f"public 方法：{'; '.join(digest.method_signatures) or '无'}")

    return "\n".join(blocks)


# 模型常把 JSON（甚至整段摘要）包在 ```json 围栏里——真实模型的常态，不是异常。
_CODE_FENCE: Final = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def strip_code_fence(text: str) -> str:
    """剥掉首尾的 markdown 代码围栏（` ```json ` / ` ``` `）与前后空白。"""
    return _CODE_FENCE.sub("", text.strip())


def extract_json_object(raw: str) -> str:
    """从模型响应里取出最外层的 JSON 对象文本。

    剥掉围栏与空白后，从第一个 `{` 起用 `json.JSONDecoder.raw_decode` 解出一个完整的 JSON
    值：前置的「以下是摘要：」与尾随的「希望有帮助」都不会让整批摘要判失败。取不到时原样返回，
    由调用方的 `json.loads` 去报「不是合法 JSON」——失败归类（整批失败）因此不变。
    """
    text = strip_code_fence(raw)
    start = text.find("{")

    if start == -1:
        return text

    try:
        _, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return text

    return text[start:end]


def normalize_summary(text: str) -> str:
    """把模型响应规整成固定 3 行：逐行截断后，把第 4 行起的折行并回第 3 行。

    `SUMMARY_MAX_CHARS` 是**逐行**上限：对整段文本一次性截断会把第 2/3 行一起切掉，入库后
    只剩 1 行、绕过「固定 3 行」的闸门。第 4 行起视为第 3 行成员列表的折行，空格拼接。
    首尾的代码围栏也在这里剥掉：模型有时把整段摘要包进围栏。
    """
    lines = [
        line.strip()[:SUMMARY_MAX_CHARS] for line in strip_code_fence(text).splitlines()
    ]

    if len(lines) > SUMMARY_LINE_COUNT:
        tail = " ".join(line for line in lines[SUMMARY_LINE_COUNT - 1 :] if line)
        lines = [*lines[: SUMMARY_LINE_COUNT - 1], tail]

    return "\n".join(lines)


def _strip_line_prefix(line: str) -> str:
    """剥掉行首可选的「职责：/机制：/关键成员：」前缀（半角 / 全角冒号都认）。"""
    for prefix in LINE_PREFIXES:
        if line.startswith(prefix):
            return line[len(prefix) :].lstrip("：: ").strip()

    return line.strip()


def validate_summary(text: str, *, fqn: str) -> str | None:
    """校验一条摘要是否满足规格 §1 的三行格式；合格返回 `None`，不合格返回原因。

    只校验**实质**，不要求字面前缀：真实模型经常写成三行自由文本、不带「机制：/关键成员：」，
    那同样是合格摘要——把「有没有前缀」当判定依据会把整批摘要全拒掉（issue #23 的实测）。
    传入的文本由 `normalize_summary` 规整（剥围栏、逐行截断、第 4 行起的折行并回第 3 行），
    因此「至少 3 行」这一条对折行的成员列表同样成立。判定规则：

    - 至少 3 行；
    - 第 1 行（剥掉前缀后）长度 ≥ `SUMMARY_MIN_HEAD_CHARS` 且不等于该类的 FQN 或简单名；
    - 前两行含中日韩字符（摘要必须用中文写，不是复述类名）；
    - 第 3 行含至少一个英文标识符（`IDENTIFIER`）——这是「关键成员」的实质。

    先判第 1 行再看整体中文：模型复述类名时（第 1 行就是 FQN）报「复述类名」比报「没有中文」
    更精确。所有规则都只决定「拒 / 收」与失败原因，不改变是否入库。
    不做语义判断：「有没有游戏机制词」「是不是在复述代码」都无法可靠校验，那是 prompt 的责任。
    """
    lines = [line.strip() for line in normalize_summary(text).splitlines()]

    if len(lines) < SUMMARY_LINE_COUNT:
        return f"至少 {SUMMARY_LINE_COUNT} 行，实际 {len(lines)} 行"

    head = _strip_line_prefix(lines[0])
    mechanism = _strip_line_prefix(lines[1])
    members = _strip_line_prefix(lines[2])

    if len(head) < SUMMARY_MIN_HEAD_CHARS:
        return f"第 1 行太短（少于 {SUMMARY_MIN_HEAD_CHARS} 个字符）"

    if head in (fqn, fqn.rsplit(".", 1)[-1]):
        return "第 1 行只是复述类名"

    if CJK_CHAR.search(head) is None or CJK_CHAR.search(mechanism) is None:
        return "前两行必须有中文（必须用中文写，不是复述类名）"

    if IDENTIFIER.search(members) is None:
        return "第 3 行必须含至少一个英文标识符（关键成员）"

    return None


def parse_batch_response(
    raw: str, digests: Sequence[ClassDigest]
) -> tuple[dict[str, str], dict[str, str]]:
    """解析一批的响应 → (fqn → 摘要, fqn → 失败原因)。

    整体不是合法 JSON（或不是对象）时，**该批全部按单类失败**；JSON 合法但缺某个 fqn、
    值不是非空字符串、或值不满足三行格式（`validate_summary`）时，只有那一个类失败
    （规格 §1/§3）——不合格的摘要不进库，走调用方既有的失败/重试路径。
    响应里的围栏与前后散文由 `extract_json_object` 剥掉：那是真实模型的常态，不是异常。
    """
    try:
        loaded = json.loads(extract_json_object(raw))
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

        # 先逐行截断（`normalize_summary`）再校验：反过来的话超长第 1 行会连第 2/3 行一起
        # 被切掉，入库后只剩 1 行，绕过「固定 3 行」的闸门。
        normalized = normalize_summary(value)
        problem = validate_summary(normalized, fqn=digest.fqn)

        if problem is not None:
            failures[digest.fqn] = f"摘要格式不合格：{problem}"
            continue

        summaries[digest.fqn] = normalized

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
            "temperature": self._config.temperature,
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

    写之前先校验 FTS 行与 `summaries` 对齐（`_insert_summary` 按 `rowid` 删旧行依赖这个前提）：
    修复前的旧库不满足它，错位时按 `rowid` 删会删掉另一个类的 FTS 行，所以先整表重建。
    单事务提交：失败时整批回滚，不留「一半新一半旧」的摘要。
    """
    connection = sqlite3.connect(db_path)

    try:
        for statement in SUMMARIES_DDL:
            connection.execute(statement)

        if _summaries_fts_is_misaligned(connection):
            rebuilt = rebuild_summaries_fts(connection)
            logger.warning(
                "summaries_fts 与 summaries 错位，已按 summaries 整表重建（%d 行）", rebuilt
            )

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


def _summaries_fts_is_misaligned(connection: sqlite3.Connection) -> bool:
    """`summaries_fts` 是否与 `summaries` 错位（rowid 不等于 id，或两边行集不一致）。

    `_insert_summary` 按 `rowid` 删旧行，前提是 FTS 行的 rowid 钉成了 `summaries.id`。修复前
    产出的索引库不满足这个前提（rowid 由 FTS5 计数器自增、删除按 fqn）：某个类被重写过就会
    错位，此时按 rowid 删会删掉**另一个类**的 FTS 行，那个类从摘要通道静默消失。
    """
    mismatched = connection.execute(
        "SELECT COUNT(*) FROM summaries s JOIN summaries_fts f ON f.fqn = s.fqn"
        " WHERE f.rowid <> s.id"
    ).fetchone()

    if mismatched is not None and int(mismatched[0]) > 0:
        return True

    # 任一侧多出来的行同样算错位：只对得上 rowid 不够，两边行集必须一致。
    return _count(connection, "summaries") != _count(connection, "summaries_fts")


def rebuild_summaries_fts(connection: sqlite3.Connection) -> int:
    """按 `summaries` 表整表重建 FTS 索引（重新 jieba 分词），返回重建的行数。

    清空 FTS 表再按 `summaries.id` 逐行钉 rowid：重建后 `_insert_summary` 的「按 rowid 删」
    才重新成立，也不会误删其它类的行。
    """
    rows = connection.execute("SELECT id, fqn, text_zh FROM summaries").fetchall()
    connection.execute("DELETE FROM summaries_fts")
    connection.executemany(
        "INSERT INTO summaries_fts (rowid, fqn, text_zh) VALUES (?, ?, ?)",
        [(int(row[0]), str(row[1]), segment(str(row[2]))) for row in rows],
    )

    return len(rows)


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
    """写入一行摘要并同步 FTS 表（`summaries.fqn` 是 UNIQUE，重写即覆盖）。

    FTS 行的 `rowid` 显式钉成 `summaries.id`（`RETURNING id` 拿到），删旧行时就能按 `rowid`
    精确定位：`DELETE ... WHERE fqn = ?` 在 FTS5 上是全表扫，5,000 个类时是 O(n²)。
    这个前提不是自明的：旧库可能已经错位，`write_summaries` 在写之前会校验并在必要时整表重建。
    """
    row = connection.execute(
        "INSERT INTO summaries"
        " (class_id, fqn, text_zh, content_hash, tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(fqn) DO UPDATE SET"
        " class_id = excluded.class_id, text_zh = excluded.text_zh,"
        " content_hash = excluded.content_hash, tokens = excluded.tokens,"
        " created_at = excluded.created_at"
        " RETURNING id",
        (class_id, fqn, text_zh, content_hash, tokens, created_at),
    ).fetchone()

    if row is None:  # `RETURNING` 必定回一行；真没有说明库状态不对，别静默写坏 FTS
        raise SummaryError(f"写入摘要失败（没有返回行）：{fqn}")

    summary_id = int(row[0])
    connection.execute("DELETE FROM summaries_fts WHERE rowid = ?", (summary_id,))
    connection.execute(
        "INSERT INTO summaries_fts (rowid, fqn, text_zh) VALUES (?, ?, ?)",
        (summary_id, fqn, segment(text_zh)),
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
