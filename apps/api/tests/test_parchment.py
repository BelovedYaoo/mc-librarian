"""Parchment 参数名重写：行号不变、槽位偏移正确、重名/歧义跳过。"""

from __future__ import annotations

from pathlib import Path

from mc_librarian.indexing import parchment


def _data() -> parchment.ParchmentData:
    data = parchment.ParchmentData()
    data.classes["net/minecraft/world/level/NaturalSpawner$SpawnState"] = {
        ("canSpawnForCategory", 2): [
            parchment.ParchmentParams(
                slots={1: "category", 2: "pos"},
                types=("MobCategory", "ChunkPos"),
            )
        ]
    }
    data.classes["net/minecraft/Foo"] = {
        ("bar", 1): [parchment.ParchmentParams(slots={0: "value"}, types=("int",))]
    }

    return data


SOURCE = """package net.minecraft.world.level;

final class NaturalSpawner {
    static class SpawnState {
        boolean canSpawnForCategory(MobCategory $$0, ChunkPos $$1) {
            return $$0 != null && $$1 != null;
        }
    }
}
"""

STATIC_SOURCE = """package net.minecraft;

class Foo {
    static void bar(int $$0) {
        System.out.println($$0);
    }
}
"""


def test_rewrite_keeps_line_count_and_records_originals() -> None:
    rewritten, mapping = parchment.rewrite_source(
        SOURCE, "net.minecraft.world.level.NaturalSpawner", _data()
    )

    assert rewritten.count("\n") == SOURCE.count("\n")
    assert "canSpawnForCategory(MobCategory category, ChunkPos pos)" in rewritten
    assert "$$0" not in rewritten and "$$1" not in rewritten
    # 方法起始行 → {ordinal: 原名}
    assert mapping[next(iter(mapping))] == {0: "$$0", 1: "$$1"}


def test_static_method_uses_slot_zero() -> None:
    rewritten, mapping = parchment.rewrite_source(STATIC_SOURCE, "net.minecraft.Foo", _data())

    assert "void bar(int value)" in rewritten
    assert mapping


def test_unknown_class_is_left_untouched() -> None:
    text = "class X { void y(int $$0) {} }"
    rewritten, mapping = parchment.rewrite_source(text, "net.minecraft.X", _data())

    assert rewritten == text
    assert mapping == {}
