"""评测题目集（issue #16 规格 §2）：加载与校验。

题目集是仓库内的 JSON（默认 `apps/api/eval/questions.json`，进版本控制）：30 条中文问题覆盖
六类，每题标 `gold`——期望命中的类 FQN 或 `Class#method`，命中任一即算命中。
格式不合法一律抛 `QuestionSetError`（带题号与字段名），由 CLI 翻成「一句话 + 退出码 2」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# 六类（规格 §2 的表格）。题目集里的 `category` 必须逐字等于其中之一。
CATEGORIES: Final[tuple[str, ...]] = ("机制", "数值", "定位", "跨类调用", "版本差异", "配置读取")

# 默认题目集：`apps/api/eval/questions.json`。本文件在 `apps/api/src/mc_librarian/eval/`，
# 往上四级就是 `apps/api`。
DEFAULT_SET_PATH: Final = Path(__file__).resolve().parents[3] / "eval" / "questions.json"


class QuestionSetError(ValueError):
    """题目集读不到或格式不合法。"""


@dataclass(frozen=True)
class Question:
    """一题：中文问题 + 期望命中的符号（`gold`，命中任一即算命中）。"""

    id: str
    category: str
    question: str
    gold: tuple[str, ...]


@dataclass(frozen=True)
class QuestionSet:
    """一份题目集：名字、题目、来源路径（路径进报告元数据，便于复现）。"""

    name: str
    questions: tuple[Question, ...]
    path: Path


def load_question_set(path: Path) -> QuestionSet:
    """读并校验题目集；任何格式问题都抛 `QuestionSetError`。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise QuestionSetError(f"读不到 {path}（{error.strerror}）") from error

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise QuestionSetError(f"{path} 不是合法 JSON：{error}") from error

    if not isinstance(payload, dict):
        raise QuestionSetError(f"{path} 的顶层必须是 JSON 对象")

    name = payload.get("name")

    if not isinstance(name, str) or not name.strip():
        raise QuestionSetError(f"{path} 缺 `name`（非空字符串）")

    items = payload.get("questions")

    if not isinstance(items, list) or not items:
        raise QuestionSetError(f"{path} 缺 `questions`（非空数组）")

    questions = tuple(
        _parse_question(item, index, path) for index, item in enumerate(items, start=1)
    )
    _reject_duplicate_ids(questions, path)

    return QuestionSet(name=name, questions=questions, path=path)


def _parse_question(item: Any, index: int, path: Path) -> Question:
    """第 `index` 题（1-based，与人工数的一致）→ `Question`。"""
    where = f"{path} 第 {index} 题"

    if not isinstance(item, dict):
        raise QuestionSetError(f"{where}必须是 JSON 对象")

    question_id = item.get("id")

    if not isinstance(question_id, str) or not question_id.strip():
        raise QuestionSetError(f"{where}缺 `id`（非空字符串）")

    where = f"{where}（id={question_id}）"
    category = item.get("category")

    if category not in CATEGORIES:
        raise QuestionSetError(
            f"{where}的 `category` 必须是六类之一（{'、'.join(CATEGORIES)}），当前是 {category!r}"
        )

    text = item.get("question")

    if not isinstance(text, str) or not text.strip():
        raise QuestionSetError(f"{where}缺 `question`（非空字符串）")

    gold = item.get("gold")

    if not isinstance(gold, list) or not gold:
        raise QuestionSetError(f"{where}缺 `gold`（至少一个类 FQN 或 `Class#method`）")

    for entry in gold:
        if not isinstance(entry, str) or not entry.strip():
            raise QuestionSetError(f"{where}的 `gold` 只能是非空字符串，当前含 {entry!r}")

    return Question(
        id=question_id,
        category=category,
        question=text,
        gold=tuple(gold),
    )


def _reject_duplicate_ids(questions: tuple[Question, ...], path: Path) -> None:
    """id 是报告里的题目标识，重复就没法定位到具体哪一题。"""
    seen: set[str] = set()

    for question in questions:
        if question.id in seen:
            raise QuestionSetError(f"{path} 里 id 重复：{question.id}")

        seen.add(question.id)
