"""uvicorn 入口：`python -m mc_librarian [--reload]`。

监听地址与端口取自 `Settings`（`MC_LIBRARIAN_HOST` / `MC_LIBRARIAN_PORT`），
因此 `bun run dev` 与手工启动走同一份配置，不存在「配置项不生效」的情况。
"""

from __future__ import annotations

import sys

import uvicorn
from pydantic import ValidationError

from mc_librarian.config import get_settings


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as error:
        # 配置不合法（例如生产环境没给会话 cookie 开 Secure）时给一句人话就退出，
        # 不要甩一整个 pydantic traceback 让人猜哪里错了。
        print("配置错误，服务未启动：", file=sys.stderr)

        for detail in error.errors():
            message = str(detail["msg"]).removeprefix("Value error, ")
            location = ".".join(str(part) for part in detail["loc"])
            print(f"  - {location or 'settings'}：{message}", file=sys.stderr)

        raise SystemExit(2) from None

    uvicorn.run(
        "mc_librarian.main:app",
        host=settings.host,
        port=settings.port,
        reload="--reload" in sys.argv[1:],
    )


if __name__ == "__main__":
    main()
