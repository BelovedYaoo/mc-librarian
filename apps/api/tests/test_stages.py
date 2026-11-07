"""阶段序列只能有一份：`stages.STAGES`，接口枚举与数据库 CHECK 都必须与它一致。

`#19` 之前同一份顺序写了三遍（runner 常量、v4 建表 CHECK、v5 重建表 CHECK），
改一处忘另一处的后果是「数据库拒收某个合法阶段」——只在真跑到那一段时才炸。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import get_args

from mc_librarian.db import Database
from mc_librarian.jobs import STAGES as EXPORTED_STAGES
from mc_librarian.schemas import JobStage
from mc_librarian.stages import FINAL_STAGE, SOURCE_STAGES, STAGES, sql_stage_check


def test_stages_are_exported_from_one_place() -> None:
    assert EXPORTED_STAGES is STAGES
    assert STAGES[2:] == SOURCE_STAGES
    assert STAGES[-1] == FINAL_STAGE


def test_interface_enum_matches_the_constant() -> None:
    """`JobStage`（OpenAPI 枚举）与 `STAGES` 同序同值——前端标签的穷尽性检查靠它。"""
    assert get_args(JobStage) == STAGES


def test_schema_check_constraint_accepts_every_stage() -> None:
    """数据库 CHECK 必须接受 `STAGES` 里的每一个阶段（含 download / decompile）。"""
    with tempfile.TemporaryDirectory() as tmp:
        database = Database.for_data_dir(Path(tmp))
        database.initialize()

        with database.connect() as conn:
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()[0]

            assert all(f"'{stage}'" in schema for stage in STAGES), schema

            conn.execute(
                "INSERT INTO jobs (id, kind, version, status, created_at)"
                " VALUES ('j', 'index', '26.3', 'queued', '2026-01-01T00:00:00Z')"
            )

            for stage in STAGES:
                conn.execute("UPDATE jobs SET stage = ? WHERE id = 'j'", (stage,))

            with conn:
                pass

            # 域外的值仍要拒收（不是为了「能写」把约束放宽成摆设）
            try:
                conn.execute("UPDATE jobs SET stage = 'bogus' WHERE id = 'j'")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("CHECK 约束必须拒绝域外阶段")


def test_sql_check_lists_all_stages() -> None:
    sql = sql_stage_check()

    assert sql.startswith("stage IS NULL OR stage IN (")
    assert all(f"'{stage}'" in sql for stage in STAGES)
