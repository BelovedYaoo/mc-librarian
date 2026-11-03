"""`mc-librarian fetch --version <v> --out <dir>`：把官方 jar 变成源码树。

与 `python -m mc_librarian fetch ...` 是同一个入口（见 `__main__.py` 的 `cli`）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mc_librarian.config import get_settings
from mc_librarian.fetch.errors import FetchError
from mc_librarian.fetch.pipeline import cache_dir_for, run_fetch
from mc_librarian.fetch.tools import default_tools
from mc_librarian.schemas import LogLevel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-librarian fetch",
        description=(
            "下载并反编译某个 Minecraft 版本的官方 client jar，"
            "产出一棵只含 .java 的源码树。"
        ),
    )
    parser.add_argument("--version", required=True, help="版本号，例如 26.3 或 1.20.1")
    parser.add_argument("--out", required=True, type=Path, help="源码树输出目录")
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="jar / mappings / checkpoint 的缓存目录（默认 <数据根>/cache/<version>）",
    )
    parser.add_argument(
        "--vineflower",
        type=Path,
        default=None,
        help="Vineflower jar 路径（默认取 MC_LIBRARIAN_VINEFLOWER_JAR）",
    )
    parser.add_argument(
        "--specialsource",
        type=Path,
        default=None,
        help="SpecialSource shaded jar 路径（默认取 MC_LIBRARIAN_SPECIALSOURCE_JAR）",
    )
    parser.add_argument("--java", default=None, help="java 可执行文件（默认取 MC_LIBRARIAN_JAVA）")
    parser.add_argument(
        "--memory",
        default=None,
        help="Vineflower 的 -Xmx，例如 4G（默认取 MC_LIBRARIAN_DECOMPILE_MEMORY）",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    cache_dir = args.cache or cache_dir_for(settings.data_dir, args.version)
    tools = default_tools(
        settings,
        vineflower_jar=args.vineflower,
        specialsource_jar=args.specialsource,
        java=args.java,
        memory=args.memory,
    )

    def log(level: LogLevel, message: str) -> None:
        print(f"[{level}] {message}", file=sys.stderr)

    try:
        # `run_fetch` 自己从 `<cache>/fetch.json` 读 checkpoint，中断后重跑会跳过已完成的阶段。
        result = run_fetch(args.version, args.out, cache_dir, tools, log)
    except FetchError as error:
        print(f"fetch 失败：{error}", file=sys.stderr)
        return 1

    print(f"{result.source_dir}：{result.java_files} 个 .java、{result.java_bytes} 字节")

    return 0
