"""检索查询的分词路由：把一句中文提问拆成「英文标识符」与「中文描述」。

中文提问里经常直接写着类名或方法名：

    谁调用了 LootTable 的 getRandomItems
    两个版本的 GameRules 分别放在哪个包
    1.20.1 与 26.3 的 MobCategory 定义有什么不同

这些字串对**符号表 / 引用表 / 源码字面量**是最精确的键，但之前整句被原样送进这三路：
`symbol` 拿整句去比类名（必然 0 命中）、`reference` 也是、`literal` 把整句当正则丢给 ripgrep
（源码里当然没有这句话）。实测这直接吃掉 30 题里的 5 题（`#23`）。

所以查询要按目标分流：

| 通道 | 用什么查 |
|---|---|
| `summary` / `patch` | 中文描述（摘要本身是中文，标识符留着也无害，仍然用整句） |
| `symbol` / `reference` / `literal` | 抽出来的英文标识符；一个都没有时才退回整句 |
"""

from __future__ import annotations

import re
from typing import Final

from mc_librarian.indexing.summarize import CJK_CHAR

# 标识符：字母开头，允许数字与下划线，允许点分（`net.minecraft.world.Level`）。
# 不用 `\b`：中文紧贴标识符时（`调用LootTable`）`\b` 在 CJK 与 ASCII 之间不成立。
IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")

# 太短或纯技术噪声的词不当标识符：`json` / `mc` / 单个字母没有区分度，还会把
# 符号通道拖进大量无关类（实测 `tag` 在 26.3 里有 36 个候选）。
MIN_IDENTIFIER_LENGTH: Final = 3
IGNORED_IDENTIFIERS: Final = frozenset({"json", "mc", "api", "id", "url", "html", "xml"})


def identifiers(text: str) -> tuple[str, ...]:
    """提问里的英文标识符（去重、保序）。`getRandomItems` 这类短名也算，只要够长。"""
    found: list[str] = []
    seen: set[str] = set()

    for match in IDENTIFIER_PATTERN.findall(text):
        lowered = match.lower()

        if len(match) < MIN_IDENTIFIER_LENGTH or lowered in IGNORED_IDENTIFIERS:
            continue

        if lowered in seen:  # `GameRules` 与 `gamerules` 视为同一个
            continue

        seen.add(lowered)
        found.append(match)

    return tuple(found)


def has_cjk(text: str) -> bool:
    return CJK_CHAR.search(text) is not None


__all__ = ["has_cjk", "identifiers"]
