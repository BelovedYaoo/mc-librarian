"""内存峰值采样（issue #13 规格补充 §3）。

用 fake 采样器断言「峰值取最大值」；`psutil` 缺失（生产不装）时只降级为「不测量」，
不抛异常、不阻断流水线。
"""

from __future__ import annotations

import time

import pytest

from mc_librarian.indexing import memory


def test_sampler_keeps_maximum_of_samples() -> None:
    values = iter([100, 300, 200])
    sampler = memory.MemorySampler(sample=lambda: next(values))

    for _ in range(3):
        sampler.sample_once()

    assert sampler.peak_bytes == 300


def test_sampler_records_peak_in_background() -> None:
    """`start()` 后后台线程按间隔采样：测试用极小间隔 + 假采样器，不依赖真实进程树。"""
    sampler = memory.MemorySampler(interval=0.01, sample=lambda: 12345)

    sampler.start()
    try:
        time.sleep(0.05)
    finally:
        peak = sampler.stop()

    assert peak == 12345


def test_sampler_without_psutil_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "psutil", None)

    assert memory.available() is False
    assert memory.tree_rss_bytes() == 0
    assert memory.MemorySampler().sample_once() == 0
    assert memory.MemorySampler().stop() == 0


@pytest.mark.skipif(not memory.available(), reason="未安装 psutil（dev 依赖）")
def test_tree_rss_counts_own_process() -> None:
    assert memory.tree_rss_bytes() > 0


def test_format_bytes_switches_unit_at_one_gib() -> None:
    assert memory.format_bytes(3 * 1024**3 + 1024**3 // 2) == "3.5 GiB"
    assert memory.format_bytes(512 * 1024**2) == "512.0 MiB"
