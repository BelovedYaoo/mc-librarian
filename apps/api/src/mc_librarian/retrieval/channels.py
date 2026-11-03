"""三路召回通道 + 摘要通道接口（issue #6 规格 §2）。

| 通道 | 数据源 | 排序依据 |
|---|---|---|
| `symbol` | 索引库的 `classes`/`methods`/`fields` | 复用 #5 的四级匹配，同级别按 `fqn` 字典序 |
| `literal` | ripgrep 子进程扫 `<data_dir>/src/<version>/` | 文件路径 + 行号 |
| `reference` | `refs` 表反查引用点 | `path, line` |
| `summary` | `summaries` 表（FTS5，由 #7 建） | bm25 升序；表不存在时返回空 |

`literal` / `reference` 拿到的是「文件 + 行号」，统一经 `query.symbol_at` 归一化成所在的最内层
符号：同一实体被多路召回时才能被 RRF 累加，`why` 也才会有多个值（规格 §1）。任何一路为空
（含 ripgrep 不可用 / 超时、`summaries` 表不存在或不是 FTS5、query 含 FTS 语法）都不算错误
（规格 §8）。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from mc_librarian.indexing import query
from mc_librarian.schemas import HitType, SymbolKind

logger = logging.getLogger("mc_librarian.retrieval")

Channel = Literal["summary", "symbol", "literal", "reference"]

# 每路各取前 50 条候选（规格 §1）。
CHANNEL_LIMIT: Final = 50

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


@dataclass(frozen=True)
class Candidate:
    """一路候选；`symbol` 为 None 表示没有任何符号覆盖这个位置（`type=file`）。"""

    channel: Channel
    path: str
    symbol: str | None
    kind: HitType
    start_line: int
    end_line: int
    summary: str | None = None

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
) -> list[Candidate]:
    """符号通道：复用 #5 的四级匹配（fqn → simple → 后缀 → 编辑距离）。"""
    hits = query.find_symbols(
        connection,
        name,
        fuzzy=True,
        kind=kind,
        limit=CHANNEL_LIMIT,
        path_prefix=path_prefix,
    )

    return [
        Candidate(
            channel="symbol",
            path=hit.path,
            symbol=hit.fqn,
            kind=hit.kind,
            start_line=hit.start_line,
            end_line=hit.end_line,
        )
        for hit in hits
    ]


def reference_candidates(
    connection: sqlite3.Connection, name: str, *, path_prefix: str | None
) -> list[Candidate]:
    """引用通道：query 命中某个符号名时，反查 `refs` 表里的引用点。"""
    hits = query.find_references(
        connection, name, limit=CHANNEL_LIMIT, path_prefix=path_prefix
    )

    return [
        _location_candidate(connection, "reference", hit.path, hit.line) for hit in hits
    ]


def literal_candidates(
    connection: sqlite3.Connection,
    source_root: Path,
    name: str,
    *,
    path_prefix: str | None,
) -> list[Candidate]:
    """字面量通道：ripgrep 子进程扫源码树，超时 / 不可用时返回空并写 warn。"""
    return [
        _location_candidate(connection, "literal", path, line)
        for path, line in _ripgrep(source_root, name, path_prefix=path_prefix)
    ]


def summary_candidates(
    connection: sqlite3.Connection, text: str, *, path_prefix: str | None
) -> list[Candidate]:
    """摘要通道接口：`summaries` 表由 #7 建，不存在时返回空、不报错（规格 §2）。

    表在时按文档 schema（`summaries(fqn, text_zh, keywords, tokens)`）做 FTS5 查询，
    bm25 升序（越负越相关，排在前）。整路包 `try/except sqlite3.Error`：表不是 FTS5、
    或 query 含 FTS 语法（`"`、`*`、`-`、`AND`……）时 SQL 会抛 `OperationalError`，
    那是「这一路没结果」，不是请求失败（规格 §8）。
    """
    try:
        if not query.has_table(connection, "summaries"):
            return []

        sql = (
            "SELECT summaries.fqn AS fqn, summaries.text_zh AS text_zh, c.kind AS kind,"
            " f.path AS path, c.start_line AS start_line, c.end_line AS end_line"
            " FROM summaries JOIN classes c ON c.fqn = summaries.fqn"
            " JOIN files f ON f.id = c.file_id"
            " WHERE summaries MATCH ?"
        )
        params: list[object] = [text]

        if path_prefix:
            sql += " AND substr(f.path, 1, length(?)) = ?"
            params.extend([path_prefix, path_prefix])

        sql += " ORDER BY bm25(summaries) LIMIT ?"
        params.append(CHANNEL_LIMIT)

        # `fetchall()` 放在 try 里：游标是惰性的，迭代期间才真正执行 SQL。
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        logger.warning(
            "摘要通道查询失败（summaries 不是 FTS5 表，或 query 含 FTS 语法），该路返回空",
            exc_info=True,
        )
        return []

    return [
        Candidate(
            channel="summary",
            path=row["path"],
            symbol=row["fqn"],
            kind=row["kind"],
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            summary=row["text_zh"],
        )
        for row in rows
    ]


def _location_candidate(
    connection: sqlite3.Connection, channel: Channel, path: str, line: int
) -> Candidate:
    """把「文件 + 行号」归一化成所在的最内层符号；没有符号时退化为 `type=file`。"""
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
        kind=symbol.kind,
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
