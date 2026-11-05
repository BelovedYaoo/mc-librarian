"""多来源索引与补丁 hunk 切块（issue #24 规格 §2/§3）。

补丁解析、`source` 列、`patches`/`patches_fts` 的写入都走真实的 `build_index_from_sources`，
不 mock；NeoForge 的 `+_,N` 新行号重建也在这里钉住。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mc_librarian.indexing import patches, query
from mc_librarian.indexing.build import (
    SourceSpec,
    build_index,
    build_index_from_sources,
)

OK_JAVA = "package net.{package};\n\npublic class {name} {{\n    public void tick() {{}}\n}}\n"

# NeoForge 风格的补丁：新侧起始行是 `_`（未记录），新侧行数正常给。
NEOFORGE_PATCH = """\
--- a/net/minecraft/world/Mob.java
+++ b/net/minecraft/world/Mob.java
@@ -10,3 +_,6 @@
     public void tick() {
+        this.checkDespawn();
+        this.customServerAiStep();
     }
@@ -30,2 +_,5 @@
     private int experienceReward() {
-        return 1;
+        return this.getExperienceReward();
+    }
"""


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    return root


def silent(_level: str, _message: str) -> None:
    pass


# ----------------------------------------------------------------------
# 补丁解析
# ----------------------------------------------------------------------


def test_parse_patch_file_splits_hunks_and_reconstructs_new_start() -> None:
    """一个 patch 文件切成多条 hunk；`+_,N` 的新起始行按前序 hunk 的行数差重建。"""
    hunks = patches.parse_patch_file(NEOFORGE_PATCH, path="net/minecraft/world/Mob.java.patch")

    assert len(hunks) == 2
    assert [hunk.hunk_index for hunk in hunks] == [0, 1]
    assert all(hunk.target == "net/minecraft/world/Mob.java" for hunk in hunks)

    # 第 1 条：无前序偏移，新起始行 = old_start；第 2 条：+（6-3）= +3。
    assert (hunks[0].old_start, hunks[0].old_lines) == (10, 3)
    assert (hunks[0].new_start, hunks[0].new_lines) == (10, 6)
    assert (hunks[1].old_start, hunks[1].new_start) == (30, 33)
    assert hunks[0].context.startswith("@@ -10,3 +_,6 @@")
    assert hunks[0].tokens > 0


def test_parse_patch_file_uses_explicit_new_start_when_present() -> None:
    """标准 unified diff 给了新起始行时以头部为准，不做重建。"""
    text = "--- a/A.java\n+++ b/A.java\n@@ -1,2 +5,3 @@\n a\n+b\n c\n"

    hunks = patches.parse_patch_file(text, path="A.java.patch")

    assert len(hunks) == 1
    assert hunks[0].new_start == 5


def test_parse_patch_file_without_hunks_is_empty() -> None:
    assert patches.parse_patch_file("--- a/A.java\n+++ b/A.java\n", path="A.java.patch") == []


# ----------------------------------------------------------------------
# 多来源构建
# ----------------------------------------------------------------------


def test_single_source_index_marks_every_row_patched(tmp_path: Path) -> None:
    """单目录入口（既有语义）：`files`/`classes` 的 `source` 都是 `patched`。"""
    root = write_tree(
        tmp_path / "src",
        {"net/minecraft/Mob.java": OK_JAVA.format(package="minecraft", name="Mob")},
    )
    db = tmp_path / "db" / "26.3.sqlite"

    build_index(root, db, log=silent)

    connection = query.open_readonly(db)

    try:
        sources = {row["source"] for row in connection.execute("SELECT source FROM files")}
        class_sources = {row["source"] for row in connection.execute("SELECT source FROM classes")}
    finally:
        connection.close()

    assert sources == {"patched"}
    assert class_sources == {"patched"}


def test_multi_source_index_separates_patched_loader_and_patches(tmp_path: Path) -> None:
    """三类来源各归其位：Java 行带 `source`，补丁进 `patches`/`patches_fts`，不进 `files`。"""
    patched = write_tree(
        tmp_path / "patched",
        {"net/minecraft/Mob.java": OK_JAVA.format(package="minecraft", name="Mob")},
    )
    loader = write_tree(
        tmp_path / "loader",
        {"net/neoforged/Fml.java": OK_JAVA.format(package="neoforged", name="Fml")},
    )
    patch_dir = write_tree(
        tmp_path / "patches", {"net/minecraft/Mob.java.patch": NEOFORGE_PATCH}
    )
    db = tmp_path / "db" / "neoforge-21.1.265+1.21.1.sqlite"

    result = build_index_from_sources(
        [
            SourceSpec(kind="patched", root=patched),
            SourceSpec(kind="loader", root=loader),
            SourceSpec(kind="patch", root=patch_dir),
        ],
        db,
        log=silent,
    )

    assert result.files == 2
    assert result.patch_files == 1
    assert result.patch_hunks == 2

    connection = query.open_readonly(db)

    try:
        by_source = {
            row["source"]: row["total"]
            for row in connection.execute(
                "SELECT source, COUNT(*) AS total FROM classes GROUP BY source"
            )
        }
        paths = {row["path"] for row in connection.execute("SELECT path FROM files")}
        patch_rows = connection.execute(
            "SELECT path, target, new_start, new_lines FROM patches ORDER BY hunk_index"
        ).fetchall()
        fts = connection.execute(
            "SELECT COUNT(*) AS total FROM patches_fts"
        ).fetchone()
        target_index = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_patches_target'"
        ).fetchone()
    finally:
        connection.close()

    assert by_source == {"patched": 1, "loader": 1}
    assert paths == {"net/minecraft/Mob.java", "net/neoforged/Fml.java"}
    assert [row["target"] for row in patch_rows] == [
        "net/minecraft/world/Mob.java",
        "net/minecraft/world/Mob.java",
    ]
    assert [row["new_start"] for row in patch_rows] == [10, 33]
    assert fts is not None and int(fts["total"]) == 2
    assert target_index is not None


def test_multi_source_index_matches_patch_fts_queries(tmp_path: Path) -> None:
    """`patches_fts` 预分词后可被标识符查询命中（与 summaries_fts 同口径）。"""
    patch_dir = write_tree(tmp_path / "patches", {"net/minecraft/Mob.java.patch": NEOFORGE_PATCH})
    db = tmp_path / "db" / "v.sqlite"

    build_index_from_sources([SourceSpec(kind="patch", root=patch_dir)], db, log=silent)

    connection = query.open_readonly(db)

    try:
        rows = connection.execute(
            "SELECT p.target FROM patches_fts JOIN patches p ON p.id = patches_fts.rowid"
            " WHERE patches_fts MATCH ?",
            ('"checkDespawn"',),
        ).fetchall()
    finally:
        connection.close()

    assert [row["target"] for row in rows] == ["net/minecraft/world/Mob.java"]


def test_patch_rows_are_not_in_files_table(tmp_path: Path) -> None:
    """补丁文件不进 `files`：`files` 只收 `.java` 与 `data/**/*.json`（规格 §3）。"""
    patch_dir = write_tree(tmp_path / "patches", {"net/minecraft/Mob.java.patch": NEOFORGE_PATCH})
    db = tmp_path / "db" / "v.sqlite"

    result = build_index_from_sources([SourceSpec(kind="patch", root=patch_dir)], db, log=silent)

    assert result.files == 0

    connection = sqlite3.connect(db)

    try:
        files = connection.execute("SELECT COUNT(*) FROM files").fetchone()
    finally:
        connection.close()

    assert files is not None and int(files[0]) == 0
