"""`mc-librarian eval --version <v> [--set <file>] [--out <dir>] [--limit 5]`（issue #16）。

与 `python -m mc_librarian eval ...` 是同一个入口（见 `__main__.py` 的 `cli`）。命令只负责
参数、路径与落盘：题目集在 `eval.questions`，指标在 `eval.metrics`，检索与问答循环在
`eval.runner`，报告渲染在 `eval.report`。

默认题目集是仓库里的 `apps/api/eval/questions.json`；默认输出目录是**数据根的兄弟目录**
`<data_dir>/../mc-librarian-eval/`——两份报告都写在仓库之外，不污染仓库。

退出码：0 成功（没配 LLM、第三个指标标为「未测」时仍是 0）；2 参数 / 题目集 / 索引不可用；
1 报告写不出去。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from mc_librarian import settings_store
from mc_librarian.config import Settings, get_settings
from mc_librarian.db import Database
from mc_librarian.eval.questions import DEFAULT_SET_PATH, QuestionSetError, load_question_set
from mc_librarian.eval.report import MEAN_TOOL_STEPS, EvalReport
from mc_librarian.eval.runner import EvalError, run_eval
from mc_librarian.schemas import VERSION_PATTERN
from mc_librarian.secretbox import SecretError

# 输出目录名（数据根的兄弟目录）。
OUTPUT_DIR_NAME = "mc-librarian-eval"
# 检索深度的上界与契约 `SearchRequest.limit` 一致。
MAX_LIMIT = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-librarian eval",
        description="对某个已建索引的版本跑人工评测集，产出 JSON + markdown 报告。",
    )
    parser.add_argument("--version", required=True, help="版本号，例如 26.3 或 1.20.1")
    parser.add_argument(
        "--set",
        type=Path,
        default=None,
        help=f"题目集 JSON（默认 {DEFAULT_SET_PATH}）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"报告输出目录（默认数据根的兄弟目录 <data_dir>/../{OUTPUT_DIR_NAME}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="检索深度 Top-N，默认 5",
    )
    parser.add_argument(
        "--no-ask",
        action="store_true",
        help="只测检索指标，跳过每题的 /v1/ask 循环（快很多，适合调检索）",
    )
    parser.add_argument(
        "--mode",
        choices=("lexical", "smart"),
        default="lexical",
        help="检索模式：lexical（默认，纯字面）或 smart（扩展召回 + LLM 精排）",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 版本号会进文件路径（`db/<version>.sqlite`），带 `/` 会跑出数据根。
    if re.fullmatch(VERSION_PATTERN, args.version) is None:
        print(
            f"版本号不合法：{args.version}（只允许字母、数字、点、下划线、连字符）",
            file=sys.stderr,
        )
        return 2

    if not 1 <= args.limit <= MAX_LIMIT:
        print(f"检索深度不合法：{args.limit}（允许 1–{MAX_LIMIT}）", file=sys.stderr)
        return 2

    set_path = (DEFAULT_SET_PATH if args.set is None else args.set).expanduser().resolve()

    try:
        question_set = load_question_set(set_path)
    except QuestionSetError as error:
        print(f"题目集不合法：{error}", file=sys.stderr)
        return 2

    settings = with_database_overrides(get_settings())
    out_dir = (
        default_out_dir(settings.data_dir) if args.out is None else args.out.expanduser().resolve()
    )

    try:
        report = run_eval(
            settings,
            args.version,
            question_set,
            top_n=args.limit,
            mode=args.mode,
            measure_ask=not args.no_ask,
        )
    except EvalError as error:
        print(f"评测失败：{error}", file=sys.stderr)
        return 2

    try:
        json_path, markdown_path = write_reports(report, out_dir)
    except OSError as error:
        print(f"报告写不出去：{error}", file=sys.stderr)
        return 1

    _print_summary(report, json_path, markdown_path)

    return 0


def default_out_dir(data_dir: Path) -> Path:
    """报告默认落在数据根的兄弟目录：仓库之外，也不动只读的数据根。"""
    return data_dir.parent / OUTPUT_DIR_NAME


def with_database_overrides(settings: Settings) -> Settings:
    """叠上控制面 `settings` 表的覆盖，让 CLI 评测与 `/v1/ask` 用同一份配置。

    `/v1/ask` 每次请求都读库覆盖（见 `settings_store`），所以评测里的步数与答案上限必须
    同样叠上，`eval.runner` 里「与 `/v1/ask` 同值」的注释才成立。控制面库还不存在（还没跑过
    服务或索引任务）、还是 `#11` 之前的 schema、或密钥解不开时退回纯环境配置并打一条 warn：
    评测不该因为读不到覆盖就跑不起来。
    """
    database = Database.for_data_dir(settings.data_dir)

    if not database.path.is_file():
        return settings

    try:
        with database.connect() as conn:
            return settings_store.effective_settings(
                settings, settings_store.read_overrides(conn, settings)
            )
    except (sqlite3.Error, SecretError) as error:
        print(f"warn：读控制面设置覆盖失败，本次评测退回环境配置：{error}", file=sys.stderr)

        return settings


def write_reports(report: EvalReport, out_dir: Path) -> tuple[Path, Path]:
    """写 JSON 与 markdown 两份报告，返回两个路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"eval-{report.version}.json"
    markdown_path = out_dir / f"eval-{report.version}.md"
    json_path.write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(report.to_markdown() + "\n", encoding="utf-8")

    return json_path, markdown_path


def _print_summary(report: EvalReport, json_path: Path, markdown_path: Path) -> None:
    """把关键数字打到 stdout：不打开文件也能看到这一跑的结果。"""
    if not report.source_root.is_dir():
        # 源码树缺失时 `literal` 通道整路为空（`_ripgrep` 的 cwd 不存在），指标会偏低。
        print(f"warn：源码树缺失 {report.source_root}，literal 通道整路为空", file=sys.stderr)

    print(f"题目集：{report.set_name}（{len(report.outcomes)} 题）{report.set_path}")
    print(f"版本 {report.version}：索引 {report.index_path}")
    print(f"摘要表：{'有' if report.index_has_summaries else '无'}")
    print(
        f"Top-{report.top_n} 命中率：{report.top_n_hit_rate * 100:.1f}%"
        f"（{report.hits}/{len(report.outcomes)}）"
    )
    print(f"MRR：{report.mrr:.4f}")

    if report.mean_tool_steps is None:
        reason = next(
            (item.reason for item in report.untested_metrics if item.name == MEAN_TOOL_STEPS),
            "未测",
        )
        print(f"平均工具调用轮数：{reason}")
    else:
        print(f"平均工具调用轮数：{report.mean_tool_steps:.2f}")

    print(f"报告：{json_path}")
    print(f"报告：{markdown_path}")
