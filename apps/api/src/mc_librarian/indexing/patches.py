"""加载器补丁（`*.patch`）的切块解析（issue #24 规格 §2/§3）。

切块单位是 **hunk**，不是一个 patch 文件一条：一个 patch 常含多个不相干的改动，整文件当一块
会让检索命中后给不出具体位置。每个 `@@ -a,b +c,d @@` 生成一条 `PatchHunk`，`context` 是
「hunk 头 + diff 正文」。

**NeoForge 的 patch 用 `+_,N` 写新侧行数、新侧起始行是 `_`（未记录）**：本模块按统一 diff 的
语义重建 `new_start`——`new_start_k = old_start_k + Σ_{j<k}(new_lines_j - old_lines_j)`，
这样 `patches.new_start` 与检索结果的 `lines`（新行号区间）才有真实含义；重建不出来
（缺 `old_start`）时留 `NULL`，由检索层回退到 `old_start`。头部显式给了数字时以头部为准。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from mc_librarian.indexing.text import count_tokens

PATCH_SUFFIX: Final = ".patch"

# `@@ -104,7 +_,10 @@`（NeoForge 的 `+_`）与标准的 `@@ -1,3 +1,5 @@` 都认。
HUNK_HEADER: Final = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))?"
    r" \+(?P<new_start>\d+|_)(?:,(?P<new_lines>\d+))? @@(?P<section>.*)$"
)


@dataclass(frozen=True)
class PatchHunk:
    """`patches` 表的一行；`new_start` 可能为 `None`（重建不出来时）。"""

    path: str
    target: str
    hunk_index: int
    old_start: int | None
    old_lines: int | None
    new_start: int | None
    new_lines: int | None
    context: str
    tokens: int


def iter_patch_files(root: Path) -> Iterator[Path]:
    """`root` 下递归的全部 `*.patch`，按路径排序（结果确定，便于断言）。"""
    for path in sorted(root.rglob(f"*{PATCH_SUFFIX}")):
        if path.is_file():
            yield path


def parse_patch_file(text: str, *, path: str) -> list[PatchHunk]:
    """把一个 patch 文件切成 hunk；没有 hunk（纯头部 / 空文件）时返回空列表。

    `path` 是补丁文件的相对路径，写进 `patches.path`；`target` 从 `+++ b/<path>` 头解析，
    头缺失时退回「去掉 `.patch` 后缀的补丁路径」。
    """
    target = patch_target(text) or path[: -len(PATCH_SUFFIX)]
    hunks: list[PatchHunk] = []
    header: str | None = None
    old_start: int | None = None
    old_lines: int | None = None
    new_start: int | None = None
    new_lines: int | None = None
    body: list[str] = []
    offset = 0

    def flush() -> None:
        nonlocal offset

        if header is None:
            return

        resolved_start = new_start if new_start is not None else (
            None if old_start is None else old_start + offset
        )
        context = "\n".join([header, *body])
        hunks.append(
            PatchHunk(
                path=path,
                target=target,
                hunk_index=len(hunks),
                old_start=old_start,
                old_lines=old_lines,
                new_start=resolved_start,
                new_lines=new_lines,
                context=context,
                tokens=count_tokens(context),
            )
        )

        if new_lines is not None and old_lines is not None:
            offset += new_lines - old_lines

    for line in text.splitlines():
        matched = HUNK_HEADER.match(line)

        if matched is not None:
            flush()
            header = line
            old_start = int(matched.group("old_start"))
            old_lines = _optional_int(matched.group("old_lines"))
            raw_new_start = matched.group("new_start")
            new_start = None if raw_new_start == "_" else int(raw_new_start)
            new_lines = _optional_int(matched.group("new_lines"))
            body = []
            continue

        if header is not None:
            body.append(line)

    flush()

    return hunks


def patch_target(text: str) -> str | None:
    """从统一 diff 头里解析被改的源文件；`+++` 优先，退回 `---`，都没有返回 `None`。"""
    minus: str | None = None
    plus: str | None = None

    for line in text.splitlines():
        if plus is None and line.startswith("+++ "):
            plus = line[4:]
        elif minus is None and line.startswith("--- "):
            minus = line[4:]

        if plus is not None and minus is not None:
            break

    raw = plus if plus is not None else minus

    if raw is None:
        return None

    # `b/path` / `a/path` 前缀与尾部的制表符时间戳都剥掉。
    raw = raw.split("\t", 1)[0].strip()

    for prefix in ("b/", "a/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break

    return raw or None


def patch_content_hash(path: str, target: str, hunks: list[PatchHunk]) -> str:
    """一个 patch 文件的内容指纹（`summaries.content_hash` 口径）：路径 + target + 各 hunk 正文。"""
    parts = [path, target, *(hunk.context for hunk in hunks)]

    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _optional_int(raw: str | None) -> int | None:
    return None if raw is None else int(raw)
