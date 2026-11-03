"""评测层（issue #16）：30 题人工评测集 + Top-N 命中率 / MRR 报告。

入口是 `mc-librarian eval --version <v> [--set <file>] [--out <dir>] [--limit 5]`（`cli`）。
前两个指标只依赖检索（`detail=locate`，走 `retrieval.search`，与 `/v1/search` 同一条路径）；
第三个指标（平均工具调用轮数）走 `/v1/ask` 的服务端循环，没配 `MC_LIBRARIAN_LLM_*` 时
如实标为「未测」，不编数字、也不跳过整份报告。

模块划分：`questions`（题目集加载与校验）、`metrics`（指标口径）、`runner`（跑检索与问答循环）、
`report`（结果模型 + JSON / markdown 渲染）、`cli`（参数与落盘）。
"""

from __future__ import annotations

from mc_librarian.eval.questions import Question, QuestionSet, load_question_set
from mc_librarian.eval.report import EvalReport, QuestionOutcome
from mc_librarian.eval.runner import EvalError, run_eval

__all__ = [
    "EvalError",
    "EvalReport",
    "Question",
    "QuestionOutcome",
    "QuestionSet",
    "load_question_set",
    "run_eval",
]
