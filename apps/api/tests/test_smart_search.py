"""智能检索（`mode=smart`）：召回并集 + LLM 列表精排，失败必须退回字面。

这一组守的三条不变量，任一条破了都会让「开关」变成风险：

1. 没配 LLM / 调用失败 → 结果与 `mode=lexical` **完全一致**（不劣化）；
2. 精排只从**真实候选**里挑，模型给的编号非法/超界时忽略，不凭空产生命中；
3. 扩展词按查询缓存：同一个问题第二次不再调 LLM。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.retrieval import smart
from mc_librarian.retrieval import smart as smart_module
from mc_librarian.retrieval.terms import identifiers
from test_search import VERSION, post_search, register


@pytest.fixture(autouse=True)
def clear_expansion_cache() -> None:
    """每个用例都从空缓存开始：否则上一个用例的扩展词会串进来。"""
    smart.clear_cache()


def _fake_llm(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """按顺序返回给定答案，并记录每次 prompt（判断是否命中缓存）。"""
    prompts: list[str] = []

    def fake_post(
        _url: str, payload: dict[str, Any], _api_key: str, _timeout: float, **_kw: Any
    ) -> str:
        prompts.append(str(payload["messages"][0]["content"]))

        return answers[min(len(prompts) - 1, len(answers) - 1)]

    monkeypatch.setattr(smart_module, "post_chat_completion", fake_post)

    return prompts


def test_smart_falls_back_to_lexical_without_llm(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """没配 LLM：`mode=smart` 与 `mode=lexical` 的结果必须一模一样（不劣化）。"""
    register(settings, conn)
    body = {"query": "谁调用了 Mob 的 tick", "detail": "locate", "limit": 5}

    lexical = post_search(user_client, {**body, "mode": "lexical"})
    smart_body = post_search(user_client, {**body, "mode": "smart"})

    assert smart_body["hits"] == lexical["hits"]
    assert smart_body.get("expanded_query") is None


def test_smart_reranks_real_candidates(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """精排能把候选里原本靠后的那个提前（模型只给编号，命中仍是索引里的真实实体）。"""
    register(settings, conn)
    body = {"query": "谁调用了 Mob 的 tick", "detail": "locate", "limit": 5}
    lexical = post_search(user_client, {**body, "mode": "lexical"})

    if len(lexical["hits"]) < 3:
        pytest.skip("夹具索引的召回不足 3 条，无法验证重排")

    # 扩展词随便给一个非空串（只要触发两段流程）；精排把第 3 名提到第 1。
    _fake_llm(monkeypatch, ["刷怪 上限 mob cap NaturalSpawner", "3,1,2,4,5"])

    smart_body = post_search(user_client, {**body, "mode": "smart"})

    assert smart_body["expanded_query"] == "刷怪 上限 mob cap NaturalSpawner"
    assert smart_body["hits"], "重排后不能丢掉命中的第一条"
    # 第 3 名被提到第 1：用 id 断言，避免依赖 score 的具体数值。
    assert smart_body["hits"][0]["id"] == lexical["hits"][2]["id"]


def test_smart_ignores_out_of_range_indices(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型给越界/非法编号时只保留合法的，且不因此报错或产生幻觉命中。"""
    register(settings, conn)
    body = {"query": "谁调用了 Mob 的 tick", "detail": "locate", "limit": 5}

    _fake_llm(monkeypatch, ["刷怪 上限", "99,0,abc,2"])

    smart_body = post_search(user_client, {**body, "mode": "smart"})

    assert smart_body["hits"], "只有一个合法编号（2）时也应该保留该条"


def test_expansion_is_cached_per_query(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个问题问两次：扩展只调一次 LLM（生产里重复提问很常见）。"""
    register(settings, conn)
    body = {"query": "谁调用了 Mob 的 tick", "detail": "locate", "limit": 3}
    prompts = _fake_llm(monkeypatch, ["刷怪 上限 mob cap", "1,2,3"])

    post_search(user_client, {**body, "mode": "smart"})
    after_first = len([p for p in prompts if "改写成检索关键词" in p])

    post_search(user_client, {**body, "mode": "smart"})
    after_second = len([p for p in prompts if "改写成检索关键词" in p])

    assert after_first == 1
    assert after_second == 1, "第二次不应再调扩展"


def test_identifiers_still_work_in_smart_mode(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """`smart` 不改变查询分流：带类名的中文提问仍然走符号/引用通道。"""
    register(settings, conn)
    body = {**{"query": "谁调用了 Mob 的 tick"}, "detail": "locate", "limit": 5, "mode": "smart"}

    result = post_search(user_client, body)

    assert identifiers("谁调用了 Mob 的 tick") == ("Mob", "tick")
    assert any(
        (hit.get("symbol") or "").startswith("net.minecraft.world.Mob") for hit in result["hits"]
    ), result["hits"]


def test_smart_mode_is_accepted_by_the_contract(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """`mode` 是契约的一部分：非法取值应当 422（不是静默忽略）。"""
    register(settings, conn)

    response = user_client.post(
        "/v1/search",
        json={"version": VERSION, "query": "刷怪", "mode": "super-smart"},
    )

    assert response.status_code == 422, response.text
