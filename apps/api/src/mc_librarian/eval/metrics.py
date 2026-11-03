"""指标口径（issue #16 规格 §3）。

| 指标 | 口径 |
|---|---|
| Top-N 命中率 | 前 N 条命中里含任一 gold 的题数 / 总题数（纯检索，`detail=locate`） |
| MRR | 第一个 gold 命中的倒数排名（`1/rank`）；Top-N 内没有记 0，再对题取平均 |
| 平均工具调用轮数 | 每题 `AskStep` 条数的均值（一次工具调用一轮）；没实测时为 `None` |

`matches_gold` 的匹配规则：`gold` 是类 FQN 时，命中该类本身或它的任一成员都算；是
`Class#method` 时只认完全相等（重载方法在索引里是同名的多条，算命中）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class HitStats:
    """一题在 Top-N 上的命中情况。"""

    hit: bool
    # 第一个 gold 命中的排名（1-based）；Top-N 内没有 gold 时为 None。
    rank: int | None
    # `1/rank`；没命中为 0.0。
    reciprocal_rank: float


def matches_gold(symbol: str | None, gold: Sequence[str]) -> bool:
    """命中符号是否算命中 gold 之一；`None`（文件级命中）不算。"""
    if symbol is None:
        return False

    for entry in gold:
        if symbol == entry:
            return True

        if "#" not in entry and symbol.startswith(f"{entry}#"):
            return True

    return False


def evaluate_hits(symbols: Sequence[str | None], gold: Sequence[str]) -> HitStats:
    """`symbols` 必须已按排名截断到 Top-N（`None` 表示该条命中没有符号）。"""
    for rank, symbol in enumerate(symbols, start=1):
        if matches_gold(symbol, gold):
            return HitStats(hit=True, rank=rank, reciprocal_rank=1.0 / rank)

    return HitStats(hit=False, rank=None, reciprocal_rank=0.0)


def hit_rate(stats: Sequence[HitStats]) -> float:
    """Top-N 命中率；空集合记 0（题目集非空，调用方保证）。"""
    if not stats:
        return 0.0

    return sum(1 for item in stats if item.hit) / len(stats)


def mean_reciprocal_rank(stats: Sequence[HitStats]) -> float:
    """MRR：每题 `1/rank` 的均值，未命中的题按 0 计入分母。"""
    if not stats:
        return 0.0

    return sum(item.reciprocal_rank for item in stats) / len(stats)


def mean_tool_steps(steps: Sequence[int]) -> float | None:
    """平均工具调用轮数；没实测（未配 LLM）时返回 `None`，不用 0 冒充。"""
    if not steps:
        return None

    return sum(steps) / len(steps)
