"""问答并发闸门（`max_concurrent_asks`，issue #8 规格 §7）。

超出上限的请求**直接 429**，不排队：问答是长连接（SSE），排队会让调用方一直等，且每个在跑的
问答都要占着一份上下文预算。计数是同步的（无 await），在单线程事件循环里 `try_acquire` 与
`release` 之间不会插入其他请求。
"""

from __future__ import annotations


class AskLimiter:
    """同时在跑的问答数上限。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self) -> bool:
        """占一个槽位；已满返回 `False`（调用方回 429）。"""
        if self._active >= self._limit:
            return False

        self._active += 1

        return True

    def release(self) -> None:
        """归还槽位。

        唯一调用点是 `/v1/ask` 的收尾（`api/v1/answer.py` 的 `_close_ask`）：它挂在 ASGI 响应的
        `__call__` 上，正常结束 / 异常 / 客户端断开取消流任务三条路径都会执行——**不能**依赖流
        生成器的 `finally`（客户端断开时生成器停在 `yield`，那段代码要等 GC / `aclose` 才跑）。
        """
        if self._active > 0:
            self._active -= 1
