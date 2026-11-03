"""文本度量：token 计数与行数。

**token 口径是 cl100k_base 真分词**：`#13` 的验收标准（26.3 约 7.5M token）就是用
cl100k 实测的，而「字符 ÷ 3.5」这种常数估算在同一批源码上偏 13%–27%（同一常数在不同代码库
上差 ±40%，两个口径不可比）。因此 `count_tokens` 是唯一的计数入口，常数估算只在 tiktoken
不可用（没装，或首次使用拉不到 BPE 词表）时兜底并写一条 `warn`。

`tiktoken` 首次使用会去 `openaipublic.blob.core.windows.net` 拉 BPE 词表：生产节点拉不到时
会阻塞在一次 HTTP 超时上，然后静默退回常数估算。`warmup_token_counting` 供 FastAPI lifespan
在启动时预热一次，并把当前口径写进 INFO 日志，让漂移可见而不是在第一次检索时才暴露。

行数口径必须与 `read` 端点的切片口径一致（规格 §2 的 1-based 闭区间）：
按 `\\n` 切分，结尾换行不额外产生一行；`\\r\\n` 由 `Path.read_text` 的通用换行翻译成 `\\n`。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Final

logger = logging.getLogger("mc_librarian.indexing.text")

# 与 #13 验收标准同一口径（真分词器）。
CL100K_ENCODING: Final = "cl100k_base"
# 常数估算的字符/token 比；只在 tiktoken 不可用时兜底。
CHARS_PER_TOKEN: Final = 3.5


def tokens_from_chars(chars: int) -> int:
    """按 `CHARS_PER_TOKEN` 估算 `chars` 个字符的 token 数（`count_tokens` 的兜底路径）。"""
    return int(chars / CHARS_PER_TOKEN)


@lru_cache(maxsize=1)
def _load_encoder() -> tuple[Callable[[str], list[int]] | None, str | None]:
    """加载 cl100k_base 的编码函数；返回 (编码函数, 失败原因)，失败时编码函数为 `None`。

    缓存（含失败结果）是刻意的：一次索引要过几千个文件，每次都重试加载会让日志被同一条
    `warn` 淹没，也会让每次计数都卡在一次 HTTP 超时上。`encode_ordinary` 忽略特殊词元
    （源码里出现 `<|endoftext|>` 这类字面量时默认的 `encode` 会抛异常）。
    """
    try:
        import tiktoken

        encoding = tiktoken.get_encoding(CL100K_ENCODING)
    except Exception as error:  # 未安装、或离线时拉不到词表
        return None, str(error)

    return encoding.encode_ordinary, None


@lru_cache(maxsize=1)
def _encoder() -> Callable[[str], list[int]] | None:
    """cl100k_base 的编码函数；加载失败返回 `None` 并写一条 `warn`（每个进程只写一次）。"""
    encoder, reason = _load_encoder()

    if encoder is None:
        logger.warning(
            "tiktoken 不可用（%s），token 退回按 %.1f 字符/token 估算", reason, CHARS_PER_TOKEN
        )

    return encoder


def warmup_token_counting() -> str:
    """启动时预热一次 token 计数，返回当前口径的一句话描述（供 lifespan 记 INFO 日志）。

    `tiktoken` 首次使用会联网拉 BPE 词表：生产节点拉不到时会阻塞在一次 HTTP 超时上，然后
    静默退回常数估算并改变 `max_tokens` 的语义。预热把这次阻塞挪到启动阶段，并让当前口径
    出现在日志里（`token 计数：cl100k（tiktoken）` 或 `token 计数：字符估算 fallback（原因：…）`），
    而不是在第一次检索时才漂移。
    """
    count_tokens("warmup")
    encoder, reason = _load_encoder()

    if encoder is None:
        return f"token 计数：字符估算 fallback（原因：{reason}）"

    return "token 计数：cl100k（tiktoken）"


def count_tokens(text: str) -> int:
    """`text` 的 token 数（cl100k_base 真分词）；tiktoken 不可用时退回常数估算。"""
    encoder = _encoder()

    if encoder is None:
        return tokens_from_chars(len(text))

    return len(encoder(text))


def split_lines(text: str) -> list[str]:
    """按 `\\n` 切分的行；结尾换行不产生额外的空行。"""
    lines = text.split("\n")

    if lines and lines[-1] == "":
        lines.pop()

    return lines


def count_lines(text: str) -> int:
    """文件行数，与 `split_lines` 同口径。"""
    return len(split_lines(text))
