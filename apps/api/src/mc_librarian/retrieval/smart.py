"""智能检索（`mode=smart`）：召回并集 + LLM 列表精排。

为什么要两段（issue #23 的实测结论）：

- **瓶颈是召回，不是排序。** 纯字面检索的召回上限只有 17/30：金标根本不在候选里，
  换任何精排都凑不出来（实测 LLM 精排对纯字面候选只有 16/30）。
- 让模型把问题**改写成检索词**（同义说法 + 可能对应的英文类名）后再召回一次，并集能到 29/30。
- 有了这个候选集，**LLM 列表精排几乎不丢分**：29/30 正好等于并集上限（`jina-reranker-v2`
  这类小 cross-encoder 在同一候选集上只有 20/30——中文语义排序这一步小模型不够）。

设计约束：

- **不劣化现状**：LLM 未配置 / 超时 / 返回不可解析时，退回纯字面结果，语义与 `mode=lexical` 一致。
- **成本可控**：每查询最多 2 次 LLM（扩展 + 精排）；扩展按查询文本缓存，重复查询为 0 次。
- **不让模型编造**：精排只允许从**索引里真实存在的候选**里挑，模型只输出编号；
  扩展词只用于召回，命中与否由索引决定。
"""

from __future__ import annotations

import logging
import re
from typing import Final

from mc_librarian.config import Settings, resolve_llm_config
from mc_librarian.indexing.summarize import post_chat_completion
from mc_librarian.retrieval.fuse import FusedHit

logger = logging.getLogger(__name__)

# 精排看多少条候选。召回并集 @20 已覆盖 25/30、@30 覆盖 ~26–27/30：再多名次也没用，
# 反而把 prompt 撑长、拖慢模型。实测 30 条与 50 条的 Top-5 无差别。
SMART_CANDIDATE_LIMIT: Final = 30
# 扩展缓存条数：同一问题第二次是 0 成本（生产里重复提问很常见）。
EXPANSION_CACHE_SIZE: Final = 512
# 精排 prompt 里每条候选只带摘要前 N 字：够模型判断主题，又不至于让 prompt 过长。
CANDIDATE_SUMMARY_CHARS: Final = 60

EXPANSION_PROMPT: Final = """把下面这个关于 Minecraft 源码的中文问题，改写成检索关键词。

要求：
1. 输出 3–8 个关键词，空格分隔，不要解释、不要标点；
2. 一半用**游戏机制的中文说法**（玩家/模组作者常用的，包括同义说法）；
3. 一半用**可能对应的英文类名或方法名的片段**
   （例如 掉落表 → LootTable loot drop；合成表 → Recipe crafting）。

问题：{question}
关键词："""

RERANK_PROMPT: Final = """下面是一个 Minecraft 源码问题，以及检索系统召回的候选类
（编号 + 类名 + 摘要）。
请挑出最能回答这个问题的 5 个，按相关性从高到低输出编号，逗号分隔，只输出编号。

问题：{question}

{candidates}

编号："""

_expansion_cache: dict[str, str] = {}


def _remember(query: str, expansion: str) -> None:
    """有上限的扩展缓存：满了先丢最早插入的那条（够用，且不引入依赖）。"""
    if len(_expansion_cache) >= EXPANSION_CACHE_SIZE:
        _expansion_cache.pop(next(iter(_expansion_cache)))

    _expansion_cache[query] = expansion


def _ask(settings: Settings, prompt: str) -> str:
    """一次同步 LLM 调用；失败返回空串（调用方据此回退，不把异常冒给 HTTP）。"""
    config = resolve_llm_config(settings)
    payload = {
        "model": config.model,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": prompt}],
    }

    return post_chat_completion(
        f"{config.base_url}/chat/completions", payload, config.api_key, settings.llm_timeout_s
    ).strip()


def expand_query(settings: Settings, query: str) -> str:
    """问题 → 检索关键词（带缓存）。失败或未配置 LLM 时返回空串。"""
    cached = _expansion_cache.get(query)

    if cached is not None:
        return cached

    try:
        expansion = _ask(settings, EXPANSION_PROMPT.format(question=query)).splitlines()[0][:200]
    except Exception as error:  # 未配置 / 网络 / 超时：静默回退到纯字面
        logger.warning("智能检索：查询扩展失败（%s）", error)

        return ""

    _remember(query, expansion)

    return expansion


def rerank(settings: Settings, query: str, candidates: list[FusedHit]) -> list[FusedHit]:
    """让模型从候选里挑 5 个并排序；任何失败都退回原顺序（不劣化）。"""
    if len(candidates) <= 1:
        return candidates

    lines = "\n".join(
        f"{index}. {hit.candidate.symbol} — "
        f"{(hit.candidate.summary or '').replace(chr(10), ' ')[:CANDIDATE_SUMMARY_CHARS]}"
        for index, hit in enumerate(candidates, start=1)
    )

    try:
        answer = _ask(settings, RERANK_PROMPT.format(question=query, candidates=lines))
    except Exception as error:
        logger.warning("智能检索：精排失败（%s）", error)

        return candidates

    picked = [int(number) for number in re.findall(r"\d+", answer)]
    picked = [number for number in picked if 1 <= number <= len(candidates)][:5]

    if not picked:
        logger.warning("智能检索：精排返回无法解析（%r），沿用原顺序", answer[:80])

        return candidates

    # 只重排被选中的：模型没提到的候选排在后面（它们本来就在长尾里）。
    head = [candidates[number - 1] for number in dict.fromkeys(picked)]
    head_keys = {hit.candidate.key for hit in head}

    return head + [hit for hit in candidates if hit.candidate.key not in head_keys]


def remember_expansion(query: str, expansion: str) -> None:
    """把一次扩展写进缓存（测试与预热用）。"""
    _remember(query, expansion)


def clear_cache() -> None:
    _expansion_cache.clear()


__all__ = [
    "SMART_CANDIDATE_LIMIT",
    "clear_cache",
    "expand_query",
    "remember_expansion",
    "rerank",
]
