"""uvicorn 入口：`python -m mc_librarian [--reload]`。

监听地址与端口取自 `Settings`（`MC_LIBRARIAN_HOST` / `MC_LIBRARIAN_PORT`），
因此 `bun run dev` 与手工启动走同一份配置，不存在「配置项不生效」的情况。
"""

from __future__ import annotations

import sys

import uvicorn

from mc_librarian.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "mc_librarian.main:app",
        host=settings.host,
        port=settings.port,
        reload="--reload" in sys.argv[1:],
    )


if __name__ == "__main__":
    main()
