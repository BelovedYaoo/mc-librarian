"""进程入口。

- `python -m mc_librarian [--reload]` 启动服务；监听地址与端口取自 `Settings`
  （`MC_LIBRARIAN_HOST` / `MC_LIBRARIAN_PORT`），因此 `bun run dev` 与手工启动走同一份配置。
- `python -m mc_librarian fetch --version <v> --out <dir>` 跑下载+反编译流水线（见 #21），
  与安装后的 `mc-librarian fetch ...` 是同一个入口。
- `python -m mc_librarian index --version <v> [--source-dir <dir>] [--force]` 端到端产出可检索
  索引（见 #13），与安装后的 `mc-librarian index ...` 是同一个入口。
- `python -m mc_librarian eval --version <v> [--set <file>] [--out <dir>] [--limit 5]` 跑人工
  评测集并产出 JSON + markdown 报告（见 #16），与安装后的 `mc-librarian eval ...` 是同一个入口。
"""

from __future__ import annotations

import sys

import uvicorn
from pydantic import ValidationError

from mc_librarian.config import get_settings


def serve() -> None:
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


def main() -> None:
    """服务入口（保留给测试与旧调用点）。"""
    serve()


def cli() -> None:
    """控制台入口：第一个参数是子命令，`fetch` 走下载+反编译，`index` 走端到端索引，其余走服务。"""
    argv = sys.argv[1:]

    if argv[:1] == ["fetch"]:
        from mc_librarian.fetch.cli import main as fetch_main

        raise SystemExit(fetch_main(argv[1:]))

    if argv[:1] == ["index"]:
        from mc_librarian.indexing.cli import main as index_main

        raise SystemExit(index_main(argv[1:]))

    if argv[:1] == ["eval"]:
        from mc_librarian.eval.cli import main as eval_main

        raise SystemExit(eval_main(argv[1:]))

    serve()


if __name__ == "__main__":
    cli()
