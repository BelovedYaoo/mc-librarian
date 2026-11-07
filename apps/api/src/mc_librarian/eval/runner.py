"""跑一次评测（issue #16 规格 §3）：检索指标 + （配了 LLM 时）工具调用轮数。

- 前两个指标走 `retrieval.search`——和 `/v1/search` 同一条服务层路径，`detail=locate`、
  `limit=Top-N`，query 就是题目原文（不做改写）；
- 第三个指标走 `/v1/ask` 的服务端循环（`answer.AskRun` + `ToolBox`），统计每题的 `AskStep`
  条数——一次工具调用一条（SSE 里同一次调用会出两个 `step` 事件：调用前带 args、调用后带
  ok/hits；`context_budget` 伪事件也不算）；
- 没配 `MC_LIBRARIAN_LLM_*` 时不碰 LLM，第三个指标如实记成「未测」，其余指标与报告照常出。

配了 LLM 时每题跑一次问答（30 题最多 30 次循环、每次最多 `settings.ask.max_steps` 轮，
即 `AskRequest.max_steps` 省略时服务端用的那个值；CLI 传入的 `settings` 已叠好 `settings`
表里的覆盖，见 `eval.cli.with_database_overrides`）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Final

from mc_librarian.answer import AskRun, OpenAiCompatChatClient, ToolBox
from mc_librarian.config import (
    LlmConfig,
    LlmConfigError,
    Settings,
    resolve_llm_config,
)
from mc_librarian.db import utc_now
from mc_librarian.eval.metrics import (
    evaluate_hits,
    hit_rate,
    mean_reciprocal_rank,
    mean_tool_steps,
)
from mc_librarian.eval.questions import Question, QuestionSet
from mc_librarian.eval.report import (
    MEAN_TOOL_STEPS,
    EvalReport,
    HitSummary,
    QuestionOutcome,
    UntestedMetric,
)
from mc_librarian.indexing import query
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.retrieval import search, search_smart
from mc_librarian.schemas import SearchRequest

# 没配 LLM 时写进报告的未测原因（规格 §3 定死的文案）。
UNTESTED_LLM_REASON: Final = "未测（需配置 MC_LIBRARIAN_LLM_*）"


class EvalError(RuntimeError):
    """评测跑不起来（索引文件不存在等）；CLI 翻成「一句话 + 退出码 2」。"""


def run_eval(
    settings: Settings,
    version: str,
    question_set: QuestionSet,
    *,
    top_n: int,
    mode: str = "lexical",
    measure_ask: bool = True,
) -> EvalReport:
    """跑完题目集，产出报告。索引以只读打开，跑完关闭。"""
    path = index_path(settings.data_dir, version)

    if not path.is_file():
        raise EvalError(f"索引文件不存在：{path}（先跑 mc-librarian index --version {version}）")

    root = source_root(settings.data_dir, version)
    # 问答的工具调用跑在 `asyncio.to_thread` 的工作线程里，连接要允许跨线程（同 `/v1/ask`）。
    index = query.open_readonly(path, check_same_thread=False)

    try:
        has_summaries = query.has_table(index, "summaries_fts")
        hits = [
            _retrieval_hits(index, settings, version, question, top_n, mode)
            for question in question_set.questions
        ]
        if measure_ask:
            steps, untested = _tool_steps(settings, version, index, question_set.questions)
        else:
            steps = []
            untested = [
                UntestedMetric(name="平均工具调用轮数", reason="跳过（--no-ask）")
            ]
    finally:
        index.close()

    outcomes = tuple(
        QuestionOutcome(
            id=question.id,
            category=question.category,
            question=question.question,
            gold=question.gold,
            hits=hit_summaries,
            stats=evaluate_hits([hit.symbol for hit in hit_summaries], question.gold),
            # `steps` 为空 = 未测（没配 LLM），不用 0 冒充。
            tool_steps=steps[position] if steps else None,
        )
        for position, (question, hit_summaries) in enumerate(
            zip(question_set.questions, hits, strict=True)
        )
    )
    stats = [outcome.stats for outcome in outcomes]

    return EvalReport(
        version=version,
        index_path=path,
        source_root=root,
        index_has_summaries=has_summaries,
        mode=mode,
        set_name=question_set.name,
        set_path=question_set.path,
        generated_at=utc_now(),
        top_n=top_n,
        outcomes=outcomes,
        top_n_hit_rate=hit_rate(stats),
        mrr=mean_reciprocal_rank(stats),
        mean_tool_steps=mean_tool_steps(steps),
        untested_metrics=tuple(untested),
    )


def _retrieval_hits(
    index: sqlite3.Connection,
    settings: Settings,
    version: str,
    question: Question,
    top_n: int,
    mode: str = "lexical",
) -> tuple[HitSummary, ...]:
    """一题的 Top-N 命中（`detail=locate`，与 `/v1/search` 同一条路径）。"""
    request = SearchRequest(
        version=version, query=question.question, detail="locate", limit=top_n,
        mode="smart" if mode == "smart" else "lexical",
    )
    response = search_smart(index, settings, request) if request.mode == "smart" else search(
        index, settings, request
    )

    return tuple(HitSummary.from_hit(rank, hit) for rank, hit in enumerate(response.hits, start=1))


def _tool_steps(
    settings: Settings,
    version: str,
    index: sqlite3.Connection,
    questions: tuple[Question, ...],
) -> tuple[list[int], list[UntestedMetric]]:
    """每题的工具调用轮数；没配 LLM 时返回空列表 + 一条「未测」说明。"""
    try:
        config = resolve_llm_config(settings)
    except LlmConfigError:
        return [], [UntestedMetric(name=MEAN_TOOL_STEPS, reason=UNTESTED_LLM_REASON)]

    return asyncio.run(_measure_steps(settings, version, index, questions, config)), []


async def _measure_steps(
    settings: Settings,
    version: str,
    index: sqlite3.Connection,
    questions: tuple[Question, ...],
    config: LlmConfig,
) -> list[int]:
    """逐个问题跑 `/v1/ask` 的循环，数每题 `AskStep` 的条数。"""
    client = OpenAiCompatChatClient(config)
    toolbox = ToolBox(index, settings, version)
    counts: list[int] = []

    for question in questions:
        # 步数与答案上限用服务端设置（`settings.ask.max_steps` / `max_answer_tokens`，也是
        # `/v1/ask` 在 `AskRequest` 省略这两个字段时用的值；CLI 传入的是叠好 `settings` 表
        # 覆盖的有效配置，所以设置页改的值同样作用于评测）。契约里的 `AskRequest.max_steps`
        # 是 `int | None`（#11 去掉了 default），拿它当整数用会崩在 `range(None)` 上。
        run = AskRun(
            client=client,
            toolbox=toolbox,
            question=question.question,
            version=version,
            max_steps=settings.ask_max_steps,
            max_answer_tokens=settings.ask_max_answer_tokens,
            max_context_tokens=settings.ask_max_context_tokens,
            timeout_s=float(settings.ask_timeout_s),
            llm_timeout_s=settings.llm_timeout_s,
        )

        async for _event in run.stream():
            pass

        counts.append(len(run.steps))

    return counts
