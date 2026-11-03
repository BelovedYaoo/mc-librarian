"""索引库的只读查询：符号查找、反查引用、文件骨架（规格 §6/§7/§9）。

一律以 `mode=ro` 打开（`docs/00-overview.md` §4 的硬约束 1）：连接按请求创建、用完即关，
不共享连接。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from mc_librarian.indexing.java import CLINIT_NAME
from mc_librarian.schemas import ClassKind, MemberKind, ReferenceKind, SymbolDetailKind

# 符号查找的返回上限（规格 §6）。
SYMBOL_LIMIT: Final = 50
# 模糊匹配的编辑距离上限（规格 §6）。
FUZZY_DISTANCE: Final = 2

SymbolTable = str
Predicate = Callable[[str], "tuple[str, list[object]]"]

# 三类符号各自的投影：`fqn` 对方法是 `Class#method`、对字段是 `Class#field`，
# `simple` 是裸名字（规格 §6 的匹配级别就建在这两列上）。
_SYMBOL_SELECTS: Final[dict[str, str]] = {
    "class": (
        "SELECT 'class' AS kind, c.fqn AS fqn, c.simple AS simple, f.path AS path,"
        " c.signature AS signature, c.superclass AS superclass, c.interfaces AS interfaces,"
        " c.annotations AS annotations, c.start_line AS start_line, c.end_line AS end_line,"
        " c.id AS class_id FROM classes c JOIN files f ON f.id = c.file_id"
    ),
    "method": (
        "SELECT 'method' AS kind, c.fqn || '#' || m.name AS fqn, m.name AS simple,"
        " f.path AS path, m.signature AS signature, NULL AS superclass, '[]' AS interfaces,"
        " m.annotations AS annotations, m.start_line AS start_line, m.end_line AS end_line,"
        " c.id AS class_id FROM methods m JOIN classes c ON c.id = m.class_id"
        " JOIN files f ON f.id = c.file_id"
    ),
    "field": (
        "SELECT 'field' AS kind, c.fqn || '#' || fd.name AS fqn, fd.name AS simple,"
        " f.path AS path, NULL AS signature, NULL AS superclass, '[]' AS interfaces,"
        " fd.annotations AS annotations, fd.start_line AS start_line, fd.end_line AS end_line,"
        " c.id AS class_id FROM fields fd JOIN classes c ON c.id = fd.class_id"
        " JOIN files f ON f.id = c.file_id"
    ),
}

_ALL_TABLES: Final[tuple[str, ...]] = ("class", "method", "field")


@dataclass(frozen=True)
class SymbolHit:
    """一条符号命中；`class_id` 只在 `kind == 'class'` 时有意义（用于取 members）。"""

    kind: SymbolDetailKind
    fqn: str
    simple: str
    path: str
    signature: str | None
    start_line: int
    end_line: int
    superclass: str | None
    interfaces: list[str]
    annotations: list[str]
    class_id: int


@dataclass(frozen=True)
class MemberHit:
    """类的一个成员（方法或字段）。"""

    kind: MemberKind
    name: str
    signature: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ReferenceHit:
    """一条引用反查结果。"""

    path: str
    line: int
    kind: ReferenceKind
    enclosing: str


@dataclass(frozen=True)
class SymbolAt:
    """某个（文件, 行号）所在的最内层符号；literal / reference 通道的归一化目标。"""

    kind: SymbolDetailKind
    fqn: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class OutlineMethod:
    name: str
    signature: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class OutlineClass:
    fqn: str
    kind: ClassKind
    signature: str | None
    start_line: int
    end_line: int
    methods: list[OutlineMethod]


@dataclass(frozen=True)
class FileRecord:
    id: int
    package: str | None


def open_readonly(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """只读打开索引库（规格 §9）。

    `check_same_thread=False` 给 `/v1/ask`：它的工具调用跑在 `asyncio.to_thread` 的工作线程里，
    而连接是在事件循环线程上建的（同一请求内串行使用，不跨请求共享）。同步端点每次都在
    同一个线程池线程里建/用/关连接，用默认的 `True` 就够。
    """
    connection = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row

    return connection


# ----------------------------------------------------------------------
# 符号查找（规格 §6）
# ----------------------------------------------------------------------


def find_symbols(
    connection: sqlite3.Connection,
    name: str,
    *,
    fuzzy: bool,
    kind: str,
    limit: int = SYMBOL_LIMIT,
    path_prefix: str | None = None,
) -> list[SymbolHit]:
    """按「fqn → simple → 后缀 → 编辑距离」四级依次尝试，先命中先返回，不跨级别混合。

    `fuzzy=false` 只做前两级；`fuzzy=true` 才启用后缀与编辑距离降级。
    `path_prefix` 在候选阶段下推到 SQL（issue #6 规格 §7）：先按前缀过滤再取 `limit`，
    避免「融合后过滤」把 top-k 打散。
    """
    tables = (kind,) if kind in _SYMBOL_SELECTS else _ALL_TABLES

    for level in (_level_fqn, _level_simple):
        hits = level(connection, tables, name, limit, path_prefix)

        if hits:
            return hits

    if not fuzzy:
        return []

    for level in (_level_suffix, _level_fuzzy):
        hits = level(connection, tables, name, limit, path_prefix)

        if hits:
            return hits

    return []


def _path_condition(alias: str, prefix: str | None) -> tuple[str, list[object]]:
    """`path_prefix` 的 SQL 片段：前缀比较用 `substr`，避免 `LIKE` 的 `%`/`_` 转义问题。"""
    if not prefix:
        return "", []

    return f" AND substr({alias}.path, 1, length(?)) = ?", [prefix, prefix]


def _fqn_expr(table: str) -> str:
    if table == "class":
        return "c.fqn"

    if table == "method":
        return "c.fqn || '#' || m.name"

    return "c.fqn || '#' || fd.name"


def _simple_expr(table: str) -> str:
    if table == "class":
        return "c.simple"

    if table == "method":
        return "m.name"

    return "fd.name"


def _run(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
    build: Predicate,
    limit: int,
    path_prefix: str | None,
) -> list[SymbolHit]:
    hits: list[SymbolHit] = []
    path_where, path_params = _path_condition("f", path_prefix)

    for table in tables:
        where, params = build(table)
        rows = connection.execute(
            f"{_SYMBOL_SELECTS[table]} WHERE {where}{path_where} ORDER BY fqn LIMIT ?",
            [*params, *path_params, limit],
        ).fetchall()
        hits.extend(_row_to_hit(row) for row in rows)

    hits.sort(key=lambda hit: hit.fqn)

    return hits[:limit]


def _level_fqn(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
    name: str,
    limit: int,
    path_prefix: str | None,
) -> list[SymbolHit]:
    return _run(
        connection, tables, lambda table: (f"{_fqn_expr(table)} = ?", [name]), limit, path_prefix
    )


def _level_simple(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
    name: str,
    limit: int,
    path_prefix: str | None,
) -> list[SymbolHit]:
    return _run(
        connection,
        tables,
        lambda table: (f"{_simple_expr(table)} = ?", [name]),
        limit,
        path_prefix,
    )


def _level_suffix(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
    name: str,
    limit: int,
    path_prefix: str | None,
) -> list[SymbolHit]:
    # 以 `.` 为边界的后缀匹配：`substr` 比较避免 `LIKE` 的 `%`/`_` 转义问题。
    return _run(
        connection,
        tables,
        lambda table: (f"substr({_fqn_expr(table)}, -(length(?) + 1)) = '.' || ?", [name, name]),
        limit,
        path_prefix,
    )


def _level_fuzzy(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
    name: str,
    limit: int,
    path_prefix: str | None,
) -> list[SymbolHit]:
    hits: list[SymbolHit] = []
    path_where, path_params = _path_condition("f", path_prefix)

    for table in tables:
        rows = connection.execute(
            f"{_SYMBOL_SELECTS[table]} WHERE length({_simple_expr(table)}) BETWEEN ? AND ?"
            f"{path_where}",
            [len(name) - FUZZY_DISTANCE, len(name) + FUZZY_DISTANCE, *path_params],
        ).fetchall()

        for row in rows:
            hit = _row_to_hit(row)

            if _edit_distance(hit.simple, name) <= FUZZY_DISTANCE:
                hits.append(hit)

    hits.sort(key=lambda hit: hit.fqn)

    return hits[:limit]


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein 距离；超过 `FUZZY_DISTANCE` 就提前返回，不再精确计算。"""
    if left == right:
        return 0

    if abs(len(left) - len(right)) > FUZZY_DISTANCE:
        return FUZZY_DISTANCE + 1

    previous = list(range(len(right) + 1))

    for i, left_char in enumerate(left, start=1):
        current = [i]

        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            )

        if min(current) > FUZZY_DISTANCE:
            return FUZZY_DISTANCE + 1

        previous = current

    return previous[-1]


def _row_to_hit(row: sqlite3.Row) -> SymbolHit:
    return SymbolHit(
        kind=row["kind"],
        fqn=row["fqn"],
        simple=row["simple"],
        path=row["path"],
        signature=row["signature"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        superclass=row["superclass"],
        interfaces=_load_list(row["interfaces"]),
        annotations=_load_list(row["annotations"]),
        class_id=int(row["class_id"]),
    )


def class_members(connection: sqlite3.Connection, class_id: int) -> list[MemberHit]:
    """类的成员：方法（不含 `<clinit>` 伪方法）与字段，按行号排序。"""
    members: list[MemberHit] = []

    for row in connection.execute(
        "SELECT name, signature, start_line, end_line FROM methods"
        " WHERE class_id = ? AND name <> ? ORDER BY start_line, name",
        (class_id, CLINIT_NAME),
    ):
        members.append(
            MemberHit(
                kind="method",
                name=row["name"],
                signature=row["signature"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
            )
        )

    for row in connection.execute(
        "SELECT name, type, start_line, end_line FROM fields"
        " WHERE class_id = ? ORDER BY start_line, name",
        (class_id,),
    ):
        members.append(
            MemberHit(
                kind="field",
                name=row["name"],
                signature=row["type"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
            )
        )

    return members


# ----------------------------------------------------------------------
# 反查引用（规格 §5）
# ----------------------------------------------------------------------


def find_references(
    connection: sqlite3.Connection,
    symbol: str,
    *,
    limit: int,
    path_prefix: str | None = None,
) -> list[ReferenceHit]:
    """反查引用。`symbol` 是方法/字段名，或 `Class#member`（按引用方所在类的简单名过滤）。

    `path_prefix` 在候选阶段下推到 SQL（issue #6 规格 §7）。
    """
    class_simple: str | None = None
    target = symbol

    if "#" in symbol:
        class_simple, _, target = symbol.partition("#")

    sql = (
        "SELECT f.path AS path, r.line AS line, r.kind AS kind,"
        " c.fqn AS class_fqn, m.name AS method_name"
        " FROM refs r JOIN methods m ON m.id = r.from_method_id"
        " JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id"
        " WHERE r.target_name = ?"
    )
    params: list[object] = [target]

    if class_simple:
        sql += " AND c.simple = ?"
        params.append(class_simple)

    path_where, path_params = _path_condition("f", path_prefix)
    sql += path_where
    params.extend(path_params)

    sql += " ORDER BY f.path, r.line, c.fqn, m.name LIMIT ?"
    params.append(limit)

    return [
        ReferenceHit(
            path=row["path"],
            line=int(row["line"]),
            kind=row["kind"],
            enclosing=f"{row['class_fqn']}#{row['method_name']}",
        )
        for row in connection.execute(sql, params)
    ]


# ----------------------------------------------------------------------
# 位置 → 最内层符号（issue #6 的 literal / reference 通道共用）
# ----------------------------------------------------------------------


def symbol_at(connection: sqlite3.Connection, path: str, line: int) -> SymbolAt | None:
    """返回覆盖 `line` 的**最内层**符号（类 / 方法 / 字段）。

    literal 与 reference 通道拿到的只有「文件 + 行号」，融合前统一解析成所在符号，
    同一实体才能被多路召回并累加 `why`（issue #6 规格 §1）。`<clinit>` 是伪方法
    （行号退化到类声明行），排除在外，否则它会盖住类本身。
    """
    row = connection.execute(
        "SELECT kind, fqn, start_line, end_line FROM ("
        " SELECT c.kind AS kind, c.fqn AS fqn, c.start_line AS start_line,"
        "  c.end_line AS end_line, 0 AS depth"
        "  FROM classes c JOIN files f ON f.id = c.file_id"
        "  WHERE f.path = ? AND c.start_line <= ? AND c.end_line >= ?"
        " UNION ALL"
        " SELECT 'method' AS kind, c.fqn || '#' || m.name AS fqn, m.start_line AS start_line,"
        "  m.end_line AS end_line, 1 AS depth"
        "  FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id"
        "  WHERE f.path = ? AND m.name <> ? AND m.start_line <= ? AND m.end_line >= ?"
        " UNION ALL"
        " SELECT 'field' AS kind, c.fqn || '#' || fd.name AS fqn, fd.start_line AS start_line,"
        "  fd.end_line AS end_line, 2 AS depth"
        "  FROM fields fd JOIN classes c ON c.id = fd.class_id JOIN files f ON f.id = c.file_id"
        "  WHERE f.path = ? AND fd.start_line <= ? AND fd.end_line >= ?"
        ")"
        " ORDER BY (end_line - start_line) ASC, depth ASC, fqn ASC LIMIT 1",
        (
            path,
            line,
            line,
            path,
            CLINIT_NAME,
            line,
            line,
            path,
            line,
            line,
        ),
    ).fetchone()

    if row is None:
        return None

    return SymbolAt(
        kind=row["kind"],
        fqn=row["fqn"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
    )


def has_table(connection: sqlite3.Connection, name: str) -> bool:
    """索引库里是否存在某张表。摘要通道据此在 `summaries` 尚未落地时静默返回空。"""
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def count_symbol_names(connection: sqlite3.Connection, names: list[str]) -> dict[str, int]:
    """每个名字在索引库里出现的次数（类简单名 + 方法名 + 字段名），供 `suggestions` 排序。"""
    if not names:
        return {}

    placeholders = ",".join("?" for _ in names)
    sql = (
        f"SELECT name, COUNT(*) AS count FROM ("
        f" SELECT simple AS name FROM classes WHERE simple IN ({placeholders})"
        f" UNION ALL SELECT name FROM methods WHERE name IN ({placeholders})"
        f" UNION ALL SELECT name FROM fields WHERE name IN ({placeholders})"
        f") GROUP BY name"
    )

    return {
        row["name"]: int(row["count"])
        for row in connection.execute(sql, [*names, *names, *names])
    }


# ----------------------------------------------------------------------
# 文件骨架（规格 §7）
# ----------------------------------------------------------------------


def find_file(connection: sqlite3.Connection, path: str) -> FileRecord | None:
    row = connection.execute(
        "SELECT id, pkg FROM files WHERE path = ?", (path,)
    ).fetchone()

    return None if row is None else FileRecord(id=int(row["id"]), package=row["pkg"])


def file_outline(connection: sqlite3.Connection, file_id: int) -> list[OutlineClass]:
    """文件里的所有类（含内部类，按行号排序），每个类带方法签名与行号；不含方法体。"""
    classes: list[OutlineClass] = []

    for row in connection.execute(
        "SELECT id, fqn, kind, signature, start_line, end_line FROM classes"
        " WHERE file_id = ? ORDER BY start_line, fqn",
        (file_id,),
    ):
        methods = [
            OutlineMethod(
                name=method["name"],
                signature=method["signature"],
                start_line=int(method["start_line"]),
                end_line=int(method["end_line"]),
            )
            for method in connection.execute(
                "SELECT name, signature, start_line, end_line FROM methods"
                " WHERE class_id = ? AND name <> ? ORDER BY start_line, name",
                (row["id"], CLINIT_NAME),
            )
        ]
        classes.append(
            OutlineClass(
                fqn=row["fqn"],
                kind=row["kind"],
                signature=row["signature"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                methods=methods,
            )
        )

    return classes


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
