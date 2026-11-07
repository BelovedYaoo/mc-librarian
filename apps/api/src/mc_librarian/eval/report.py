"""评测报告（issue #16 规格 §4）：机器可读 JSON + 人读 markdown。

JSON 带每题结果、汇总指标与运行元数据（版本、索引路径、时间戳、是否含 LLM 指标）；
markdown 是汇总表 + 分类命中 + 失败题列表 + 每题命中的前 N 条。两份报告都从同一份
`EvalReport` 渲染，字段不会各自漂移。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mc_librarian.eval.metrics import HitStats
from mc_librarian.eval.questions import CATEGORIES
from mc_librarian.schemas import Hit

# 汇总表里的指标名：JSON 用这三个键，markdown 用中文标签。
TOP_N_HIT_RATE: Final = "top_n_hit_rate"
MRR: Final = "mrr"
MEAN_TOOL_STEPS: Final = "mean_tool_steps"


@dataclass(frozen=True)
class HitSummary:
    """一条命中的定位信息（`detail=locate` 的全部字段，不含 snippet / summary）。"""

    rank: int
    symbol: str | None
    path: str
    lines: tuple[int, int]
    why: tuple[str, ...]
    score: float

    @classmethod
    def from_hit(cls, rank: int, hit: Hit) -> HitSummary:
        return cls(
            rank=rank,
            symbol=hit.symbol,
            path=hit.path,
            lines=(hit.lines[0], hit.lines[1]),
            why=tuple(hit.why),
            score=hit.score,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "path": self.path,
            "lines": [self.lines[0], self.lines[1]],
            "why": list(self.why),
            "score": self.score,
        }


@dataclass(frozen=True)
class UntestedMetric:
    """没实测的指标与原因（如未配 LLM 的平均工具调用轮数）。"""

    name: str
    reason: str


@dataclass(frozen=True)
class QuestionOutcome:
    """一题的结果：Top-N 命中 + 命中统计 + （配了 LLM 时的）工具调用轮数。"""

    id: str
    category: str
    question: str
    gold: tuple[str, ...]
    hits: tuple[HitSummary, ...]
    stats: HitStats
    tool_steps: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "question": self.question,
            "gold": list(self.gold),
            "hit": self.stats.hit,
            "rank": self.stats.rank,
            "reciprocal_rank": self.stats.reciprocal_rank,
            "tool_steps": self.tool_steps,
            "hits": [hit.to_json() for hit in self.hits],
        }


@dataclass(frozen=True)
class EvalReport:
    """一次评测的完整结果：元数据 + 每题结果 + 汇总指标。"""

    version: str
    index_path: Path
    source_root: Path
    index_has_summaries: bool
    mode: str
    set_name: str
    set_path: Path
    generated_at: str
    top_n: int
    outcomes: tuple[QuestionOutcome, ...]
    top_n_hit_rate: float
    mrr: float
    mean_tool_steps: float | None
    untested_metrics: tuple[UntestedMetric, ...]

    @property
    def has_llm_metrics(self) -> bool:
        return self.mean_tool_steps is not None

    @property
    def hits(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.stats.hit)

    def to_json(self) -> dict[str, Any]:
        """机器可读结果：每题 + 汇总指标 + 运行元数据。"""
        return {
            "version": self.version,
            "index_path": str(self.index_path),
            "source_root": str(self.source_root),
            "index_has_summaries": self.index_has_summaries,
            "question_set": {
                "name": self.set_name,
                "path": str(self.set_path),
                "questions": len(self.outcomes),
            },
            "generated_at": self.generated_at,
            "top_n": self.top_n,
            "metrics": {
                TOP_N_HIT_RATE: self.top_n_hit_rate,
                MRR: self.mrr,
                MEAN_TOOL_STEPS: self.mean_tool_steps,
            },
            "tested_metrics": [name for name in (TOP_N_HIT_RATE, MRR) if self._tested(name)],
            "untested_metrics": [
                {"name": item.name, "reason": item.reason} for item in self.untested_metrics
            ],
            "has_llm_metrics": self.has_llm_metrics,
            "questions": [outcome.to_json() for outcome in self.outcomes],
        }

    def to_markdown(self) -> str:
        """人读报告：汇总表 + 分类命中 + 失败题列表 + 每题命中的前 N 条。"""
        return "\n".join(
            [
                *self._metadata_lines(),
                "",
                "## 汇总",
                "",
                *self._summary_table(),
                "",
                "### 分类命中",
                "",
                *self._category_table(),
                "",
                f"## 失败题（Top-{self.top_n} 内无 gold）",
                "",
                *self._failure_lines(),
                "",
                f"## 每题命中的前 {self.top_n} 条",
                "",
                *self._question_lines(),
            ]
        )

    def _tested(self, name: str) -> bool:
        """指标是否实测：检索指标总是实测，工具轮数看有没有值。"""
        if name == MEAN_TOOL_STEPS:
            return self.mean_tool_steps is not None

        return True

    def _untested_reason(self, name: str) -> str | None:
        return next((item.reason for item in self.untested_metrics if item.name == name), None)

    def _metadata_lines(self) -> list[str]:
        return [
            f"# 评测报告 · {self.version}",
            "",
            f"- 题目集：{self.set_name}（{len(self.outcomes)} 题）`{self.set_path}`",
            f"- 索引：`{self.index_path}`（摘要表：{'有' if self.index_has_summaries else '无'}）",
            f"- 源码树：`{self.source_root}`",
            f"- 生成时间：{self.generated_at}",
            f"- 检索口径：`detail=locate`，Top-{self.top_n}（走 `retrieval.search`，"
            "与 `/v1/search` 同一条服务层路径）",
        ]

    def _summary_table(self) -> list[str]:
        steps_value = "—" if self.mean_tool_steps is None else f"{self.mean_tool_steps:.2f}"
        steps_status = (
            "已测（`/v1/ask` 服务端循环，统计 `AskStep` 条数）"
            if self.mean_tool_steps is not None
            else self._untested_reason(MEAN_TOOL_STEPS) or "未测"
        )

        return [
            "| 指标 | 值 | 状态 |",
            "| --- | --- | --- |",
            f"| Top-{self.top_n} 命中率 | {_percent(self.top_n_hit_rate)}"
            f"（{self.hits}/{len(self.outcomes)}） | 已测（纯检索） |",
            f"| MRR | {self.mrr:.4f} | 已测（纯检索） |",
            f"| 平均工具调用轮数 | {steps_value} | {steps_status} |",
        ]

    def _category_table(self) -> list[str]:
        present = {outcome.category for outcome in self.outcomes}
        ordered = [category for category in CATEGORIES if category in present]
        ordered += sorted(present - set(CATEGORIES))
        rows = ["| 类别 | 题数 | 命中 | 命中率 |", "| --- | --- | --- | --- |"]

        for category in ordered:
            group = [item for item in self.outcomes if item.category == category]
            rows.append(
                f"| {category} | {len(group)} | {sum(1 for item in group if item.stats.hit)} |"
                f" {_percent(hit_rate_of(group))} |"
            )

        return rows

    def _failure_lines(self) -> list[str]:
        failures = [item for item in self.outcomes if not item.stats.hit]

        if not failures:
            return ["（无）"]

        return [
            "| id | 类别 | 问题 | gold |",
            "| --- | --- | --- | --- |",
            *[
                f"| {item.id} | {item.category} | {item.question} | {_gold_text(item.gold)} |"
                for item in failures
            ],
        ]

    def _question_lines(self) -> list[str]:
        blocks: list[str] = []

        for outcome in self.outcomes:
            mark = (
                f"✅ 命中（rank {outcome.stats.rank}）"
                if outcome.stats.hit
                else f"❌ 未命中（Top-{self.top_n} 内无 gold）"
            )
            blocks.append(f"### {outcome.id} · {outcome.category} · {outcome.question} — {mark}")
            blocks.append("")
            blocks.append(f"gold：{_gold_text(outcome.gold)}")

            if outcome.tool_steps is not None:
                blocks.append(f"工具调用轮数：{outcome.tool_steps}")

            blocks.append("")

            if not outcome.hits:
                blocks.append("（无命中）")
                blocks.append("")
                continue

            blocks.append("| # | 符号 | 位置 | why | score |")
            blocks.append("| --- | --- | --- | --- | --- |")

            for hit in outcome.hits:
                symbol = hit.symbol or "—"
                blocks.append(
                    f"| {hit.rank} | `{symbol}` | `{hit.path}`:{hit.lines[0]}-{hit.lines[1]} |"
                    f" {', '.join(hit.why)} | {hit.score:.6f} |"
                )

            blocks.append("")

        return blocks


def hit_rate_of(outcomes: Sequence[QuestionOutcome]) -> float:
    """一组题的命中率（分类表用）；空组记 0。"""
    if not outcomes:
        return 0.0

    return sum(1 for item in outcomes if item.stats.hit) / len(outcomes)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _gold_text(gold: Sequence[str]) -> str:
    return "、".join(f"`{entry}`" for entry in gold)
