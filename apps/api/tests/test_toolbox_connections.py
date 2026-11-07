"""`ToolBox` 的连接所有权：每次工具调用开一个、用完立刻关。

`#27` 的教训是「共享连接 + 跨线程」会静默读错行；`#28` 的结论是**不池化、不线程本地**——
每次调用开一个（实测开+关 0.17ms），没有生命周期状态、不可能泄漏。
这一组守住两条：连接确实被关；并发调用不互相干扰。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from mc_librarian.answer.tools import ToolBox
from mc_librarian.config import Settings
from mc_librarian.indexing import query as index_query
from mc_librarian.indexing.layout import index_path
from test_search import VERSION, register


def _toolbox(settings: Settings, tmp_index: Path) -> ToolBox:
    return ToolBox(tmp_index, settings, VERSION)


def test_each_tool_call_closes_its_connection(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次工具调用结束后，它开过的连接必须已经关闭（否则就是泄漏）。"""
    register(settings, conn)
    opened: list[sqlite3.Connection] = []
    original = index_query.open_readonly

    def spy(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(*args, **kwargs)
        opened.append(connection)

        return connection

    monkeypatch.setattr("mc_librarian.answer.tools.query.open_readonly", spy)

    outcome = _toolbox(settings, index_path(settings.data_dir, VERSION)).execute(
        "symbol", {"name": "Mob"}
    )

    assert outcome.ok, outcome.text
    assert opened, "工具应当开过连接"

    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")  # 已关闭的连接不能再操作


def test_concurrent_tool_calls_use_separate_connections(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8 线程同时跑工具：不得报错，且每个连接都被关上（不能只关最后一个）。"""
    register(settings, conn)
    opened: list[sqlite3.Connection] = []
    lock = threading.Lock()
    original = index_query.open_readonly

    def spy(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(*args, **kwargs)
        with lock:
            opened.append(connection)

        return connection

    monkeypatch.setattr("mc_librarian.answer.tools.query.open_readonly", spy)
    toolbox = _toolbox(settings, index_path(settings.data_dir, VERSION))
    errors: list[str] = []

    def worker(worker_id: int) -> None:
        for _ in range(3):
            try:
                outcome = toolbox.execute("symbol", {"name": "Mob"})
                if not outcome.ok:
                    errors.append(outcome.text)
            except Exception as error:  # 并发下不应有任何异常冒出来
                errors.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors[:3]
    assert len(opened) == 24, f"每次调用一个连接，期望 24 个，实际 {len(opened)}"

    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
