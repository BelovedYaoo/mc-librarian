"""文本度量：token 估算与行数。

行数口径必须与 `read` 端点的切片口径一致（规格 §2 的 1-based 闭区间）：
按 `\\n` 切分，结尾换行不额外产生一行；`\\r\\n` 由 `Path.read_text` 的通用换行翻译成 `\\n`。
"""

from __future__ import annotations

from typing import Final

# token 估算：不引入 tiktoken 依赖，按「字符数 ÷ 3.5」近似 cl100k 词元数（#13 可换真分词）。
CHARS_PER_TOKEN: Final = 3.5


def tokens_from_chars(chars: int) -> int:
    """按 `CHARS_PER_TOKEN` 估算 `chars` 个字符的 token 数。"""
    return int(chars / CHARS_PER_TOKEN)


def split_lines(text: str) -> list[str]:
    """按 `\\n` 切分的行；结尾换行不产生额外的空行。"""
    lines = text.split("\n")

    if lines and lines[-1] == "":
        lines.pop()

    return lines


def count_lines(text: str) -> int:
    """文件行数，与 `split_lines` 同口径。"""
    return len(split_lines(text))
