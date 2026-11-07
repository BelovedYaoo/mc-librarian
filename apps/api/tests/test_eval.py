"""评测集与报告（issue #16）：指标口径、题目集校验、报告渲染、无 LLM 路径与稳定性。

端到端用 `tests/fixtures/source` 的假源码树建一个小索引（不联网、不碰真实数据），
真实 26.3 / 1.20.1 的基线数据见 issue #16 的评论。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import tool_call
from mc_librarian.config import Settings
from mc_librarian.db import Database
from mc_librarian.eval import cli as eval_cli
from mc_librarian.eval import metrics, questions
from mc_librarian.eval.metrics import HitStats
from mc_librarian.eval.questions import QuestionSetError, load_question_set
from mc_librarian.eval.report import EvalReport, HitSummary, QuestionOutcome, UntestedMetric
from mc_librarian.eval.runner import UNTESTED_LLM_REASON, EvalError, run_eval
from mc_librarian.indexing.build import build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"
REPO_SET_PATH = Path(__file__).parents[1] / "eval" / "questions.json"

VERSION = "26.3"


def silent(_level: str, _message: str) -> None:
    pass


def make_settings(data_dir: Path, *, llm: bool) -> Settings:
    """评测用的设置：`llm=False` 模拟没配 `MC_LIBRARIAN_LLM_*` 的环境。"""
    if llm:
        return Settings(
            data_dir=data_dir,
            llm_base_url="http://fake-llm.invalid/v1",
            llm_model="fake-model",
            llm_api_key="fake-key",
        )

    return Settings(data_dir=data_dir)


def build_fixture_index(data_dir: Path) -> Path:
    """把 fixture 源码树就位并建索引；返回索引文件路径。"""
    root = source_root(data_dir, VERSION)
    place_source_tree(FIXTURE_SOURCE, root)
    index = index_path(data_dir, VERSION)
    build_index(root, index, log=silent)

    return index


def write_set(path: Path, items: list[dict[str, Any]]) -> Path:
    """写一份题目集 JSON（只给必填字段，其余用默认）。"""
    payload = {"name": "测试题目集", "questions": items}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    return path


@pytest.fixture
def fixture_set(tmp_path: Path) -> Path:
    """三题：一题符号通道必中、一题中文问句必不中、一题 `Class#method` 必中。"""
    return write_set(
        tmp_path / "questions.json",
        [
            {
                "id": "hit-01",
                "category": "定位",
                "question": "Mob",
                "gold": ["net.minecraft.world.Mob"],
            },
            {
                "id": "miss-01",
                "category": "机制",
                "question": "刷怪上限是怎么算的",
                "gold": ["net.minecraft.world.NaturalSpawner"],
            },
            {
                "id": "hit-02",
                "category": "跨类调用",
                "question": "spawn",
                "gold": ["net.minecraft.world.World#spawn"],
            },
        ],
    )


# --- 指标口径（规格 §3 / §8 第 1 条） -------------------------------------------------


def test_matches_gold_accepts_class_and_member() -> None:
    gold = ["net.minecraft.world.Mob"]

    assert metrics.matches_gold("net.minecraft.world.Mob", gold)
    assert metrics.matches_gold("net.minecraft.world.Mob#getName", gold)
    # 嵌套类是另一个类，不算命中。
    assert not metrics.matches_gold("net.minecraft.world.Mob$Inner", gold)
    assert not metrics.matches_gold("net.minecraft.world.World", gold)
    assert not metrics.matches_gold(None, gold)


def test_matches_gold_requires_exact_member_for_class_hash_method() -> None:
    gold = ["net.minecraft.world.World#spawn"]

    assert metrics.matches_gold("net.minecraft.world.World#spawn", gold)
    assert not metrics.matches_gold("net.minecraft.world.World", gold)
    assert not metrics.matches_gold("net.minecraft.world.World#spawnOther", gold)


def test_evaluate_hits_reports_rank_and_reciprocal_rank() -> None:
    stats = metrics.evaluate_hits(["a.A", "b.B", "c.C"], ["c.C"])

    assert stats.hit
    assert stats.rank == 3
    assert stats.reciprocal_rank == pytest.approx(1 / 3)


def test_evaluate_hits_without_gold_in_top_n_is_zero() -> None:
    """Top-N 内没有 gold：MRR 记 0（规格 §3 的边界）。"""
    stats = metrics.evaluate_hits(["a.A", "b.B"], ["c.C"])

    assert not stats.hit
    assert stats.rank is None
    assert stats.reciprocal_rank == 0.0


def test_hit_rate_and_mrr_aggregate_over_questions() -> None:
    stats = [
        metrics.evaluate_hits(["a.A"], ["a.A"]),
        metrics.evaluate_hits(["a.A"], ["b.B"]),
        metrics.evaluate_hits(["x.X", "y.Y"], ["y.Y"]),
    ]

    assert metrics.hit_rate(stats) == pytest.approx(2 / 3)
    assert metrics.mean_reciprocal_rank(stats) == pytest.approx((1 + 0 + 0.5) / 3)


def test_mean_tool_steps_is_none_without_measurement() -> None:
    assert metrics.mean_tool_steps([]) is None
    assert metrics.mean_tool_steps([6, 4]) == 5.0


# --- 题目集格式校验（规格 §8 第 4 条） ----------------------------------------------


def test_load_question_set_rejects_missing_gold(tmp_path: Path) -> None:
    path = write_set(
        tmp_path / "q.json",
        [{"id": "a-1", "category": "机制", "question": "刷怪上限是怎么算的"}],
    )

    with pytest.raises(QuestionSetError) as excinfo:
        load_question_set(path)

    message = str(excinfo.value)
    assert "a-1" in message
    assert "gold" in message


def test_load_question_set_rejects_unknown_category(tmp_path: Path) -> None:
    path = write_set(
        tmp_path / "q.json",
        [{"id": "a-1", "category": "性能", "question": "刷怪上限", "gold": ["x.Y"]}],
    )

    with pytest.raises(QuestionSetError) as excinfo:
        load_question_set(path)

    message = str(excinfo.value)
    assert "a-1" in message
    assert "性能" in message
    assert "机制" in message


def test_load_question_set_rejects_duplicate_id(tmp_path: Path) -> None:
    path = write_set(
        tmp_path / "q.json",
        [
            {"id": "a-1", "category": "机制", "question": "一", "gold": ["x.Y"]},
            {"id": "a-1", "category": "数值", "question": "二", "gold": ["x.Z"]},
        ],
    )

    with pytest.raises(QuestionSetError, match="id 重复"):
        load_question_set(path)


def test_load_question_set_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(QuestionSetError, match="读不到"):
        load_question_set(tmp_path / "nope.json")


def test_repository_question_set_covers_six_categories() -> None:
    """仓库里那份 30 题必须真的覆盖六类，且每题都有 gold（规格 §2）。"""
    question_set = load_question_set(REPO_SET_PATH)

    assert len(question_set.questions) == 30

    counts = {category: 0 for category in questions.CATEGORIES}

    for question in question_set.questions:
        assert question.gold, f"{question.id} 缺 gold"
        counts[question.category] += 1

    assert counts == {category: 5 for category in questions.CATEGORIES}


# --- 报告渲染（规格 §8 第 2 条） ----------------------------------------------------


def make_report(
    tmp_path: Path, *, steps: float | None, untested: tuple[UntestedMetric, ...] = ()
) -> EvalReport:
    """手搭一份报告：一题命中（rank 1）、一题未命中。"""
    hit = QuestionOutcome(
        id="hit-01",
        category="定位",
        question="Mob",
        gold=("net.minecraft.world.Mob",),
        hits=(
            HitSummary(
                rank=1,
                symbol="net.minecraft.world.Mob",
                path="net/minecraft/world/Mob.java",
                lines=(3, 18),
                why=("symbol", "literal"),
                score=1.0,
            ),
        ),
        stats=HitStats(hit=True, rank=1, reciprocal_rank=1.0),
        tool_steps=None if steps is None else 2,
    )
    miss = QuestionOutcome(
        id="miss-01",
        category="机制",
        question="刷怪上限是怎么算的",
        gold=("net.minecraft.world.NaturalSpawner",),
        hits=(),
        stats=HitStats(hit=False, rank=None, reciprocal_rank=0.0),
        tool_steps=None if steps is None else 4,
    )

    return EvalReport(
        version=VERSION,
        index_path=index_path(tmp_path, VERSION),
        source_root=source_root(tmp_path, VERSION),
        index_has_summaries=False,
        mode="lexical",
        set_name="测试题目集",
        set_path=tmp_path / "questions.json",
        generated_at="2026-01-01T00:00:00Z",
        top_n=5,
        outcomes=(hit, miss),
        top_n_hit_rate=0.5,
        mrr=0.5,
        mean_tool_steps=steps,
        untested_metrics=untested,
    )


def test_report_json_carries_metrics_and_metadata(tmp_path: Path) -> None:
    payload = make_report(tmp_path, steps=None).to_json()

    assert payload["version"] == VERSION
    assert payload["index_path"] == str(index_path(tmp_path, VERSION))
    assert payload["generated_at"] == "2026-01-01T00:00:00Z"
    assert payload["has_llm_metrics"] is False
    assert payload["metrics"] == {
        "top_n_hit_rate": 0.5,
        "mrr": 0.5,
        "mean_tool_steps": None,
    }
    assert payload["tested_metrics"] == ["top_n_hit_rate", "mrr"]
    assert payload["questions"][0]["hits"][0]["why"] == ["symbol", "literal"]
    assert payload["questions"][1]["hits"] == []


def test_report_markdown_has_summary_failures_and_hits(tmp_path: Path) -> None:
    text = make_report(tmp_path, steps=None).to_markdown()

    assert "# 评测报告 · 26.3" in text
    assert "Top-5 命中率 | 50.0%（1/2）" in text
    assert "## 失败题（Top-5 内无 gold）" in text
    assert "miss-01" in text
    assert "## 每题命中的前 5 条" in text
    assert "`net.minecraft.world.Mob`" in text
    assert "net/minecraft/world/Mob.java`:3-18" in text


def test_report_markdown_marks_untested_metric(tmp_path: Path) -> None:
    report = make_report(
        tmp_path,
        steps=None,
        untested=(UntestedMetric(name="mean_tool_steps", reason=UNTESTED_LLM_REASON),),
    )

    assert f"平均工具调用轮数 | — | {UNTESTED_LLM_REASON}" in report.to_markdown()


# --- 端到端（规格 §8 第 3 条 / §5 稳定性） ------------------------------------------


def run_cli(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    set_path: Path,
    out_dir: Path,
    *extra: str,
) -> int:
    monkeypatch.setattr(eval_cli, "get_settings", lambda: settings)

    return eval_cli.main(
        ["--version", VERSION, "--set", str(set_path), "--out", str(out_dir), *extra]
    )


def test_cli_writes_both_reports_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_set: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """没配 LLM：报告照常出、第三个指标标为未测、退出码 0（规格 §8 第 3 条）。"""
    settings = make_settings(tmp_path / "data", llm=False)
    build_fixture_index(settings.data_dir)
    out_dir = tmp_path / "out"

    code = run_cli(settings, monkeypatch, fixture_set, out_dir)

    assert code == 0
    stdout = capsys.readouterr().out
    assert UNTESTED_LLM_REASON in stdout

    payload = json.loads((out_dir / f"eval-{VERSION}.json").read_text(encoding="utf-8"))
    assert payload["has_llm_metrics"] is False
    assert payload["metrics"]["mean_tool_steps"] is None
    assert payload["metrics"]["top_n_hit_rate"] == pytest.approx(2 / 3)
    assert payload["metrics"]["mrr"] > 0
    assert payload["untested_metrics"] == [
        {"name": "mean_tool_steps", "reason": UNTESTED_LLM_REASON}
    ]
    assert all(item["tool_steps"] is None for item in payload["questions"])

    markdown = (out_dir / f"eval-{VERSION}.md").read_text(encoding="utf-8")
    assert UNTESTED_LLM_REASON in markdown
    assert "失败题" in markdown


def test_cli_measures_tool_steps_when_llm_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_set: Path,
    fake_chat: Any,
) -> None:
    """配了 LLM：走 `/v1/ask` 的循环，按每题的工具调用轮数取平均。"""
    settings = make_settings(tmp_path / "data", llm=True)
    # 步数取服务端设置（也是 `/v1/ask` 在 `AskRequest` 省略 `max_steps` 时用的值）。用非默认值
    # 才看得出不是硬编码的 6；而 `AskRequest.max_steps` 本身是 `None`，拿它当整数用会崩在
    # `range(None)` 上（#11 去掉了契约里的 default）。
    settings.ask_max_steps = 3
    build_fixture_index(settings.data_dir)
    # 每次都要求调一次 search：循环跑满 max_steps（3 轮）后强制收尾。
    fake_chat.always_tools = tool_call("search", query="Mob")["tool_calls"]
    out_dir = tmp_path / "out"

    assert run_cli(settings, monkeypatch, fixture_set, out_dir) == 0

    payload = json.loads((out_dir / f"eval-{VERSION}.json").read_text(encoding="utf-8"))
    assert payload["has_llm_metrics"] is True
    assert payload["metrics"]["mean_tool_steps"] == 3.0
    assert [item["tool_steps"] for item in payload["questions"]] == [3, 3, 3]


def test_cli_layers_database_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_set: Path,
    fake_chat: Any,
) -> None:
    """CLI 叠上 `settings` 表覆盖：设置页改的步数同样作用于评测（与 `/v1/ask` 同值）。"""
    settings = make_settings(tmp_path / "data", llm=True)
    build_fixture_index(settings.data_dir)
    database = Database.for_data_dir(settings.data_dir)
    database.initialize()

    with database.connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value_json, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("ask.max_steps", "3", "2026-01-01T00:00:00Z"),
        )

    fake_chat.always_tools = tool_call("search", query="Mob")["tool_calls"]
    out_dir = tmp_path / "out"

    assert run_cli(settings, monkeypatch, fixture_set, out_dir) == 0

    payload = json.loads((out_dir / f"eval-{VERSION}.json").read_text(encoding="utf-8"))
    # 环境默认是 6；库里覆盖 3 生效才说明 CLI 真的叠了覆盖。
    assert payload["metrics"]["mean_tool_steps"] == 3.0


def _markdown_without_generated_at(run_dir: Path) -> str:
    """markdown 报告去掉 `- 生成时间：` 一行后的正文。

    `generated_at` 是秒级时间戳，每次运行都不同——它不是稳定性的一部分，两次运行跨过一秒
    边界时整文件比较会假红（实测约 50%）。其余行逐字节比较，指标漂移照样能抓到。
    """
    lines = (run_dir / f"eval-{VERSION}.md").read_text(encoding="utf-8").splitlines()

    return "\n".join(line for line in lines if not line.startswith("- 生成时间："))


def test_cli_results_are_identical_on_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_set: Path
) -> None:
    """稳定性（规格 §5）：同一索引跑两次，Top-N 与 MRR 逐题完全相同。"""
    settings = make_settings(tmp_path / "data", llm=False)
    build_fixture_index(settings.data_dir)

    assert run_cli(settings, monkeypatch, fixture_set, tmp_path / "run-1") == 0
    assert run_cli(settings, monkeypatch, fixture_set, tmp_path / "run-2") == 0

    first = json.loads((tmp_path / "run-1" / f"eval-{VERSION}.json").read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "run-2" / f"eval-{VERSION}.json").read_text(encoding="utf-8"))

    assert first["metrics"] == second["metrics"]
    assert [item["hits"] for item in first["questions"]] == [
        item["hits"] for item in second["questions"]
    ]
    # markdown 侧只排除 `- 生成时间：` 一行（`generated_at` 每次运行都不同，不是稳定性的一部分）；
    # JSON 侧本来就只比 `metrics` 与逐题 `hits`，从不比整份文档，同样没把时间戳算进去。
    assert _markdown_without_generated_at(tmp_path / "run-1") == _markdown_without_generated_at(
        tmp_path / "run-2"
    )


def test_cli_exits_2_when_index_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_set: Path
) -> None:
    settings = make_settings(tmp_path / "data", llm=False)

    assert run_cli(settings, monkeypatch, fixture_set, tmp_path / "out") == 2


def test_cli_exits_2_on_bad_question_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path / "data", llm=False)
    build_fixture_index(settings.data_dir)
    bad = write_set(
        tmp_path / "bad.json",
        [{"id": "a-1", "category": "机制", "question": "一"}],
    )

    assert run_cli(settings, monkeypatch, bad, tmp_path / "out") == 2


def test_run_eval_rejects_unknown_version(tmp_path: Path, fixture_set: Path) -> None:
    """`run_eval` 本身也守索引存在性（不只靠 CLI）。"""
    settings = make_settings(tmp_path / "data", llm=False)

    with pytest.raises(EvalError, match="索引文件不存在"):
        run_eval(settings, VERSION, load_question_set(fixture_set), top_n=5)
