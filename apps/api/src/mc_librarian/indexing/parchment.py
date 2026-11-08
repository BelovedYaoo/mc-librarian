"""Parchment 参数名：下载、解析、源码重写。

Parchment 是「官方 mappings 之上的参数名 / javadoc」数据
（maven：`org.parchmentmc.data:parchment-<mc>`）。
我们只用参数名，不用 javadoc——**只替换标识符、不动换行**，这样两棵源码树行号 1:1，
引用与历史定位都不需要迁移。

原版源码里参数名是 Vineflower 的位置占位（`$$0`/`$$1`），Forge 是 SRG 占位（`p_186549_`）。
Parchment 的 `index` 是 JVM 局部变量槽位：实例方法从 1 开始、静态方法从 0 开始，
long/double 占两个槽，合成参数（`this$0`、枚举 `$enum$*`）要跳过。
"""

from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import tree_sitter_java
from tree_sitter import Language, Node, Parser

_LANGUAGE: Final = Language(tree_sitter_java.language())
_PARSER: Final = Parser(_LANGUAGE)

TYPE_DECLARATIONS: Final[frozenset[str]] = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
_METHOD_DECLARATIONS: Final[frozenset[str]] = frozenset(
    {"method_declaration", "constructor_declaration"}
)
_WIDE_TYPES: Final[frozenset[str]] = frozenset({"long", "double"})

PARCHMENT_MAP_NAME: Final = ".parchment-map.json"
DEFAULT_MAVEN: Final = "https://maven.parchmentmc.org"
METADATA_PATH: Final = "org/parchmentmc/data/parchment-{mc}/maven-metadata.xml"


@dataclass(frozen=True)
class ParchmentParams:
    """一个方法的 Parchment 参数：`JVM 槽位 → name` 与类型（用于同 arity 的重载消歧）。"""

    slots: dict[int, str]
    types: tuple[str, ...]


@dataclass
class ParchmentData:
    """`(类名斜杠形式, 方法名, 参数个数) → 候选参数表`（同 arity 的重载可能有多个）。"""

    classes: dict[str, dict[tuple[str, int], list[ParchmentParams]]] = field(
        default_factory=dict
    )

    def lookup(
        self,
        class_fqn: str,
        method_name: str,
        arity: int,
        type_simples: tuple[str, ...],
        *,
        is_static: bool,
    ) -> dict[int, str] | None:
        """按类 + 方法名 + arity + 类型简单名找唯一候选，返回 `ordinal → Parchment 名`。

        有歧义时返回 `None`（宁可不改）。槽位偏移由源码的 `static` 决定：静态 0、实例 1。
        """
        for name in _class_name_variants(class_fqn):
            methods = self.classes.get(name)

            if methods is None:
                continue

            candidates = methods.get((method_name, arity))

            if not candidates:
                continue

            matched = [c for c in candidates if c.types == type_simples]
            # 同一份方法可能因为「按两种 static 偏移」各记一次，去重后只剩一个。
            unique = {(c.types, tuple(sorted(c.slots.items()))) for c in matched}

            if len(unique) != 1:
                continue

            selected = matched[0]
            offset = 0 if is_static else 1
            mapping: dict[int, str] = {}

            for ordinal in range(arity):
                param_name = selected.slots.get(_slot_of(ordinal, selected.types, offset))

                if param_name is not None and param_name != "":
                    mapping[ordinal] = param_name

            if mapping:
                return mapping

        return None


def _class_name_variants(fqn: str) -> list[str]:
    """`a.b.C.Inner` 的几种可能类名：全斜杠，以及把末尾若干个点换成 `$`。"""
    parts = fqn.split(".")
    variants = ["/".join(parts)]

    for count in range(1, min(len(parts) - 1, 4) + 1):
        head = parts[: len(parts) - count]
        tail = "$".join(parts[len(parts) - count :])
        variants.append("/".join([*head, tail]))

    return variants


def load_parchment(zip_path: Path) -> ParchmentData:
    """读 `parchment.json`（zip 内单文件）；结构不符的条目跳过，不阻断构建。"""
    data = ParchmentData()

    with zipfile.ZipFile(zip_path) as archive:
        payload = json.loads(archive.read("parchment.json"))

    for cls in payload.get("classes", []):
        raw_name = str(cls.get("name", "")).replace(".", "/")
        methods: dict[tuple[str, int], list[ParchmentParams]] = {}

        for method in cls.get("methods", []):
            name = str(method.get("name", ""))
            parameters = method.get("parameters") or []

            if not name or not parameters:
                continue

            slot_to_name = {
                int(item["index"]): str(item["name"])
                for item in parameters
                if "index" in item and "name" in item
            }
            types = tuple(_descriptor_types(str(method.get("descriptor", ""))))

            if not types:
                continue

            # 槽位 → 参数序号：静态方法第 0 个参数在 slot 0；实例方法在 slot 1。
            # 这里不知道 static 与否，先按两种可能都记一份，改写时按源码的 static 选。
            methods.setdefault((name, len(types)), []).append(
                ParchmentParams(slots=slot_to_name, types=types)
            )

        if methods:
            data.classes[raw_name] = methods

    return data


def _descriptor_types(descriptor: str) -> list[str]:
    """`(Lnet/minecraft/world/entity/MobCategory;I)V` → `["MobCategory", "int"]`。"""
    inner = descriptor[1 : descriptor.index(")")] if descriptor.startswith("(") else ""
    types: list[str] = []
    index = 0

    while index < len(inner):
        char = inner[index]

        if char == "L":
            end = inner.index(";", index)
            raw = inner[index + 1 : end]
            types.append(raw.rsplit("/", 1)[-1])
            index = end + 1
        elif char == "[":
            index += 1
        else:
            primitive = {
                "Z": "boolean",
                "B": "byte",
                "C": "char",
                "S": "short",
                "I": "int",
                "J": "long",
                "F": "float",
                "D": "double",
            }.get(char, "?")
            types.append(primitive)
            index += 1

    return types


def _slot_of(ordinal: int, types: tuple[str, ...], offset: int) -> int:
    slot = offset

    for type_name in types[:ordinal]:
        slot += 2 if type_name in _WIDE_TYPES else 1

    return slot


def rewrite_source(
    text: str, class_fqn: str, parchment: ParchmentData
) -> tuple[str, dict[int, dict[int, str]]]:
    """把一份源码里的参数名换成 Parchment 名。

    返回 `(新文本, {方法起始行: {ordinal: 原名}})`；匹配不到就原样返回（空映射）。
    """
    source = text.encode("utf-8")
    tree = _PARSER.parse(source)
    originals: dict[int, dict[int, str]] = {}
    edits: list[tuple[int, int, bytes]] = []

    def visit(node: Node) -> None:
        if node.type in TYPE_DECLARATIONS:
            simple = _text(node, "name", source)
            fqn = class_fqn if class_fqn else simple
            body = node.child_by_field_name("body")

            # 方法在 `class_body` 里，不是类声明的直接子节点。
            for child in body.named_children if body is not None else node.named_children:
                visit_member(child, fqn)

            return

        for child in node.named_children:
            visit(child)

    def visit_member(node: Node, fqn: str) -> None:
        if node.type in TYPE_DECLARATIONS:
            nested = _text(node, "name", source)
            inner = f"{fqn}.{nested}" if fqn else nested
            body = node.child_by_field_name("body")

            for child in body.named_children if body is not None else node.named_children:
                visit_member(child, inner)

            return

        if node.type in _METHOD_DECLARATIONS:
            _plan_method(node, fqn)

    def _plan_method(node: Node, fqn: str) -> None:
        params_node = node.child_by_field_name("parameters")

        if params_node is None:
            return

        method_name = (
            _text(node, "name", source)
            or fqn.rsplit(".", 1)[-1]
        )
        params = [
            child
            for child in params_node.named_children
            if child.type in {"formal_parameter", "spread_parameter", "receiver_parameter"}
        ]
        names = [_param_name(child, source) for child in params]
        types = tuple(
            _simple_type(_type_text(child, source))
            for child in params
        )

        if any(name == "" for name in names):
            return

        is_static = "static" in _modifiers(node, source)
        candidates = parchment.lookup(
            fqn, method_name, len(params), types, is_static=is_static
        )

        if candidates is None:
            return

        line = node.start_point[0] + 1
        mapping: dict[int, str] = {}

        for ordinal, parchment_name in candidates.items():
            if ordinal >= len(names):
                continue

            original = names[ordinal]

            if parchment_name == "" or original == "" or original == parchment_name:
                continue

            mapping[ordinal] = original

            # 方法体内所有出现都要换（引用不止声明那一处）。
            for start, end in _identifier_spans(node, original, source):
                edits.append((start, end, parchment_name.encode("utf-8")))

        if mapping:
            originals[line] = mapping

    visit(tree.root_node)

    if not edits:
        return text, originals

    # 从后往前替换，前面的偏移不会失效。
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]

    return source.decode("utf-8"), originals


def _identifier_spans(node: Node, name: str, source: bytes) -> list[tuple[int, int]]:
    """方法节点范围内所有 `name` 的字节区间（按 ASCII 标识符边界）。"""
    pattern = re.compile(rb"(?<![\w$])" + re.escape(name.encode("utf-8")) + rb"(?![\w$])")

    return [
        (match.start(), match.end())
        for match in pattern.finditer(source, node.start_byte, node.end_byte)
    ]


def _text(node: Node, field: str, source: bytes) -> str:
    child = node.child_by_field_name(field)

    return "" if child is None else (child.text or b"").decode("utf-8", errors="replace")


def _param_name(node: Node, source: bytes) -> str:
    name = node.child_by_field_name("name")

    if name is not None:
        return (name.text or b"").decode("utf-8", errors="replace")

    declarator = next(
        (child for child in node.named_children if child.type == "variable_declarator"),
        None,
    )

    if declarator is None:
        return ""

    return _text(declarator, "name", source)


def _type_text(node: Node, source: bytes) -> str:
    type_node = node.child_by_field_name("type")

    return (
        "" if type_node is None else (type_node.text or b"").decode("utf-8", errors="replace")
    )


def _simple_type(raw: str) -> str:
    """去掉泛型、数组、注解，取简单名：`List<Foo>[]` → `List`。"""
    text = re.sub(r"@\w+(\([^)]*\))?", " ", raw)
    text = text.split("<", 1)[0]
    text = text.replace("...", "").replace("[]", "").strip()
    text = text.rsplit(".", 1)[-1]

    return text.strip()


def _modifiers(node: Node, _source: bytes) -> set[str]:
    block = next((child for child in node.named_children if child.type == "modifiers"), None)

    if block is None:
        return set()

    # 修饰符（`static`/`public`…）在 tree-sitter 里是匿名 token，`named_children` 取不到；
    # 直接按文本切词（注解参数里的单词混进来也无所谓，只判断是否含 `static`）。
    return set((block.text or b"").decode("utf-8", errors="replace").replace("(", " ").split())


def fetch_parchment(
    mc_version: str, dest: Path, *, base: str = DEFAULT_MAVEN, timeout: float = 30.0
) -> Path:
    """下载指定 MC 版本的 Parchment release zip 到 `dest`（已存在则复用）。"""
    if dest.is_file():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    metadata_url = f"{base}/{METADATA_PATH.format(mc=mc_version)}"

    with urllib.request.urlopen(metadata_url, timeout=timeout) as response:
        metadata = response.read().decode("utf-8", errors="replace")

    release = re.search(r"<release>([^<]+)</release>", metadata)
    version = release.group(1) if release else None

    if version is None:
        raise RuntimeError(f"Parchment 元数据里没有 release 版本：{metadata_url}")

    artifact = (
        f"{base}/org/parchmentmc/data/parchment-{mc_version}/{version}/"
        f"parchment-{mc_version}-{version}.zip"
    )

    with urllib.request.urlopen(artifact, timeout=timeout) as response:
        payload = response.read()

    dest.write_bytes(payload)

    return dest


def apply_tree(root: Path, data: ParchmentData) -> dict[str, dict[int, dict[int, str]]]:
    """把 `root` 下所有 `.java` 的参数名改成 Parchment 名，返回原始名映射。

    `root` 必须是**可读树**（会被原地改写）；调用方负责先把原树备份到
    `src-official/<version>/`。返回的映射写成 `root/.parchment-map.json`，`build` 阶段据此
    在 `params` 表里同时记录可读名与原名。
    """
    originals: dict[str, dict[int, dict[int, str]]] = {}

    for path in sorted(root.rglob("*.java")):
        relative = path.relative_to(root)
        # 以**文件路径**（含 `.java`，与 `files.path` 同口径）为键：方法行号在文件内唯一，
        # 而一个文件可能含多个（内部）类，用类 fqn 做键会让内部类的方法找不到原名。
        file_key = relative.as_posix()
        fqn = ".".join(relative.with_suffix("").parts)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rewritten, mapping = rewrite_source(text, fqn, data)

        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")

        if mapping:
            originals[file_key] = mapping

    if originals:
        (root / PARCHMENT_MAP_NAME).write_text(
            json.dumps(originals, ensure_ascii=False, indent=0), encoding="utf-8"
        )

    return originals
