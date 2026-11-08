"""反编译分块：覆盖完整、互不重叠、大小受控，且每块真的用 `--only` 调一次 Vineflower。

内存约束来自实测：整包 26.3 连 2G 都 OOM，按包切块后 1G 峰值约 1.2 GB。
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from mc_librarian.fetch.chunks import class_names_of, plan_chunks, select_classes
from mc_librarian.fetch.tools import VineflowerDecompiler


def test_plan_chunks_covers_each_class_exactly_once() -> None:
    names = [f"net/minecraft/world/level/C{i}" for i in range(7)]
    names += ["com/mojang/blaze3d/GpuDevice", "net/minecraft/SharedConstants"]

    chunks = plan_chunks(names, max_classes=3)
    covered: list[str] = []

    for prefixes, count in chunks:
        matched = select_classes(names, prefixes)

        assert count == len(matched), f"块大小与覆盖类数不符：{prefixes}"
        assert len(matched) <= 3, f"块超过上限：{prefixes} → {len(matched)}"
        covered.extend(matched)

    assert sorted(covered) == sorted(names), "有类没被任何块覆盖，或被覆盖两次"


def test_plan_splits_oversize_package_one_level_deeper() -> None:
    # 同一个包下 6 个类：depth-4 前缀只有 1 个且超限，必须按类名再拆。
    names = [f"net/minecraft/world/level/C{i}" for i in range(6)]

    chunks = plan_chunks(names, max_classes=2)

    assert len(chunks) == 3
    assert all(count <= 2 for _prefixes, count in chunks)


def test_class_names_of_skips_meta_inf(tmp_path: Path) -> None:
    jar = tmp_path / "client.jar"

    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("net/minecraft/A.class", b"")
        archive.writestr("net/minecraft/A$Inner.class", b"")
        archive.writestr("META-INF/MANIFEST.MF", b"")

    assert sorted(class_names_of(jar)) == ["net/minecraft/A", "net/minecraft/A$Inner"]


def test_decompiler_runs_one_invocation_per_chunk_and_merges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: list[tuple[list[str], list[str]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subset = Path(command[-2])
        out = Path(command[-1])
        out.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(subset) as archive:
            entries = [name[: -len(".class")] for name in archive.namelist()]

        recorded.append((command, entries))
        # 每块产出一个文件，用来验证「逐块合并」。
        target = out / entries[0].replace("/", "_")
        target.mkdir(parents=True, exist_ok=True)
        (target / "Out.java").write_text("class Out {}", encoding="utf-8")

        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mc_librarian.fetch.tools.subprocess.run", fake_run)

    jar = tmp_path / "vineflower.jar"
    jar.write_bytes(b"jar")
    source = tmp_path / "client.jar"

    with zipfile.ZipFile(source, "w") as archive:
        for name in ("net/minecraft/world/level/A", "net/minecraft/world/level/B", "com/mojang/C"):
            archive.writestr(f"{name}.class", b"")

    dest = tmp_path / "out"

    VineflowerDecompiler(
        jar_path=jar, java="java", memory="1G", chunk_classes=2, threads=2
    ).decompile(source, dest)

    assert len(recorded) == 2, "3 个类、每块 2 个 → 应分 2 次调用"
    assert all("-Xmx1G" in command for command, _ in recorded)
    assert all("--thread-count=2" in command for command, _ in recorded)
    assert all("-XX:MaxMetaspaceSize=192m" in command for command, _ in recorded)
    # 每块只带自己那些类（子 jar），不再用 --only。
    assert all("--only" not in " ".join(command) for command, _ in recorded)
    assert sorted(sum((entries for _, entries in recorded), [])) == [
        "com/mojang/C",
        "net/minecraft/world/level/A",
        "net/minecraft/world/level/B",
    ]
    # 产物合并进目标树，临时目录已清理。
    assert list(dest.rglob("Out.java"))
    assert not (dest.parent / f".{dest.name}.chunks").exists()
