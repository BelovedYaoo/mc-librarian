"""反编译分块：把「整个 jar」拆成若干类名前缀，逐块丢给 Vineflower。

为什么需要它：Vineflower 的内存峰值由**一次要处理的类数量**决定，不是 jar 大小。

实测（本机，`-XX:ActiveProcessorCount=2 --thread-count=2`）：

| 输入 | 堆 | 结果 |
|---|---|---|
| 1.20.1 整包（7,436 类） | 1G | OOM |
| 1.20.1 整包 | 1536m | 峰值 1,719 MB |
| 26.3 整包（11,383 类） | 2G | OOM |
| 26.3 `net/minecraft/world/level/`（2,176 类） | 1G | 峰值 1,171 MB |

所以「按前缀切块 + 每块 1G」能把峰值从 >2G 压到 ~1.2G，3.5 GiB 的节点才跑得动。
"""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path
from typing import Final

# 切到第几级包名。4 足够把 `net/minecraft/world/level` 与 `.../entity` 分开；
# 再深会切得太碎（每块都要重新扫一遍 jar，时间反而变长）。
DEFAULT_CHUNK_DEPTH: Final = 4


def class_names_of(jar: Path) -> list[str]:
    """jar 里的类名（`net/minecraft/Foo$Bar`）；跳过 `META-INF` 与其它资源。"""
    with zipfile.ZipFile(jar) as archive:
        return [
            name[: -len(".class")]
            for name in archive.namelist()
            if name.endswith(".class") and not name.startswith("META-INF/")
        ]


def _prefix_of(class_name: str, depth: int) -> str:
    return "/".join(class_name.split("/")[:depth])


def plan_chunks(
    class_names: list[str],
    *,
    max_classes: int,
    depth: int = DEFAULT_CHUNK_DEPTH,
) -> list[tuple[list[str], int]]:
    """把类名切成若干前缀组，每组尽量不超过 `max_classes`。返回 `[(前缀列表, 类数)]`。

    规则：

    1. 先按第 `depth` 级包名分组；单个前缀超过上限就**再深一级**递归切开
       （例如 `net/minecraft/world/level` 2,176 类 → 按第 5 级拆成更小的块）。
    2. 剩下的前缀按类数从大到小贪心打包，直到再加就超上限。
    3. 调用方给每个前缀加 `"/"` 后缀作为 `--only` 模式，避免 `Foo` 误匹配 `FooBar`
       （Vineflower 的 `--only` 是前缀匹配）。

    切不动时（类名段数已经到顶）保留超限块——宁可内存高一点，也不能漏类。
    """
    if max_classes <= 0:
        return [([], len(class_names))]

    counted = _split_to_fit(class_names, max_classes=max_classes, depth=depth)
    # 打包：大的先放，减少「最后剩一个塞不进去」的碎片。
    ordered = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    chunks: list[tuple[list[str], int]] = []
    current: list[str] = []
    current_size = 0

    for prefix, count in ordered:
        if current and current_size + count > max_classes:
            chunks.append((current, current_size))
            current, current_size = [], 0

        current.append(prefix)
        current_size += count

    if current:
        chunks.append((current, current_size))

    return chunks


def _split_to_fit(
    class_names: list[str], *, max_classes: int, depth: int
) -> dict[str, int]:
    """按 `depth` 分组；超限的组继续加深，直到每组的类数都不超过上限。"""
    groups = Counter(_prefix_of(name, depth) for name in class_names)
    result: dict[str, int] = {}

    for prefix, count in groups.items():
        if count <= max_classes:
            result[prefix] = count
            continue

        children = [name for name in class_names if _prefix_of(name, depth) == prefix]
        # 只要还有类带更深的段，就能再拆一级；否则保留超限块（宁可多占内存也不漏类）。
        splittable = any(name.count("/") >= depth for name in children)

        if splittable:
            result.update(_split_to_fit(children, max_classes=max_classes, depth=depth + 1))
        else:
            result[prefix] = count

    return result


def select_classes(class_names: list[str], prefixes: list[str]) -> list[str]:
    """按前缀组挑出属于这一块的类。

    前缀可能是包名，也可能是类名（切到类这一级时）：包名按 `prefix + "/"` 匹配，
    类名按「本身或内部类（`$`）」匹配。
    """
    selected: list[str] = []

    for name in class_names:
        for prefix in prefixes:
            if name == prefix or name.startswith(f"{prefix}/") or name.startswith(f"{prefix}$"):
                selected.append(name)
                break

    return selected
