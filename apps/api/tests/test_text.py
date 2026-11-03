"""token 计数口径（issue #13 的验收标准与 #6 的 `max_tokens` 预算都靠它）。

口径定死为 **cl100k_base 真分词**：验收标准里的「26.3 约 7.5M token」就是用它实测的，
常数估算（字符 ÷ 3.5）在同一批源码上偏 13%–27%，两个数不可比。tiktoken 不可用时才退回
常数估算并写 `warn`。
"""

from __future__ import annotations

import logging
import sys

import pytest

from mc_librarian.indexing import text


@pytest.fixture(autouse=True)
def clear_encoder_cache() -> None:
    """`_encoder` 是 `lru_cache` 的：每个用例都从干净状态开始，否则会串味。"""
    text._encoder.cache_clear()


def test_count_tokens_matches_cl100k() -> None:
    """真分词：与 cl100k_base 的编码长度一致，而不是字符数 ÷ 3.5。"""
    import tiktoken

    sample = "public class MobCategory { private int maxInstancesPerChunk; }"
    expected = len(tiktoken.get_encoding(text.CL100K_ENCODING).encode(sample))

    assert text.count_tokens(sample) == expected


def test_count_tokens_ignores_special_tokens() -> None:
    """源码里可能出现 `<|endoftext|>` 这类字面量：不能当成特殊词元抛异常。"""
    assert text.count_tokens("<|endoftext|>") > 0


def test_count_tokens_falls_back_when_tiktoken_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """tiktoken 装不上（或离线拉不到词表）时退回常数估算并写 warn，不抛异常。"""
    # `sys.modules` 里放 `None` 会让 `import tiktoken` 抛 ImportError（标准做法）。
    monkeypatch.setitem(sys.modules, "tiktoken", None)

    with caplog.at_level(logging.WARNING, logger="mc_librarian.indexing.text"):
        tokens = text.count_tokens("a" * 35)

    assert tokens == text.tokens_from_chars(35) == 10
    assert "tiktoken 不可用" in caplog.text


def test_encoder_failure_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """加载失败只 warn 一次：一次索引要过几千个文件，重试会把日志刷满。"""
    monkeypatch.setitem(sys.modules, "tiktoken", None)

    with caplog.at_level(logging.WARNING, logger="mc_librarian.indexing.text"):
        for _ in range(3):
            text.count_tokens("abc")

    assert len(caplog.records) == 1
