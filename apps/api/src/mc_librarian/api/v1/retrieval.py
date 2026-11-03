"""检索与阅读端点：`POST /v1/search`、`/v1/symbol`、`/v1/references`、`/v1/outline`、`/v1/read`。

规格见 issue #5 的规格补充 §6–§9 与 §16、issue #6 的规格补充 §1–§11；契约见 `docs/openapi.yaml`。
端点都只要求登录（`user` 即可）：数据来自该版本的索引库 `db/<version>.sqlite`
与源码树 `src/<version>/`，两者都在数据根下（`indexing.layout`）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query

from mc_librarian.auth.deps import CurrentUser, DbConn, SettingsDep
from mc_librarian.config import Settings
from mc_librarian.indexing import java, query
from mc_librarian.indexing.layout import source_root
from mc_librarian.indexing.text import split_lines, tokens_from_chars
from mc_librarian.jobs import store
from mc_librarian.problems import ProblemException, problem_response
from mc_librarian.retrieval import search as run_search
from mc_librarian.schemas import (
    FileSlice,
    Outline,
    OutlineClass,
    OutlineMethod,
    Reference,
    ReferenceResponse,
    SearchRequest,
    SearchResponse,
    SymbolDetail,
    SymbolKind,
    SymbolMatches,
    SymbolMember,
)

router = APIRouter()

NOT_FOUND_TITLE: Final = "资源不存在"
BAD_REQUEST_TITLE: Final = "请求不合法"

VersionQuery = Annotated[str, Query(description="目标版本，必须已完成索引；不支持 latest")]
PathQuery = Annotated[str, Query(description="相对该版本源码根（`src/<version>/`）的路径")]

# 四个端点的错误响应：未认证、版本/文件不存在、查询参数不合法。每次构造新字典，
# 避免多条路由共享同一个可变对象。
def endpoint_responses() -> dict[int | str, dict[str, Any]]:
    return {
        401: problem_response("未认证"),
        404: problem_response("版本未注册、索引文件不存在，或源码文件缺失"),
        422: problem_response("请求不合法"),
    }


def _require_index(conn: sqlite3.Connection, settings: Settings, version: str) -> Path:
    """规格 §9：版本未注册 / `db_path` 为空 / 文件不存在一律 404，返回索引文件路径。"""
    registered, db_path = store.index_db_path(conn, version)

    if not registered:
        raise ProblemException(404, NOT_FOUND_TITLE, f"版本 {version} 未注册")

    if db_path is None:
        raise ProblemException(404, NOT_FOUND_TITLE, f"版本 {version} 尚无索引文件")

    path = Path(db_path)

    if not path.is_file():
        raise ProblemException(404, NOT_FOUND_TITLE, f"索引文件不存在：{path}")

    return path


def _read_source(settings: Settings, version: str, path: str) -> str:
    """读 `<data_dir>/src/<version>/<path>`；缺失或越出源码根一律 404（规格 §16）。"""
    root = source_root(settings.data_dir, version).resolve()
    candidate = (root / path).resolve()

    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ProblemException(404, NOT_FOUND_TITLE, f"该版本的源码树缺失（期望路径 {root}）")

    return candidate.read_text(encoding="utf-8", errors="replace")


@router.post(
    "/search",
    tags=["retrieve"],
    summary="混合检索（摘要 / 符号 / 字面量）",
    description=(
        "三路召回（symbol / literal / reference，摘要通道待 #7）在服务端用 RRF 融合，"
        "调用方不需要知道该用哪条通道。`detail=locate` 只返回定位信息（省 token），"
        "`snippet` 额外附带源码片段与摘要；`max_tokens` 按 score 降序截断；"
        "`path_prefix` 在候选阶段过滤。"
    ),
    operation_id="search",
    response_model_exclude_none=True,
    responses={
        401: problem_response("未认证"),
        404: problem_response("版本未注册或索引文件不存在"),
        422: problem_response("请求不合法"),
    },
)
def search(
    conn: DbConn,
    _user: CurrentUser,
    settings: SettingsDep,
    body: SearchRequest,
) -> SearchResponse:
    index = query.open_readonly(_require_index(conn, settings, body.version))

    try:
        return run_search(index, settings, body)
    finally:
        index.close()


@router.get(
    "/symbol",
    tags=["retrieve"],
    summary="符号查找",
    description=(
        "按「全限定名 → 简单名 → 后缀 → 编辑距离」四级依次尝试，先命中先返回，不跨级别混合；"
        "`fuzzy=false` 只做前两级。"
    ),
    operation_id="getSymbol",
    responses=endpoint_responses(),
)
def get_symbol(
    conn: DbConn,
    _user: CurrentUser,
    settings: SettingsDep,
    version: VersionQuery,
    name: Annotated[str, Query(min_length=1, max_length=256)],
    fuzzy: Annotated[bool, Query()] = True,
    kind: Annotated[SymbolKind, Query()] = "any",
) -> SymbolMatches:
    index = query.open_readonly(_require_index(conn, settings, version))

    try:
        hits = query.find_symbols(index, name, fuzzy=fuzzy, kind=kind)
        matches = [_to_symbol_detail(index, hit) for hit in hits]
    finally:
        index.close()

    return SymbolMatches(matches=matches)


@router.get(
    "/references",
    tags=["retrieve"],
    summary="反查引用",
    description="`symbol` 是方法名或 `Class#method`；数据来自 AST 启发式提取，召回优先。",
    operation_id="getReferences",
    responses=endpoint_responses(),
)
def get_references(
    conn: DbConn,
    _user: CurrentUser,
    settings: SettingsDep,
    version: VersionQuery,
    symbol: Annotated[str, Query(description="方法名或 `Class#method`")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ReferenceResponse:
    index = query.open_readonly(_require_index(conn, settings, version))

    try:
        hits = query.find_references(index, symbol, limit=limit)
    finally:
        index.close()

    return ReferenceResponse(
        symbol=symbol,
        version=version,
        references=[
            Reference(
                path=hit.path,
                lines=[hit.line, hit.line],
                enclosing=hit.enclosing,
                kind=hit.kind,
            )
            for hit in hits
        ],
    )


@router.get(
    "/outline",
    tags=["read"],
    summary="文件骨架",
    description="包名、imports、类与方法签名和行号，不含方法体；`imports` 实时从源码解析。",
    operation_id="getOutline",
    responses=endpoint_responses(),
)
def get_outline(
    conn: DbConn,
    _user: CurrentUser,
    settings: SettingsDep,
    version: VersionQuery,
    path: PathQuery,
) -> Outline:
    index = query.open_readonly(_require_index(conn, settings, version))

    try:
        record = query.find_file(index, path)

        if record is None:
            raise ProblemException(404, NOT_FOUND_TITLE, f"索引里没有文件：{path}")

        classes = query.file_outline(index, record.id)
    finally:
        index.close()

    imports = java.parse_imports(_read_source(settings, version, path))

    return Outline(
        path=path,
        package=record.package,
        imports=imports,
        classes=[
            OutlineClass(
                fqn=item.fqn,
                kind=item.kind,
                signature=item.signature,
                lines=[item.start_line, item.end_line],
                methods=[
                    OutlineMethod(
                        name=method.name,
                        signature=method.signature,
                        lines=[method.start_line, method.end_line],
                    )
                    for method in item.methods
                ],
            )
            for item in classes
        ],
    )


@router.get(
    "/read",
    tags=["read"],
    summary="源码切片",
    description="`start`/`end` 必填，不提供整文件读取；超出 `max_tokens` 时按行从尾部截断。",
    operation_id="readFile",
    responses=endpoint_responses(),
)
def read_file(
    conn: DbConn,
    _user: CurrentUser,
    settings: SettingsDep,
    version: VersionQuery,
    path: PathQuery,
    start: Annotated[int, Query(description="起始行（1-based，闭区间）")],
    end: Annotated[int, Query(description="结束行（1-based，闭区间）")],
    max_tokens: Annotated[int, Query(ge=100, le=32000)] = 4000,
) -> FileSlice:
    # 行号校验自己做（规格 §8）：三条越界都要在 detail 里带上文件总行数。
    _require_index(conn, settings, version)
    lines = split_lines(_read_source(settings, version, path))
    total_lines = len(lines)

    if start < 1 or end < start or end > total_lines:
        raise ProblemException(
            422, BAD_REQUEST_TITLE, f"行号超出范围：文件共 {total_lines} 行"
        )

    selected = lines[start - 1 : end]
    kept: list[str] = []
    used = 0

    for line in selected:
        cost = tokens_from_chars(len(line) + 1)

        if kept and used + cost > max_tokens:
            break

        kept.append(line)
        used += cost

    return FileSlice(
        path=path,
        version=version,
        start=start,
        end=start + len(kept) - 1,
        total_lines=total_lines,
        content="\n".join(kept),
        truncated=len(kept) < len(selected),
    )


def _to_symbol_detail(connection: sqlite3.Connection, hit: query.SymbolHit) -> SymbolDetail:
    """符号命中 → `SymbolDetail`；类的命中附带成员列表（方法 + 字段）。"""
    members: list[SymbolMember] = []

    if hit.kind == "class":
        members = [
            SymbolMember(
                kind=member.kind,
                name=member.name,
                signature=member.signature,
                lines=[member.start_line, member.end_line],
            )
            for member in query.class_members(connection, hit.class_id)
        ]

    return SymbolDetail(
        fqn=hit.fqn,
        kind=hit.kind,
        signature=hit.signature,
        path=hit.path,
        lines=[hit.start_line, hit.end_line],
        superclass=hit.superclass,
        interfaces=hit.interfaces,
        annotations=hit.annotations,
        members=members,
    )
