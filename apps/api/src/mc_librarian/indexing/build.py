"""把源码树解析成 `db/<version>.sqlite`（规格补充 §1/§3/§4/§14）。

原子写入：先在同目录写一个临时文件，全部文件解析完、DDL 与数据都落盘后再 `os.replace`
替换目标——服务端永远读不到半成品（`docs/00-overview.md` §4 的硬约束 1）。

失败降级（规格 §4）：单个文件读不了或解析失败就跳过并记一条 `warn` 日志；失败率超过
`MAX_PARSE_FAILURE_RATIO` 则整体失败，说明环境有问题（例如 tree-sitter 语法版本不匹配）。

另提供 `read_index_totals`：只读读回已有索引库的统计，供流水线复用已有索引与 `index` CLI 汇总。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from mc_librarian.indexing import java
from mc_librarian.indexing.text import count_lines, count_tokens
from mc_librarian.schemas import LogLevel

JAVA_SUFFIX: Final = ".java"
JSON_SUFFIX: Final = ".json"
DATA_DIR_NAME: Final = "data"

# 单文件解析失败率超过这个比例就整体失败（规格 §4）。
MAX_PARSE_FAILURE_RATIO: Final = 0.2

# 规格 §1 的 schema。`classes(fqn)` 由 UNIQUE 约束隐式建索引，不再重复建一条。
SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE files (
      id     INTEGER PRIMARY KEY,
      path   TEXT NOT NULL UNIQUE,
      pkg    TEXT,
      loc    INTEGER NOT NULL DEFAULT 0,
      tokens INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE classes (
      id          INTEGER PRIMARY KEY,
      file_id     INTEGER NOT NULL REFERENCES files (id),
      fqn         TEXT NOT NULL UNIQUE,
      simple      TEXT NOT NULL,
      kind        TEXT NOT NULL,
      signature   TEXT,
      superclass  TEXT,
      interfaces  TEXT NOT NULL DEFAULT '[]',
      annotations TEXT NOT NULL DEFAULT '[]',
      start_line  INTEGER NOT NULL,
      end_line    INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE methods (
      id          INTEGER PRIMARY KEY,
      class_id    INTEGER NOT NULL REFERENCES classes (id),
      name        TEXT NOT NULL,
      signature   TEXT,
      return_type TEXT,
      params      TEXT NOT NULL DEFAULT '[]',
      modifiers   TEXT NOT NULL DEFAULT '[]',
      annotations TEXT NOT NULL DEFAULT '[]',
      start_line  INTEGER NOT NULL,
      end_line    INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE fields (
      id          INTEGER PRIMARY KEY,
      class_id    INTEGER NOT NULL REFERENCES classes (id),
      name        TEXT NOT NULL,
      type        TEXT,
      modifiers   TEXT NOT NULL DEFAULT '[]',
      annotations TEXT NOT NULL DEFAULT '[]',
      start_line  INTEGER NOT NULL,
      end_line    INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE refs (
      id             INTEGER PRIMARY KEY,
      from_method_id INTEGER NOT NULL REFERENCES methods (id),
      target_name    TEXT NOT NULL,
      kind           TEXT NOT NULL,
      line           INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_classes_simple ON classes (simple)",
    "CREATE INDEX idx_methods_name ON methods (name)",
    "CREATE INDEX idx_methods_class ON methods (class_id)",
    "CREATE INDEX idx_fields_name ON fields (name)",
    "CREATE INDEX idx_refs_target ON refs (target_name)",
)


class BuildError(RuntimeError):
    """索引构建可预期的失败；`jobs.runner` 会把它翻成 `StageError`。"""


@dataclass(frozen=True)
class BuildResult:
    """一次构建的汇总；前三个字段与 `#4` 的 checkpoint 口径一致。"""

    files: int
    tokens: int
    source_size_bytes: int
    parsed: int
    skipped: int


@dataclass(frozen=True)
class IndexTotals:
    """已有索引库的统计（`mc-librarian index` 复用索引与汇总时读）。"""

    files: int
    java_files: int
    lines: int
    tokens: int


@dataclass(frozen=True)
class _FileRecord:
    relative: str
    package: str | None
    loc: int
    tokens: int
    parsed: java.ParsedFile | None


def iter_source_files(root: Path) -> Iterator[Path]:
    """索引范围内的文件（规格 §3）：`*.java`（递归）与 `data/**/*.json`。

    白名单之外一律不进，因此 `assets/`（不在白名单内）天然被排除。
    """
    seen: set[Path] = set()

    for path in sorted(root.rglob(f"*{JAVA_SUFFIX}")):
        if path.is_file():
            seen.add(path)
            yield path

    data_root = root / DATA_DIR_NAME

    if not data_root.is_dir():
        return

    for path in sorted(data_root.rglob(f"*{JSON_SUFFIX}")):
        if path.is_file() and path not in seen:
            yield path


def read_index_totals(db_path: Path) -> IndexTotals | None:
    """只读打开已有索引库读出统计；打不开或缺 `files` 表（0 字节文件/截断文件）返回 `None`。

    调用方把 `None` 当作「没有可用索引」：流水线会正常重建，而不是把坏文件登记成版本。
    与 `api.v1.admin_versions.read_index_stats` 的分工：那个给注册接口用（还要 `summaries`
    标记），这个给流水线复用已有索引与 CLI 汇总用（还要行数）。
    """
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None

    try:
        has_files = connection.execute(
            "SELECT 1 AS hit FROM sqlite_master WHERE type = 'table' AND name = 'files'"
        ).fetchone()

        if has_files is None:
            return None

        row = connection.execute(
            "SELECT COUNT(*) AS files,"
            " COALESCE(SUM(CASE WHEN path LIKE '%.java' THEN 1 ELSE 0 END), 0) AS java_files,"
            " COALESCE(SUM(loc), 0) AS lines,"
            " COALESCE(SUM(tokens), 0) AS tokens FROM files"
        ).fetchone()
    except sqlite3.DatabaseError:
        # 文件存在但不是 sqlite（或已损坏）：当作没有可用索引。
        return None
    finally:
        connection.close()

    if row is None:
        return None

    return IndexTotals(
        files=int(row[0]),
        java_files=int(row[1]),
        lines=int(row[2]),
        tokens=int(row[3]),
    )


def place_source_tree(source_dir: Path, dest: Path) -> None:
    """把源树就位到 `<data_dir>/src/<version>/`（规格补充二 §14）。

    已经是该目录就什么都不做；否则整体复制过去。拒绝互相嵌套的源/目标，
    否则 `copytree` 会把自己复制进自己。
    """
    source = source_dir.resolve()
    target = dest.resolve()

    if source == target:
        return

    if target.is_relative_to(source) or source.is_relative_to(target):
        raise BuildError(f"源目录与就位目录互相嵌套，拒绝复制：{source} → {target}")

    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copytree(source, target, dirs_exist_ok=True)
    except OSError as error:
        raise BuildError(f"源码树就位失败：{error}") from error


def source_tree_size_bytes(root: Path) -> int:
    """`root` 下索引范围内的文件总字节数（与 `build_index` 的 `source_size_bytes` 同口径）。

    `root` 不存在时返回 0。供 `POST /v1/admin/versions` 注册时统计源码树体积——
    与任务产出路径写的是同一个量（源码树字节数，不是索引 sqlite 的大小）。
    """
    total = 0

    for path in iter_source_files(root):
        try:
            total += path.stat().st_size
        except OSError:
            continue

    return total


def build_index(
    root: Path, db_path: Path, *, log: Callable[[LogLevel, str], None]
) -> BuildResult:
    """解析 `root` 下的源码并写出 `db_path`；返回汇总统计。"""
    records: list[_FileRecord] = []
    java_total = 0
    parsed = 0
    skipped = 0
    total_tokens = 0
    total_size = 0

    for path in iter_source_files(root):
        relative = path.relative_to(root).as_posix()

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            size_bytes = path.stat().st_size
        except OSError as error:
            skipped += 1
            log("warn", f"parse：跳过 {relative}（{error}）")
            continue

        tokens = count_tokens(text)
        total_tokens += tokens
        total_size += size_bytes
        parsed_file: java.ParsedFile | None = None

        if path.suffix == JAVA_SUFFIX:
            java_total += 1

            try:
                parsed_file = java.parse_java(text, path=relative)
            except Exception as error:  # tree-sitter 的解析失败：跳过这个文件
                skipped += 1
                log("warn", f"parse：跳过 {relative}（{error}）")
                continue

            parsed += 1

        records.append(
            _FileRecord(
                relative=relative,
                package=parsed_file.package if parsed_file is not None else None,
                loc=count_lines(text),
                tokens=tokens,
                parsed=parsed_file,
            )
        )

    if java_total and skipped / java_total > MAX_PARSE_FAILURE_RATIO:
        raise BuildError(
            f"解析失败率过高：{skipped}/{java_total} 个 .java 文件失败"
            f"（阈值 {MAX_PARSE_FAILURE_RATIO:.0%}），请检查 tree-sitter 语法包是否匹配"
        )

    _write_index(db_path, records)

    return BuildResult(
        files=len(records),
        tokens=total_tokens,
        source_size_bytes=total_size,
        parsed=parsed,
        skipped=skipped,
    )


def _write_index(db_path: Path, records: list[_FileRecord]) -> None:
    """建库 + 写数据 + 原子替换。临时文件与目标同目录，`os.replace` 才是原子的。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp = db_path.parent / f".{db_path.name}.{os.getpid()}.tmp"
    temp.unlink(missing_ok=True)

    connection = sqlite3.connect(temp)

    try:
        # 一次性批量写入：关掉回滚日志与同步，构建中途崩溃只影响这个临时文件。
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")

        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)

        _insert_records(connection, records)
        connection.commit()
    except BaseException:
        connection.close()
        temp.unlink(missing_ok=True)
        raise
    else:
        connection.close()

    os.replace(temp, db_path)


def _insert_records(connection: sqlite3.Connection, records: list[_FileRecord]) -> None:
    for record in records:
        cursor = connection.execute(
            "INSERT INTO files (path, pkg, loc, tokens) VALUES (?, ?, ?, ?)",
            (record.relative, record.package, record.loc, record.tokens),
        )

        if record.parsed is None:
            continue

        file_id = int(cursor.lastrowid or 0)

        for parsed_class in record.parsed.classes:
            class_id = _insert_class(connection, file_id, parsed_class)

            for method in parsed_class.methods:
                method_cursor = connection.execute(
                    "INSERT INTO methods (class_id, name, signature, return_type, params,"
                    " modifiers, annotations, start_line, end_line)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        class_id,
                        method.name,
                        method.signature,
                        method.return_type,
                        _dumps(method.params),
                        _dumps(method.modifiers),
                        _dumps(method.annotations),
                        method.start_line,
                        method.end_line,
                    ),
                )
                _insert_refs(connection, int(method_cursor.lastrowid or 0), method.refs)

            for parsed_field in parsed_class.fields:
                connection.execute(
                    "INSERT INTO fields (class_id, name, type, modifiers, annotations,"
                    " start_line, end_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        class_id,
                        parsed_field.name,
                        parsed_field.type,
                        _dumps(parsed_field.modifiers),
                        _dumps(parsed_field.annotations),
                        parsed_field.start_line,
                        parsed_field.end_line,
                    ),
                )


def _insert_class(
    connection: sqlite3.Connection, file_id: int, parsed_class: java.ParsedClass
) -> int:
    cursor = connection.execute(
        "INSERT INTO classes (file_id, fqn, simple, kind, signature, superclass, interfaces,"
        " annotations, start_line, end_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            parsed_class.fqn,
            parsed_class.simple,
            parsed_class.kind,
            parsed_class.signature,
            parsed_class.superclass,
            _dumps(parsed_class.interfaces),
            _dumps(parsed_class.annotations),
            parsed_class.start_line,
            parsed_class.end_line,
        ),
    )

    return int(cursor.lastrowid or 0)


def _insert_refs(
    connection: sqlite3.Connection, method_id: int, refs: list[java.ParsedRef]
) -> None:
    """同一方法内相同的 `(target_name, kind)` 只留一条，取最小行号（规格 §5）。"""
    best: dict[tuple[str, str], int] = {}

    for ref in refs:
        key = (ref.target_name, ref.kind)

        if key not in best or ref.line < best[key]:
            best[key] = ref.line

    for (target_name, kind), line in sorted(best.items(), key=lambda item: (item[1], item[0])):
        connection.execute(
            "INSERT INTO refs (from_method_id, target_name, kind, line) VALUES (?, ?, ?, ?)",
            (method_id, target_name, kind, line),
        )


def _dumps(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)
