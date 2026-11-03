"""内存峰值采样（规格补充 §3）。

验收要求「单次运行内存峰值低于 6 GiB」。反编译的 `java` 子进程是内存大头，只量本进程会
严重低估，所以量的是**进程树**（本进程 + 全部子孙）的 RSS 之和，按 1 秒采样取最大值。

`psutil` 只在 dev 依赖里（生产不装它）：导入失败时降级为「不测量」，调用方写一条 warn
即可，绝不阻断流水线。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final

# 采样间隔（秒）；规格补充 §3 定死 1 秒。
SAMPLE_INTERVAL_S: Final = 1.0

try:
    import psutil
except ImportError:  # pragma: no cover - 生产不装 psutil 时才走到
    psutil = None


def available() -> bool:
    """`psutil` 是否可用；不可用时调用方写一条 warn 并跳过测量。"""
    return psutil is not None


def tree_rss_bytes() -> int:
    """本进程 + 全部子孙进程的 RSS 之和（字节）；`psutil` 缺失或进程已消失时返回 0。"""
    if psutil is None:
        return 0

    try:
        root = psutil.Process()
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0

    total = 0

    for process in processes:
        try:
            total += int(process.memory_info().rss)
        except psutil.Error:
            # 采样的瞬间子进程可能刚好退出：跳过它，不影响其余进程的统计。
            continue

    return total


def format_bytes(size: int) -> str:
    """人类可读的体积文本，例如 `3.8 GiB`（命令输出与任务日志都用它）。"""
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GiB"

    return f"{size / 1024**2:.1f} MiB"


class MemorySampler:
    """后台线程按固定间隔采样，记录峰值（字节）。

    `sample` 可注入（测试用假采样器，不依赖真实进程树）；线程是 daemon，进程退出不会卡住。
    """

    def __init__(
        self,
        *,
        interval: float = SAMPLE_INTERVAL_S,
        sample: Callable[[], int] | None = None,
    ) -> None:
        self._interval = interval
        self._sample = sample if sample is not None else tree_rss_bytes
        self._peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def peak_bytes(self) -> int:
        return self._peak_bytes

    def sample_once(self) -> int:
        """采样一次并更新峰值，返回本次采样值。"""
        value = self._sample()

        if value > self._peak_bytes:
            self._peak_bytes = value

        return value

    def start(self) -> None:
        if self._thread is not None:
            return

        self._thread = threading.Thread(target=self._loop, name="memory-sampler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.sample_once()

    def stop(self) -> int:
        """停止采样并返回峰值（字节）；未 `start()` 时返回 0。"""
        self._stop.set()
        thread, self._thread = self._thread, None

        if thread is not None:
            thread.join(timeout=self._interval * 2 + 1.0)

        return self._peak_bytes
