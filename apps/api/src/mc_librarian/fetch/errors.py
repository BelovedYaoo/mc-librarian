"""下载 / 反编译流水线可预期的失败。

单独放在叶子模块，避免 `manifest` / `tools` / `pipeline` 互相 import 成环。
"""

from __future__ import annotations


class FetchError(RuntimeError):
    """流水线失败；消息会进任务日志（`StageError`）或 CLI 的 stderr。"""
