"""RRF（Reciprocal Rank Fusion）融合（issue #6 规格 §1）。

三路的分数量纲不可比（FTS5 的 bm25、符号匹配级别、ripgrep 无分数），所以不做分数加权：

    score(d) = Σ_over_channels 1 / (k + rank_c(d))        k = 60

同一实体被多路召回时累加；再除以「本次融合的最高分」归一化到 `[0, 1]`，最高分为 1.0。
`why` 是去重后的通道数组，顺序固定为 `summary → symbol → literal → reference → patch`（规格 §4；
`patch` 是 issue #24 追加的第五路）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mc_librarian.retrieval.channels import Candidate, Channel

RRF_K: Final = 60
# 融合前每路只保留前 N 条。
#
# 不截断时长尾会把强信号淹掉：摘要通道一次给 50 条，名次 4 和名次 50 的分差只有 0.002，
# 于是一个「引用通道第 2 名」（说明这个词确实被谁调用）根本浮不上来。截断到 10 条后，
# 各路的名次才有可比性——实测 30 题中文评测从 12/30 提到 14/30（issue #23）。
CHANNEL_CANDIDATE_CAP: Final = 10
WHY_ORDER: Final[tuple[Channel, ...]] = ("summary", "symbol", "literal", "reference", "patch")


@dataclass(frozen=True)
class FusedHit:
    """融合后的一条命中；`channels` 已按 `WHY_ORDER` 排序。"""

    candidate: Candidate
    channels: tuple[Channel, ...]
    score: float


def fuse(channels: dict[Channel, list[Candidate]]) -> list[FusedHit]:
    """把各路候选按实体键融合、归一化，并按 score 降序返回（同分按实体键字典序）。"""
    first: dict[str, Candidate] = {}
    ranks: dict[str, dict[Channel, int]] = {}

    for channel in WHY_ORDER:
        seen: set[str] = set()
        rank = 0

        for candidate in channels.get(channel, [])[:CHANNEL_CANDIDATE_CAP]:
            key = candidate.key

            # 同一路里同一实体只算一次（取最靠前的一次），否则单路重复命中会虚增分数。
            if key in seen:
                continue

            seen.add(key)
            rank += 1
            first.setdefault(key, candidate)
            ranks.setdefault(key, {})[channel] = rank

    raw = {key: sum(1 / (RRF_K + rank) for rank in ranks[key].values()) for key in first}
    top = max(raw.values(), default=0.0)

    fused = [
        FusedHit(
            candidate=first[key],
            channels=tuple(channel for channel in WHY_ORDER if channel in ranks[key]),
            score=(raw[key] / top) if top else 0.0,
        )
        for key in first
    ]
    fused.sort(key=lambda hit: (-hit.score, hit.candidate.key))

    return fused


def apply_token_budget(costs: list[int], max_tokens: int) -> list[int]:
    """按 score 降序累加各条命中的 `tokens`，超出预算即丢弃当前及之后的条目（规格 §6）。

    `costs` 与 `fuse` 的结果同序、同长（调用方按 `detail` 决定每条的成本，见
    `retrieval.service`）。**至少返回第一条**（即使它自己就超预算），避免「有命中却返回空」。
    返回保留的下标。
    """
    kept: list[int] = []
    used = 0

    for index, cost in enumerate(costs):
        if kept and used + cost > max_tokens:
            break

        kept.append(index)
        used += cost

    return kept
