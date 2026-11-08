"""三路召回通道 + 摘要通道接口（issue #6 规格 §2）。

| 通道 | 数据源 | 排序依据 |
|---|---|---|
| `symbol` | 索引库的 `classes`/`methods`/`fields` | 复用 #5 的四级匹配，同级别按 `fqn` 字典序 |
| `literal` | ripgrep 子进程扫 `<data_dir>/src/<version>/` | 文件路径 + 行号 |
| `reference` | `refs` 表反查引用点 | `path, line` |
| `summary` | `summaries_fts`（FTS5，由 #7 建） | bm25 升序；表不存在时返回空 |
| `patch` | `patches_fts`（FTS5，issue #24 建） | bm25 升序；表不存在时返回空 |

`literal` / `reference` 拿到的是「文件 + 行号」，统一经 `query.symbol_at` 归一化成所在的最内层
符号：同一实体被多路召回时才能被 RRF 累加，`why` 也才会有多个值（规格 §1）。任何一路为空
（含 ripgrep 不可用 / 超时、`summaries_fts` 表不存在或不是 FTS5、query 含 FTS 语法）都不算错误
（规格 §8）。


注：`import shutil as shutil` / `import subprocess as subprocess` 的 `as` 写法是
给 mypy 的显式再导出标记——测试里会 monkeypatch `channels.subprocess.run` 与
`channels.shutil.which`，不这样标注会被 `no_implicit_reexport` 判为 attr-defined。
"""

from __future__ import annotations

import json
import logging
import re
import shutil as shutil
import sqlite3
import subprocess as subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from mc_librarian.indexing import query
from mc_librarian.indexing.summarize import fts_match_query
from mc_librarian.schemas import HitType, SymbolKind

logger = logging.getLogger("mc_librarian.retrieval")

Channel = Literal["summary", "symbol", "param", "literal", "reference", "patch"]

# 每路各取前 50 条候选（规格 §1）。
CHANNEL_LIMIT: Final = 50

# 送进 ripgrep 的只能是「像标识符的 token」：中文整句当正则在源码里恒不命中，还白耗一次
# 5 秒超时的子进程（issue #23）。驼峰、点号路径、下划线都算，长度 ≥ 3（短于此的多是噪声）。
IDENTIFIER_TOKEN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")
IDENTIFIER_MIN_LEN: Final = 3

# ripgrep 的固定参数（规格 §3）；`--json` 便于按行解析。
RG_BINARY: Final = "rg"
RG_TIMEOUT_S: Final = 5.0
RG_ARGS: Final[tuple[str, ...]] = (
    "--json",
    "--max-count",
    "50",
    "--max-columns",
    "200",
    "--max-filesize",
    "2M",
    "--no-ignore",
)


# 索引库的 `classes.kind` 有五种（class/interface/enum/record/annotation），而契约 `Hit.type`
# 只声明 class/method/field/file。类级命中一律归 `class`——与 `/v1/symbol` 的既有行为一致
# （`find_symbols` 里是 `SELECT 'class' AS kind`）。
_TYPE_BY_KIND: Final[dict[str, HitType]] = {
    "class": "class",
    "interface": "class",
    "enum": "class",
    "record": "class",
    "annotation": "class",
    "method": "method",
    "field": "field",
    "file": "file",
}


def hit_type(kind: str) -> HitType:
    """索引库的 kind → 契约的 `Hit.type`。

    未知值**不静默兜底**：那是索引与契约漂移的信号，让它抛出来比返回一个错类型好。
    """
    return _TYPE_BY_KIND[kind]


@dataclass(frozen=True)
class Candidate:
    """一路候选；`symbol` 为 None 表示没有任何符号覆盖这个位置（`type=file`）。

    `target`/`patch_context` 只对 patch 命中有意义（issue #24 规格 §5）：`target` 是被改的源文件，
    `patch_context` 是 hunk 头 + diff 正文（直接当 `snippet` 用，不再读盘）。
    """

    channel: Channel
    path: str
    symbol: str | None
    kind: HitType
    start_line: int
    end_line: int
    summary: str | None = None
    target: str | None = None
    patch_context: str | None = None

    @property
    def key(self) -> str:
        """融合用的实体键。

        带符号时包含行区间：Java 允许同名重载方法，`Class#get` 可能是多个实体，
        只靠 `path#fqn` 会把它们错误地并成一个；行区间由 `symbol_at` 与符号通道一致地给出。
        无符号（`type=file`）时退化为位置 `path:line`。
        """
        if self.symbol:
            return f"{self.path}#{self.symbol}:{self.start_line}-{self.end_line}"

        return f"{self.path}:{self.start_line}"


def symbol_candidates(
    connection: sqlite3.Connection,
    name: str,
    *,
    kind: SymbolKind,
    path_prefix: str | None,
    source: str | None = None,
) -> list[Candidate]:
    """符号通道：复用 #5 的四级匹配（fqn → simple → 后缀 → 编辑距离）。"""
    hits = query.find_symbols(
        connection,
        name,
        fuzzy=True,
        kind=kind,
        limit=CHANNEL_LIMIT,
        path_prefix=path_prefix,
        source=source,
    )

    return [
        Candidate(
            channel="symbol",
            path=hit.path,
            symbol=hit.fqn,
            kind=hit_type(hit.kind),
            start_line=hit.start_line,
            end_line=hit.end_line,
        )
        for hit in hits
    ]


def reference_candidates(
    connection: sqlite3.Connection,
    name: str,
    *,
    path_prefix: str | None,
    source: str | None = None,
) -> list[Candidate]:
    """引用通道：query 命中某个符号名时，反查 `refs` 表里的引用点。"""
    hits = query.find_references(
        connection, name, limit=CHANNEL_LIMIT, path_prefix=path_prefix, source=source
    )

    return [
        candidate
        for hit in hits
        if (candidate := _location_candidate(connection, "reference", hit.path, hit.line, source))
        is not None
    ]


def literal_candidates(
    connection: sqlite3.Connection,
    source_root: Path,
    name: str,
    *,
    path_prefix: str | None,
    source: str | None = None,
) -> list[Candidate]:
    """字面量通道：ripgrep 子进程扫源码树，超时 / 不可用时返回空并写 warn。

    `name` 是自然语言查询时，只把其中「像标识符的 token」送进 ripgrep（`literal_pattern`）：
    整句中文当正则在源码里恒不命中，还白耗一次 5 秒超时的子进程（issue #23）。一个标识符
    token 都没有（纯中文 / 纯标点）时不起子进程，直接返回空。
    """
    pattern = literal_pattern(name)

    if pattern is None:
        return []

    return [
        candidate
        for path, line in _ripgrep(source_root, pattern, path_prefix=path_prefix)
        if (candidate := _location_candidate(connection, "literal", path, line, source))
        is not None
    ]


def literal_pattern(name: str) -> str | None:
    """从查询里抽出「像标识符的 token」，拼成 ripgrep 的候选正则；一个都没有时返回 `None`。

    `IDENTIFIER_TOKEN` 覆盖驼峰、点号路径、下划线（与 issue #23 的提取规则一致）。多个 token
    去重后按出现顺序用 `|` 连接（任一个命中都算候选）；每个 token 用 `re.escape` 转义：`.`/`$`
    在正则里有特殊含义，而这里要的是字面量。
    """
    tokens = list(
        dict.fromkeys(
            token for token in IDENTIFIER_TOKEN.findall(name) if len(token) >= IDENTIFIER_MIN_LEN
        )
    )

    if not tokens:
        return None

    return "|".join(re.escape(token) for token in tokens)


def summary_candidates(
    connection: sqlite3.Connection, text: str, *, path_prefix: str | None, source: str | None = None
) -> list[Candidate]:
    """摘要通道：查 `summaries_fts`（#7 建），命中时 `why` 含 `summary`（规格 §7）。

    表不存在（老索引、#7 未跑）时返回空、不报错。query 与入库时一样先过 jieba 分词：
    FTS5 的 `unicode61` 不切中文，不预分词的话两字词（「刷怪」）永远命中不了（规格 §6）。
    分词结果由 `fts_match_query` 拼成 `"词1" "词2"`：标点碎片被丢掉、每个词加引号，
    于是「刷怪，掉落」这种带中文标点的 query 照常命中，`"`/`*`/`-` 也不会把整路打成空。
    整路包 `try/except sqlite3.Error`：表不是 FTS5 时 SQL 会抛 `OperationalError`，那是
    「这一路没结果」，不是请求失败（规格 §8）。

    `source`（issue #24 规格 §5）过滤 `summaries.source`；老索引没有该列时不过滤（保持
    改动前的行为）。
    """
    try:
        if not query.has_table(connection, "summaries_fts"):
            return []

        match = fts_match_query(text)

        if not match:
            # query 全是标点：分词结果在 FTS5 里是零 token，这一路没有可匹配的词。
            return []

        sql = (
            "SELECT s.fqn AS fqn, s.text_zh AS text_zh, c.kind AS kind,"
            " f.path AS path, c.start_line AS start_line, c.end_line AS end_line"
            " FROM summaries_fts"
            " JOIN summaries s ON s.fqn = summaries_fts.fqn"
            " JOIN classes c ON c.id = s.class_id"
            " JOIN files f ON f.id = c.file_id"
            " WHERE summaries_fts MATCH ?"
        )
        params: list[object] = [match]

        if source is not None and query.has_column(connection, "summaries", "source"):
            sql += " AND s.source = ?"
            params.append(source)

        if path_prefix:
            sql += " AND substr(f.path, 1, length(?)) = ?"
            params.extend([path_prefix, path_prefix])

        sql += " ORDER BY bm25(summaries_fts) LIMIT ?"
        params.append(CHANNEL_LIMIT)

        # `fetchall()` 放在 try 里：游标是惰性的，迭代期间才真正执行 SQL。
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        # 规格明确的正常降级（summaries_fts 不是 FTS5 表）：表被外部改过时才会走到这里，
        # 打完整 traceback 的噪声大于信息量，只留 warn 与原因。
        logger.warning(
            "摘要通道查询失败（summaries_fts 不是 FTS5 表），该路返回空"
        )
        return []

    return [
        Candidate(
            channel="summary",
            path=row["path"],
            symbol=row["fqn"],
            kind=hit_type(row["kind"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            summary=row["text_zh"],
        )
        for row in rows
    ]


def patch_candidates(
    connection: sqlite3.Connection,
    text: str,
    *,
    path_prefix: str | None,
    source: str | None = None,
) -> list[Candidate]:
    """补丁通道（issue #24 规格 §5）：查 `patches_fts`（hunk 级），命中时 `why` 含 `patch`。

    `source` 传了 `patched`/`loader` 时这一路直接返回空（补丁只在 `patch` 来源里）；
    `source` 为 `patch` 或不传时照常查。`patches_fts` 不存在（原版索引）时静默返回空。
    命中给的是 hunk 级位置：`path` 是补丁文件路径，`target` 是被改的源文件，行区间是新行号。
    """
    if source is not None and source != "patch":
        return []

    if not query.has_table(connection, "patches_fts"):
        return []

    match = fts_match_query(text)

    if not match:
        return []

    has_patch_summaries = query.has_table(connection, "summaries") and query.has_column(
        connection, "summaries", "source"
    )
    summary_expr = "s.text_zh AS summary" if has_patch_summaries else "NULL AS summary"
    summary_join = (
        " LEFT JOIN summaries s ON s.fqn = p.path AND s.source = 'patch'"
        if has_patch_summaries
        else ""
    )

    sql = (
        f"SELECT p.path AS path, p.target AS target, p.context AS context,"
        f" p.old_start AS old_start, p.old_lines AS old_lines,"
        f" p.new_start AS new_start, p.new_lines AS new_lines, {summary_expr}"
        " FROM patches_fts"
        " JOIN patches p ON p.id = patches_fts.rowid"
        f"{summary_join}"
        " WHERE patches_fts MATCH ?"
    )
    params: list[object] = [match]

    if path_prefix:
        sql += " AND substr(p.path, 1, length(?)) = ?"
        params.extend([path_prefix, path_prefix])

    sql += " ORDER BY bm25(patches_fts) LIMIT ?"
    params.append(CHANNEL_LIMIT)

    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        logger.warning("补丁通道查询失败（patches_fts 不是 FTS5 表），该路返回空")
        return []

    return [_patch_candidate(row) for row in rows]


def _patch_candidate(row: sqlite3.Row) -> Candidate:
    """`patches` 行 → 候选：行区间取新行号（重建不出来时回退 `old_start`，见 `indexing.patches`）。
    """
    start = row["new_start"] if row["new_start"] is not None else row["old_start"]
    lines = row["new_lines"] if row["new_lines"] is not None else row["old_lines"]
    start_line = 1 if start is None else int(start)
    end_line = start_line + max(int(lines or 1), 1) - 1

    return Candidate(
        channel="patch",
        path=str(row["path"]),
        symbol=None,
        kind="patch",
        start_line=start_line,
        end_line=end_line,
        summary=None if row["summary"] is None else str(row["summary"]),
        target=str(row["target"]),
        patch_context=str(row["context"]),
    )


def _location_candidate(
    connection: sqlite3.Connection,
    channel: Channel,
    path: str,
    line: int,
    source: str | None = None,
) -> Candidate | None:
    """把「文件 + 行号」归一化成所在的最内层符号；没有符号时退化为 `type=file`。

    `source` 给定时按文件来源过滤（issue #24 规格 §5）：不属于该来源的文件直接丢弃（返回
    `None`）。
    """
    if source is not None and query.file_source(connection, path) != source:
        return None

    symbol = query.symbol_at(connection, path, line)

    if symbol is None:
        return Candidate(
            channel=channel,
            path=path,
            symbol=None,
            kind="file",
            start_line=line,
            end_line=line,
        )

    return Candidate(
        channel=channel,
        path=path,
        symbol=symbol.fqn,
        kind=hit_type(symbol.kind),
        start_line=symbol.start_line,
        end_line=symbol.end_line,
    )


def _ripgrep(root: Path, pattern: str, *, path_prefix: str | None) -> list[tuple[str, int]]:
    """跑 ripgrep，返回按「文件路径 + 行号」排序、去重后的 `(path, line)`。

    `path_prefix` 在候选阶段过滤（规格 §7）：先过滤再截断到 `CHANNEL_LIMIT`。
    """
    binary = shutil.which(RG_BINARY)

    if binary is None:
        logger.warning("ripgrep 不可用（%s 不在 PATH），literal 通道返回空", RG_BINARY)
        return []

    try:
        completed = subprocess.run(
            [binary, *RG_ARGS, "--", pattern, "."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RG_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ripgrep 超时（>%.0f 秒），literal 通道返回空", RG_TIMEOUT_S)
        return []
    except OSError:
        logger.warning("ripgrep 启动失败，literal 通道返回空", exc_info=True)
        return []

    # 退出码 1 是「没有匹配」，不算错误；其余非 0（含非法正则）都降级为空。
    if completed.returncode not in (0, 1):
        logger.warning(
            "ripgrep 退出码 %s，literal 通道返回空：%s",
            completed.returncode,
            completed.stderr.strip(),
        )
        return []

    hits = sorted(set(_parse_rg_json(completed.stdout)))

    if path_prefix:
        hits = [hit for hit in hits if hit[0].startswith(path_prefix)]

    return hits[:CHANNEL_LIMIT]


def _parse_rg_json(stdout: str) -> list[tuple[str, int]]:
    """解析 `ripgrep --json` 的行式输出，只取 `type == "match"` 的文件路径与行号。"""
    hits: list[tuple[str, int]] = []

    for raw in stdout.splitlines():
        if not raw.strip():
            continue

        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict) or event.get("type") != "match":
            continue

        data = event.get("data")

        if not isinstance(data, dict):
            continue

        path = _rg_text(data.get("path"))
        line = data.get("line_number")

        if path is None or not isinstance(line, int):
            continue

        # `cwd=root` + 参数 `.` 时路径带 `./`（Windows 上是 `.\`）前缀且用反斜杠，
        # 归一成与 `files.path` 一致的 POSIX 相对路径。
        path = path.replace("\\", "/")

        if path.startswith("./"):
            path = path[2:]

        hits.append((path, line))

    return hits


def _rg_text(field: object) -> str | None:
    """ripgrep 的字符串字段是 `{"text": ...}` 或 `{"bytes": base64}`。"""
    if not isinstance(field, dict):
        return None

    text = field.get("text")

    if isinstance(text, str):
        return text

    return None


def param_candidates(
    connection: sqlite3.Connection,
    name: str,
    *,
    path_prefix: str | None,
    source: str | None = None,
) -> list[Candidate]:
    """参数通道：按参数名找所属方法（Parchment 名与原名都参与匹配）。

    老索引没有 `params` 表时返回空（与 `source` 列的守卫同一套思路），不报错。
    每个方法只出一次（同一个方法可能有多个参数同名？取名字命中的第一条）。
    """
    if not query.has_table(connection, "params"):
        return []

    sql = (
        "SELECT c.fqn AS fqn, c.kind AS kind, f.path AS path,"
        " m.name AS method_name, m.start_line AS start_line, m.end_line AS end_line,"
        " p.name AS param_name, p.original AS param_original"
        " FROM params p"
        " JOIN methods m ON m.id = p.method_id"
        " JOIN classes c ON c.id = m.class_id"
        " JOIN files f ON f.id = c.file_id"
        " WHERE (p.name = ? COLLATE NOCASE OR p.original = ? COLLATE NOCASE)"
    )
    params: list[object] = [name, name]

    if source is not None and query.has_column(connection, "params", "source"):
        sql += " AND p.source = ?"
        params.append(source)

    if path_prefix:
        sql += " AND substr(f.path, 1, length(?)) = ?"
        params.extend([path_prefix, path_prefix])

    sql += " ORDER BY p.id LIMIT ?"
    params.append(CHANNEL_LIMIT)

    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        logger.warning("参数通道查询失败，该路返回空")

        return []

    seen: set[str] = set()
    candidates: list[Candidate] = []

    for row in rows:
        symbol = f"{row['fqn']}#{row['method_name']}"

        if symbol in seen:
            continue

        seen.add(symbol)
        candidates.append(
            Candidate(
                channel="param",
                path=row["path"],
                symbol=symbol,
                kind=hit_type(row["kind"]),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                summary=f"参数 {row['param_name']}"
                + (f"（原名 {row['param_original']}）" if row["param_original"] else ""),
            )
        )

    return candidates
