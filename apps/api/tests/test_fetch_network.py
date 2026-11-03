"""真实链路：联网下载官方 jar、跑真 remap 与真反编译。

默认跳过（见 `conftest.py` 的 `pytest_collection_modifyitems`）；用 `--run-network`
或 `MC_LIBRARIAN_NETWORK=1` 启用，并需要：

- `MC_LIBRARIAN_VINEFLOWER_JAR` —— Vineflower jar；
- `MC_LIBRARIAN_SPECIALSOURCE_JAR` —— SpecialSource shaded jar（≤ 1.21.11 的版本才需要）。

预期文件数取自 `docs/00-overview.md` §6 的实测基线，容差 ±5%。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mc_librarian.config import Settings
from mc_librarian.fetch.pipeline import run_fetch
from mc_librarian.fetch.tools import PipelineTools, default_tools

pytestmark = pytest.mark.network

EXPECTED_JAVA_FILES = {
    "26.3": 7301,
    "1.20.1": 4786,
}

# remap 后的树上应该能按包路径找到这些类（#14 §2 的正面判据）。
KNOWN_READABLE_CLASSES = (
    "net.minecraft.world.entity.Mob",
    "net.minecraft.world.level.Level",
    "net.minecraft.world.item.Item",
)
# 混淆树的形态：`a.java`、`ab.java`（#14 §2 的反面判据）。
OBFUSCATED_PATH = re.compile(r"^[a-z]{1,2}\.java$")


def _tools(settings: Settings, version: str) -> PipelineTools:
    if settings.vineflower_jar is None:
        pytest.skip("未配置 MC_LIBRARIAN_VINEFLOWER_JAR")

    if version != "26.3" and settings.specialsource_jar is None:
        pytest.skip("未配置 MC_LIBRARIAN_SPECIALSOURCE_JAR")

    return default_tools(settings)


@pytest.mark.parametrize(("version", "expected"), sorted(EXPECTED_JAVA_FILES.items()))
def test_fetch_produces_java_tree(
    settings: Settings, tmp_path: Path, version: str, expected: int
) -> None:
    tools = _tools(settings, version)
    source = tmp_path / "src" / version
    cache = tmp_path / "cache" / version

    result = run_fetch(version, source, cache, tools, lambda level, message: None)

    low, high = int(expected * 0.95), int(expected * 1.05)
    assert low <= result.java_files <= high

    files = [path for path in source.rglob("*") if path.is_file()]
    assert files
    # 只产出 `.java`：不含 `assets/`、`data/`、`META-INF/`（空目录也会被清掉）。
    assert all(path.suffix == ".java" for path in files)
    assert "assets" not in {path.relative_to(source).parts[0] for path in files}
    assert "data" not in {path.relative_to(source).parts[0] for path in files}
    assert not (source / "assets").exists()
    assert not (source / "data").exists()

    if version == "26.3":
        # Mojang 已不混淆：无 mappings，直接反编译。
        assert result.checkpoint["obfuscated"] is False
        assert result.checkpoint["mappings_path"] is None
    else:
        # 走 mappings 路径：remap 后类名是可读英文（`net/minecraft/...`）。
        assert result.checkpoint["obfuscated"] is True
        assert result.checkpoint["mappings_path"]
        assert (source / "net" / "minecraft").is_dir()
        # #14 §2：可读类名在（正面）、没有 `a.java` / `ab.java` 这种文件（反面）。
        # `files.path` 就是这棵树的相对路径，所以两个判据在同一份事实上。
        for fqn in KNOWN_READABLE_CLASSES:
            assert (source / f"{fqn.replace('.', '/')}.java").is_file()

        obfuscated_paths = [
            path.relative_to(source).as_posix()
            for path in source.rglob("*.java")
            if OBFUSCATED_PATH.match(path.relative_to(source).as_posix())
        ]

        assert obfuscated_paths == []
