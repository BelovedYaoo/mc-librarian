"""中文摘要层（issue #7）：格式、幂等、失败保留、分词、分批、降级与覆盖率。

LLM 调用由 conftest 的 `fake_llm` fixture 换掉（不联网）；索引与源码都落在 `tmp_path`，
不碰仓库里的真实数据根（`mc-librarian-data/`）。

逐条对应验收标准：摘要行格式 / 幂等不重复调用 / 单类失败保留其余 / 两字词命中 /
未配置 LLM 明确报错 / 融合（融合那条在 `test_search.py`，走真实检索链路）。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Final

import jieba
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SECRET
from mc_librarian.config import (
    LLM_API_KEY_ENV,
    LLM_BASE_URL_ENV,
    LLM_MODEL_ENV,
    LlmConfigError,
    Settings,
    resolve_llm_config,
)
from mc_librarian.indexing import summarize
from mc_librarian.indexing.build import build_index
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.indexing.summarize import ClassDigest, SummaryRow
from mc_librarian.main import create_app
from mc_librarian.retrieval import channels

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"
VERSION = "26.3"
TERMINAL = {"succeeded", "failed", "canceled"}
WAIT_TIMEOUT_S = 10.0

# 游戏机制词（规格 §1：摘要必须带上这些词，写成「X 是一个枚举类」就是失败）。
MECHANIC_WORDS = ("刷怪", "生成", "掉落", "刻", "区块", "附魔", "红石")

# 不合格摘要的典型形态（规格 §1 的三行格式）：每种形态各一条用例，断言该类的失败被记录、
# 且不入库（`parse_batch_response` → `validate_summary`）。前缀**不是**判定依据（issue #23：
# 真实模型经常写成三行自由文本），所以这里不再把「缺前缀」当不合格。
BAD_SUMMARIES: Final[dict[str, str]] = {
    "行数不是 3": "刷怪上限：每区块生成预算\n关键成员：spawnInterval",
    "第 1 行没有中文": "Spawn budget per chunk\n机制：按刻调度\n关键成员：spawnInterval",
    "第 2 行没有中文": "刷怪上限：每区块生成预算\nby ticking\n关键成员：spawnInterval",
    "第 3 行没有英文标识符": (
        "刷怪上限：每区块生成预算\n机制：按刻调度\n关键成员：生成间隔与上限"
    ),
    "第 1 行太短": "刷怪\n机制：按刻调度\n关键成员：spawnInterval",
    "第 1 行只是复述类名": (
        "net.minecraft.world.Mob\n机制：按刻调度\n关键成员：spawnInterval"
    ),
}


def silent(_level: str, _message: str) -> None:
    pass


def run_summarize(settings: Settings, index: Path) -> summarize.SummaryStats:
    """用真实客户端（HTTP 被 `fake_llm` 换掉）跑一次摘要生成。"""

    def factory() -> summarize.OpenAiCompatClient:
        return summarize.OpenAiCompatClient(resolve_llm_config(settings), timeout=5.0)

    return asyncio.run(
        summarize.summarize_index(
            index,
            client_factory=factory,
            batch_size=settings.summary_batch_size,
            concurrency=settings.summary_concurrency,
            log=silent,
        )
    )


def read_summaries(index: Path) -> dict[str, str]:
    connection = sqlite3.connect(index)
    connection.row_factory = sqlite3.Row

    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summaries'"
        ).fetchone()

        if exists is None:
            return {}

        return {
            str(row["fqn"]): str(row["text_zh"])
            for row in connection.execute("SELECT fqn, text_zh FROM summaries")
        }
    finally:
        connection.close()


def read_fts_rows(index: Path) -> list[tuple[str, str]]:
    connection = sqlite3.connect(index)

    try:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT fqn, text_zh FROM summaries_fts")
        ]
    finally:
        connection.close()


def _class_id(index: Path, fqn: str) -> int:
    connection = sqlite3.connect(index)

    try:
        row = connection.execute("SELECT id FROM classes WHERE fqn = ?", (fqn,)).fetchone()
    finally:
        connection.close()

    assert row is not None, f"索引里没有这个类：{fqn}"

    return int(row[0])


def digest(fqn: str, package: str | None) -> ClassDigest:
    return ClassDigest(
        class_id=0,
        fqn=fqn,
        package=package,
        superclass=None,
        interfaces=(),
        field_names=(),
        field_signatures=(),
        method_signatures=(),
        content_hash="hash",
    )


def submit(client: TestClient, source_dir: Path) -> Any:
    return client.post(
        "/v1/admin/jobs",
        json={
            "kind": "index",
            "version": VERSION,
            "options": {"source_dir": str(source_dir)},
        },
    )


def wait_for_terminal(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        last = client.get(f"/v1/admin/jobs/{job_id}").json()

        if last["status"] in TERMINAL:
            return last

        time.sleep(0.05)

    raise AssertionError(f"任务 {job_id} 未在超时内结束：{last}")


def setup_admin(client: TestClient) -> None:
    created = client.post(
        "/v1/setup", json={"username": "admin", "password": "admin-password-1"}
    )

    assert created.status_code == 201, created.text


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    root = tmp_path / "src-input" / VERSION
    shutil.copytree(FIXTURE_SOURCE, root)

    return root


@pytest.fixture
def indexed(settings: Settings) -> Path:
    """把 fixture 源码树建成索引（6 个类，全在 `net.minecraft.world`）。"""
    root = source_root(settings.data_dir, VERSION)
    shutil.copytree(FIXTURE_SOURCE, root)
    index = index_path(settings.data_dir, VERSION)
    build_index(root, index, log=silent)

    return index


# ----------------------------------------------------------------------
# 摘要行格式与存储
# ----------------------------------------------------------------------


def test_summary_is_three_lines_with_a_mechanic_word(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """摘要固定 3 行、中文在前、带游戏机制词（验收，规格 §1）。"""
    stats = run_summarize(settings, indexed)

    assert stats.generated == 6
    assert stats.failed == 0

    summaries = read_summaries(indexed)

    assert summaries, "应该有摘要入库"
    mob = summaries["net.minecraft.world.Mob"]

    assert len(mob.splitlines()) == summarize.SUMMARY_LINE_COUNT
    assert any(word in mob for word in MECHANIC_WORDS)
    # 中文在前、英文标识符在后：假响应的第三行是「关键成员：…」。
    assert mob.splitlines()[2].startswith("关键成员：")


@pytest.mark.parametrize("shape", sorted(BAD_SUMMARIES))
def test_malformed_summary_is_a_per_class_failure_and_is_not_stored(
    shape: str, settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """格式不合格的摘要按单类失败处理：记失败原因、不入库、同批其余照常（规格 §1/§3）。"""
    fake_llm.overrides = {"net.minecraft.world.Mob": BAD_SUMMARIES[shape]}

    stats = run_summarize(settings, indexed)

    assert stats.failed == 1, stats.failures
    assert stats.generated == 5
    assert any(
        item.startswith("net.minecraft.world.Mob：摘要格式不合格") for item in stats.failures
    ), stats.failures
    assert "net.minecraft.world.Mob" not in read_summaries(indexed)
    assert all(fqn != "net.minecraft.world.Mob" for fqn, _ in read_fts_rows(indexed))


def test_valid_summary_passes_the_format_gate() -> None:
    """合法三行摘要不被格式闸门拦住（否则就是闸门写得太紧）。"""
    text = "刷怪上限：每区块生成预算\n机制：按 mob cap 与玩家距离调度\n关键成员：spawnInterval"

    assert summarize.validate_summary(text, fqn="net.minecraft.world.Mob") is None


def test_unprefixed_three_line_summary_passes_the_gate() -> None:
    """三行自由文本（不带「职责：/机制：/关键成员：」前缀）同样合格（issue #23 实测）。

    真实模型经常不写前缀。把「有没有前缀」当判定依据会把整批摘要全拒掉，闸门因此只校验实质：
    前两行有中文、第 3 行有英文标识符。
    """
    text = (
        "控制每个区块的刷怪上限与生成节奏\n"
        "按生物群系权重挑选候选，受 mob cap 与玩家距离约束\n"
        "spawnInterval, getMaxInstancesPerChunk"
    )

    assert summarize.validate_summary(text, fqn="net.minecraft.world.Mob") is None


def test_members_line_without_an_identifier_is_rejected() -> None:
    """第 3 行（关键成员）没有英文标识符就是不合格：这是该行唯一的可靠实质。"""
    text = "控制每个区块的刷怪上限\n按生物群系权重挑选候选\n生成间隔与最大实例数"
    problem = summarize.validate_summary(text, fqn="net.minecraft.world.Mob")

    assert problem is not None and "英文标识符" in problem


@pytest.mark.parametrize("head", ["管理玩家背包", "刷怪上限与预算", "管理玩家背包物品"])
def test_short_but_valid_head_passes_the_gate(head: str) -> None:
    """简洁但合格的第 1 行（6/7/8 字符）不被格式闸门拒掉：拒好摘要的闸门比没有更糟。"""
    text = f"{head}\n机制：按每刻预算调度\n关键成员：spawnInterval"

    assert summarize.validate_summary(text, fqn="net.minecraft.world.Mob") is None


def test_head_below_the_floor_is_still_rejected() -> None:
    """闸门放宽到 4 字符，但「刷怪上」这类过短的复述仍然挡掉。"""
    text = "刷怪上\n机制：按每刻预算调度\n关键成员：spawnInterval"
    problem = summarize.validate_summary(text, fqn="net.minecraft.world.Mob")

    assert problem is not None and "第 1 行太短" in problem


def test_wrapped_member_list_is_merged_into_line_three(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """第 3 行成员列表折行成第 4 行：接受并把第 4 行并回第 3 行，而不是整条判失败。"""
    fake_llm.overrides = {
        "net.minecraft.world.Mob": (
            "刷怪上限：每区块生成预算\n"
            "机制：按 mob cap 与玩家距离调度\n"
            "关键成员：spawnInterval,\n"
            "getName"
        )
    }

    stats = run_summarize(settings, indexed)

    assert stats.failed == 0, stats.failures

    lines = read_summaries(indexed)["net.minecraft.world.Mob"].splitlines()

    assert len(lines) == 3
    assert lines[2].startswith("关键成员：")
    assert "getName" in lines[2]


def test_overlong_head_is_truncated_per_line_and_still_three_lines(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """超长第 1 行：逐行截断后再过闸门，入库仍是 3 行（整体截断会把第 2/3 行一起切掉）。"""
    long_head = "刷怪" * (summarize.SUMMARY_MAX_CHARS // 2 + 100)
    fake_llm.overrides = {
        "net.minecraft.world.Mob": (
            f"{long_head}\n机制：按 mob cap 与玩家距离调度\n关键成员：spawnInterval"
        )
    }

    stats = run_summarize(settings, indexed)

    assert stats.failed == 0, stats.failures

    lines = read_summaries(indexed)["net.minecraft.world.Mob"].splitlines()

    assert len(lines) == 3
    assert len(lines[0]) <= summarize.SUMMARY_MAX_CHARS
    assert lines[1].startswith("机制：")
    assert lines[2].startswith("关键成员：")


def test_summaries_fts_stores_jieba_segmented_text(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """FTS5 里存的是 jieba 预分词结果（空格分隔），两字词才可能命中（规格 §6）。"""
    run_summarize(settings, indexed)

    rows = read_fts_rows(indexed)

    assert len(rows) == 6
    _, segmented = next(row for row in rows if row[0] == "net.minecraft.world.Mob")

    assert "刷怪" in segmented.split(), "「刷怪」必须是一个独立的 FTS token"
    assert " " in segmented


def test_segment_keeps_two_char_words_together() -> None:
    """`segment` 是查询与入库共用的分词：两字词（「刷怪」）不被拆成单字。"""
    tokens = summarize.segment("刷怪上限").split()

    assert "刷怪" in tokens


def test_segment_does_not_reset_jieba_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """`jieba.setLogLevel` 已移到模块导入时：`segment` 逐类调用，不再每次重设。"""
    calls: list[Any] = []
    monkeypatch.setattr(jieba, "setLogLevel", calls.append)

    summarize.segment("刷怪上限")

    assert calls == []


def test_warmup_segmentation_loads_the_dictionary() -> None:
    """启动预热把 jieba 词典加载掉，并返回可写进日志的描述（首次摘要查询不再阻塞）。"""
    description = summarize.warmup_segmentation()

    assert description.startswith("中文分词：jieba 词典已加载")
    assert "0 个词" not in description


def test_lifespan_logs_segmentation_warmup(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """jieba 词典预热与 token 口径一样出现在启动日志里。"""
    with (
        caplog.at_level(logging.INFO, logger="mc_librarian"),
        TestClient(create_app(settings)),
    ):
        pass

    assert "中文分词：jieba 词典已加载" in caplog.text


def test_content_hash_changes_when_public_method_changes() -> None:
    """内容哈希覆盖 FQN + 父类 + 接口 + 字段签名 + public 方法签名（规格 §4）。"""
    base = summarize.compute_content_hash("A", None, (), ("int x",), ("public void tick()",))

    assert base == summarize.compute_content_hash(
        "A", None, (), ("int x",), ("public void tick()",)
    )
    # 任一参与项变化，哈希都变。
    assert base != summarize.compute_content_hash(
        "A", None, (), ("int x",), ("public void tick(int)",)
    )
    assert base != summarize.compute_content_hash(
        "A", "B", (), ("int x",), ("public void tick()",)
    )
    assert base != summarize.compute_content_hash(
        "A", None, ("I",), ("int x",), ("public void tick()",)
    )
    assert base != summarize.compute_content_hash(
        "A", None, (), ("long x",), ("public void tick()",)
    )


# ----------------------------------------------------------------------
# 幂等（按类内容哈希）
# ----------------------------------------------------------------------


def test_unchanged_classes_are_not_regenerated(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """类内容未变时重跑不重复调用 LLM（验收，规格 §4）。"""
    first = run_summarize(settings, indexed)
    calls_after_first = len(fake_llm.calls)

    assert first.generated == 6
    assert calls_after_first >= 1

    second = run_summarize(settings, indexed)

    assert second.generated == 0
    assert second.reused == 6
    assert len(fake_llm.calls) == calls_after_first, "重跑不该再调 LLM"


def test_reparse_keeps_unchanged_summaries(
    admin_client: TestClient, settings: Settings, fake_llm: Any, source_dir: Path
) -> None:
    """重提任务会重建索引（`parse` 原子替换整库）：已有摘要被搬过去，只重生成变化的那一个类。

    这是「类内容未变化时重跑不重复生成」在真实任务路径上的落点（验收，规格 §4）：
    不搬的话每次重提都会把摘要表连库一起换掉，内容哈希幂等形同虚设。
    """
    first = submit(admin_client, source_dir)

    assert wait_for_terminal(admin_client, first.json()["id"])["status"] == "succeeded"

    calls_after_first = len(fake_llm.calls)

    mob = source_dir / "net" / "minecraft" / "world" / "Mob.java"
    mob.write_text(
        mob.read_text(encoding="utf-8").replace(
            "public String getName()", "public String getName(int seed)"
        ),
        encoding="utf-8",
    )

    second = submit(admin_client, source_dir)

    assert wait_for_terminal(admin_client, second.json()["id"])["status"] == "succeeded"

    requested = [fqn for call in fake_llm.calls[calls_after_first:] for fqn in call]

    assert requested == ["net.minecraft.world.Mob"], "只有变化的类该重新生成"

    summaries = read_summaries(index_path(settings.data_dir, VERSION))

    assert len(summaries) == 6


def test_export_restore_remaps_class_ids_and_drops_removed_classes(
    settings: Settings, indexed: Path
) -> None:
    """搬回摘要时 `class_id` 按 fqn 重映射；类已不存在的摘要丢弃（不指向错误类）。"""
    summarize.write_summaries(
        indexed,
        [
            SummaryRow(
                class_id=_class_id(indexed, "net.minecraft.world.Mob"),
                fqn="net.minecraft.world.Mob",
                text_zh="刷怪上限：x\n机制：y\n关键成员：z",
                content_hash="h",
            )
        ],
    )

    carried = summarize.export_summaries(indexed)

    assert [item.fqn for item in carried] == ["net.minecraft.world.Mob"]

    # 重建索引（模拟 `parse`）：整库被替换，摘要表一并消失。
    build_index(source_root(settings.data_dir, VERSION), indexed, log=silent)

    assert summarize.export_summaries(indexed) == []

    restored = summarize.restore_summaries(indexed, carried)

    assert restored == 1

    connection = sqlite3.connect(indexed)

    try:
        row = connection.execute(
            "SELECT class_id, content_hash FROM summaries WHERE fqn = 'net.minecraft.world.Mob'"
        ).fetchone()
        class_id = connection.execute(
            "SELECT id FROM classes WHERE fqn = 'net.minecraft.world.Mob'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert row is not None
    assert row[0] == class_id, "class_id 必须重映射到新索引里的类"
    assert row[1] == "h", "搬回时保留原 content_hash，重跑才不会重复生成"

    # 类已不存在：该摘要丢弃。
    gone = [
        summarize.StoredSummary(
            fqn="gone.Class", text_zh="x", content_hash="h", tokens=0, created_at="t"
        )
    ]

    assert summarize.restore_summaries(indexed, gone) == 0


# ----------------------------------------------------------------------
# 分批与失败处理
# ----------------------------------------------------------------------


def test_build_prompt_states_the_three_line_format() -> None:
    """格式要求与样板写在**用户消息**里（issue #23：只写在 system 消息里模型会忽略）。"""
    prompt = summarize.build_prompt([digest("net.minecraft.world.Mob", "net.minecraft.world")])

    for prefix in ("职责：", "机制：", "关键成员："):
        assert prefix in prompt, prefix

    assert "只许英文标识符" in prompt
    assert "### net.minecraft.world.Mob" in prompt


def test_group_by_package_splits_batches_within_a_package() -> None:
    """按 package 分批：同包按 `batch_size` 切，不同包不混批（规格 §3）。"""
    batches = summarize.group_by_package(
        [
            digest("a.A", "a"),
            digest("a.B", "a"),
            digest("a.C", "a"),
            digest("b.D", "b"),
        ],
        batch_size=2,
    )

    assert [[item.fqn for item in batch] for batch in batches] == [
        ["a.A", "a.B"],
        ["a.C"],
        ["b.D"],
    ]


def test_invalid_json_marks_every_class_in_the_batch_failed(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """整批响应不是合法 JSON：该批每个类都算单类失败（规格 §3）。"""
    fake_llm.raw_override = "这不是 JSON"

    stats = run_summarize(settings, indexed)

    assert stats.generated == 0
    assert stats.failed == 6
    assert read_summaries(indexed) == {}


def test_fenced_json_response_is_parsed() -> None:
    """```json 围栏（真实模型的常态）在解析前被剥掉，不是「响应不是合法 JSON」。"""
    body = (
        '{"net.minecraft.world.Mob": "职责：控制刷怪上限\\n'
        '机制：按 mob cap 调度\\n关键成员：spawnInterval"}'
    )
    digests = [digest("net.minecraft.world.Mob", "net.minecraft.world")]

    for raw in (f"```json\n{body}\n```", f"```\n{body}\n```", body):
        parsed, failures = summarize.parse_batch_response(raw, digests)

        assert failures == {}, raw
        assert "net.minecraft.world.Mob" in parsed, raw


def test_prose_around_the_json_object_is_tolerated() -> None:
    """JSON 前后的说明文字（「以下是摘要：」/「希望有帮助」）不影响解析。"""
    body = (
        '{"net.minecraft.world.Mob": "职责：控制刷怪上限\\n'
        '机制：按 mob cap 调度\\n关键成员：spawnInterval"}'
    )
    digests = [digest("net.minecraft.world.Mob", "net.minecraft.world")]

    for raw in (f"以下是摘要：\n{body}", f"{body}\n希望有帮助！", f"好的：\n```json\n{body}\n```"):
        parsed, failures = summarize.parse_batch_response(raw, digests)

        assert failures == {}, raw
        assert "net.minecraft.world.Mob" in parsed, raw


def test_single_class_failure_keeps_the_rest(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """单类失败只丢那一个类，同批其余照常入库（规格 §5）。"""
    fake_llm.failures = {"net.minecraft.world.Mob"}

    stats = run_summarize(settings, indexed)

    assert stats.failed == 1
    assert stats.generated == 5

    summaries = read_summaries(indexed)

    assert "net.minecraft.world.Mob" not in summaries
    assert len(summaries) == 5


def test_stage_failure_keeps_generated_summaries_and_marks_job_failed(
    admin_client: TestClient, settings: Settings, fake_llm: Any, source_dir: Path
) -> None:
    """一个类失败 → 任务 `failed`，但已生成的摘要保留（验收，规格 §5）。"""
    fake_llm.failures = {"net.minecraft.world.Mob"}

    created = submit(admin_client, source_dir)

    assert created.status_code == 202, created.text

    job = wait_for_terminal(admin_client, created.json()["id"])

    assert job["status"] == "failed"
    assert job["error"] is not None and "摘要失败 1 个类" in job["error"]

    summaries = read_summaries(index_path(settings.data_dir, VERSION))

    assert len(summaries) == 5, "失败类之外的摘要必须保留"
    assert "net.minecraft.world.Mob" not in summaries

    logs = admin_client.get(f"/v1/admin/jobs/{job['id']}/logs").json()["lines"]

    assert any("摘要：生成 5、复用 0、失败 1" in line["message"] for line in logs)


def test_retry_after_failure_only_regenerates_the_failed_class(
    admin_client: TestClient, settings: Settings, fake_llm: Any, source_dir: Path
) -> None:
    """重试只重跑失败的类（靠哈希机制自然实现，验收第 1 条）。"""
    fake_llm.failures = {"net.minecraft.world.Mob"}

    created = submit(admin_client, source_dir)
    job = wait_for_terminal(admin_client, created.json()["id"])

    assert job["status"] == "failed"

    fake_llm.failures = set()
    calls_before = len(fake_llm.calls)

    assert admin_client.post(f"/v1/admin/jobs/{job['id']}/retry").status_code == 202
    assert wait_for_terminal(admin_client, job["id"])["status"] == "succeeded"

    requested = [fqn for call in fake_llm.calls[calls_before:] for fqn in call]

    assert requested == ["net.minecraft.world.Mob"], "只该重跑失败的那一个类"


# ----------------------------------------------------------------------
# LLM 配置降级
# ----------------------------------------------------------------------


def test_unconfigured_llm_fails_the_stage_explicitly(
    tmp_path: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MC_LIBRARIAN_LLM_*` 未配置 → `summarize` 阶段明确失败，不是静默跳过（验收，规格 §2）。"""
    for name in (LLM_BASE_URL_ENV, LLM_MODEL_ENV, LLM_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with TestClient(create_app(settings)) as client:
        setup_admin(client)
        created = submit(client, source_dir)

        assert created.status_code == 202, created.text

        job = wait_for_terminal(client, created.json()["id"])

    assert job["status"] == "failed"
    assert job["error"] is not None
    assert "未配置 LLM" in job["error"]
    assert LLM_BASE_URL_ENV in job["error"], "报错要指出缺哪个环境变量"

    coverage = summarize.read_summary_coverage(index_path(settings.data_dir, VERSION))

    assert coverage is not None
    assert coverage.summaries == 0, "明确失败，而不是留下一个「看起来有摘要」的空索引"


def test_resolve_llm_config_rejects_partial_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只配了一半也算未配置：报错列出缺的那几项。"""
    monkeypatch.delenv(LLM_MODEL_ENV, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV, raising=False)

    settings = Settings(data_dir=tmp_path / "data", llm_base_url="http://x/v1")

    with pytest.raises(LlmConfigError, match=LLM_MODEL_ENV):
        resolve_llm_config(settings)


def test_fully_reused_index_needs_no_llm_config(
    settings: Settings, fake_llm: Any, indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量复用（类未变）时不要求 LLM 配置：重跑幂等不该因为没配密钥而失败。"""
    run_summarize(settings, indexed)

    for name in (LLM_BASE_URL_ENV, LLM_MODEL_ENV, LLM_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)

    unconfigured = Settings(data_dir=settings.data_dir)
    stats = run_summarize(unconfigured, indexed)

    assert stats.generated == 0
    assert stats.reused == 6


# ----------------------------------------------------------------------
# 覆盖率（规格 §8）
# ----------------------------------------------------------------------


def test_coverage_counts_summaries_over_classes(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """覆盖率 = 已生成 / 类总数，`/v1/versions` 与任务日志都用它。"""
    before = summarize.read_summary_coverage(indexed)

    assert before is not None
    assert before.summaries == 0
    assert before.classes == 6

    run_summarize(settings, indexed)

    after = summarize.read_summary_coverage(indexed)

    assert after is not None
    assert (after.summaries, after.classes) == (6, 6)


def test_versions_endpoint_reports_coverage(
    admin_client: TestClient, settings: Settings, fake_llm: Any, source_dir: Path
) -> None:
    """版本页/任务页要显示的覆盖率（规格 §8）：`/v1/versions` 实时给出已生成 / 类总数。"""
    created = submit(admin_client, source_dir)

    assert wait_for_terminal(admin_client, created.json()["id"])["status"] == "succeeded"

    versions = admin_client.get("/v1/versions").json()["versions"]
    registered = next(item for item in versions if item["version"] == VERSION)

    assert registered["has_summaries"] is True
    assert registered["summary_count"] == 6
    assert registered["class_count"] == 6


def test_write_summaries_is_idempotent_per_fqn(settings: Settings, indexed: Path) -> None:
    """同一个 fqn 再写一次是覆盖而不是插入第二行（`summaries.fqn` UNIQUE）。"""
    row = SummaryRow(
        class_id=1,
        fqn="net.minecraft.world.Mob",
        text_zh="刷怪上限：第一次\n机制：x\n关键成员：y",
        content_hash="h1",
    )

    summarize.write_summaries(indexed, [row])
    summarize.write_summaries(
        indexed,
        [
            SummaryRow(
                class_id=1,
                fqn="net.minecraft.world.Mob",
                text_zh="刷怪上限：第二次\n机制：x\n关键成员：y",
                content_hash="h2",
            )
        ],
    )

    summaries = read_summaries(indexed)

    assert list(summaries) == ["net.minecraft.world.Mob"]
    assert summaries["net.minecraft.world.Mob"].endswith("第二次\n机制：x\n关键成员：y")
    assert len(read_fts_rows(indexed)) == 1, "FTS 行也只能留一行（旧行必须被删掉）"


def test_insert_summary_deletes_the_fts_row_by_rowid(settings: Settings, indexed: Path) -> None:
    """FTS 旧行按 `rowid` 精确定位：`WHERE fqn = ?` 在 FTS5 上是全表扫，5,000 个类时是 O(n²)。

    `set_trace_callback` 拿到的就是实际执行的 SQL（参数已展开），所以这是对「按什么匹配」
    的直接断言，而不是间接的耗时猜测。
    """
    # 先建表（`write_summaries` 的真实路径），再把 trace 回调挂上：只关心 FTS 同步那几句。
    summarize.write_summaries(indexed, [])

    connection = sqlite3.connect(indexed)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    try:
        summarize._insert_summary(
            connection,
            class_id=_class_id(indexed, "net.minecraft.world.Mob"),
            fqn="net.minecraft.world.Mob",
            text_zh="刷怪上限：每区块生成预算\n机制：按 mob cap 调度\n关键成员：spawnInterval",
            content_hash="h",
            tokens=1,
            created_at="t",
        )
        connection.commit()
        rows = connection.execute(
            "SELECT s.id AS id, f.rowid AS rowid FROM summaries s"
            " JOIN summaries_fts f ON f.fqn = s.fqn"
        ).fetchall()
    finally:
        connection.close()

    fts_statements = [sql for sql in statements if "summaries_fts" in sql]
    deletes = [sql for sql in fts_statements if sql.startswith("DELETE")]
    inserts = [sql for sql in fts_statements if sql.startswith("INSERT")]

    assert len(deletes) == 1, fts_statements
    assert "rowid" in deletes[0] and "fqn" not in deletes[0], deletes[0]
    assert len(inserts) == 1 and "rowid" in inserts[0], inserts
    # `summaries.id` 与 FTS 行的 `rowid` 对齐：按 rowid 删才删得准。
    assert len(rows) == 1
    assert rows[0][0] == rows[0][1]


def test_misaligned_fts_is_rebuilt_from_summaries_before_writing(
    settings: Settings, fake_llm: Any, indexed: Path
) -> None:
    """旧库的 FTS rowid 与 `summaries.id` 错位时，写摘要前按 `summaries` 整表重建（验收）。

    修复前的库：FTS 行的 rowid 由 FTS5 计数器自增、删除按 fqn，某个类被重写过就错位。此时
    按 rowid 删旧行会删掉**另一个类**的 FTS 行，那个类从摘要通道静默消失（`summaries` 行还在）。
    """
    digests = {item.fqn: item for item in summarize.read_class_digests(indexed)}
    stale_fqn = "net.minecraft.world.Container"  # 它的 FTS 行被挪去撞下一个 summaries.id
    pending_fqn = "net.minecraft.world.World"

    # 先写出「除了 pending 那个类之外」的摘要（content_hash 与索引一致，重跑时命中复用）。
    summarize.write_summaries(
        indexed,
        [
            SummaryRow(
                class_id=item.class_id,
                fqn=item.fqn,
                text_zh=(
                    f"刷怪与区块生成：{item.fqn.rsplit('.', 1)[-1]} 负责生成与掉落\n"
                    "机制：按每刻预算与区块范围调度\n"
                    f"关键成员：{item.fqn.rsplit('.', 1)[-1]}"
                ),
                content_hash=item.content_hash,
            )
            for item in digests.values()
            if item.fqn != pending_fqn
        ],
    )

    # 模拟修复前的库：Container 被重写过，FTS 行的 rowid 跑到下一个 summaries.id（= World 的 id）。
    connection = sqlite3.connect(indexed)
    try:
        next_id = int(connection.execute("SELECT MAX(id) + 1 FROM summaries").fetchone()[0])
        connection.execute("DELETE FROM summaries_fts WHERE fqn = ?", (stale_fqn,))
        connection.execute(
            "INSERT INTO summaries_fts (rowid, fqn, text_zh) VALUES (?, ?, ?)",
            (next_id, stale_fqn, summarize.segment("错位的旧分词")),
        )
        connection.commit()
        misaligned = connection.execute(
            "SELECT COUNT(*) FROM summaries s JOIN summaries_fts f ON f.fqn = s.fqn"
            " WHERE f.rowid <> s.id"
        ).fetchone()[0]
    finally:
        connection.close()

    assert misaligned == 1, "构造的旧库应当错位"

    stats = run_summarize(settings, indexed)

    assert stats.generated == 1, stats.failures  # 只有缺摘要的 World 重新生成

    connection = sqlite3.connect(indexed)
    connection.row_factory = sqlite3.Row

    try:
        aligned, total = connection.execute(
            "SELECT (SELECT COUNT(*) FROM summaries s JOIN summaries_fts f"
            "         ON f.fqn = s.fqn AND f.rowid = s.id),"
            " (SELECT COUNT(*) FROM summaries)"
        ).fetchone()
        fts_fqns = {str(row[0]) for row in connection.execute("SELECT fqn FROM summaries_fts")}
        summary_fqns = {str(row[0]) for row in connection.execute("SELECT fqn FROM summaries")}

        # (a) 重建：每条摘要的 FTS 行都钉回自己的 id。
        assert (int(aligned), int(total)) == (6, 6)
        # (c) 没有类的 FTS 行被误删（两边行集一致，也没有孤儿行）。
        assert fts_fqns == summary_fqns == set(digests)

        # (b) 每个类的摘要都能被摘要通道命中。
        for fqn in sorted(summary_fqns):
            hits = channels.summary_candidates(
                connection, fqn.rsplit(".", 1)[-1], path_prefix=None
            )
            assert any(hit.symbol == fqn for hit in hits), f"{fqn} 命不中摘要通道"
    finally:
        connection.close()
