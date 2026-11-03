"""tree-sitter Java 解析：把一份 `.java` 源码抽成类 / 方法 / 字段与引用。

规格见 issue #5 的规格补充 §1（字段语义）、§2（1-based 闭区间行号）、§5（引用提取规则）。
解析刻意是**启发式**的：不做作用域解析、不做类型检查，只按语法节点取名字，因此允许误报——
误报由调用方 `read` 时证伪。

行号：`start_line` 取声明节点的起始行（**含注解行**），`end_line` 取声明节点的结束行
（方法/类的右花括号行）；都是 1-based。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Final

import tree_sitter_java
from tree_sitter import Language, Node, Parser

_LANGUAGE: Final = Language(tree_sitter_java.language())

# 会产出 classes 行的五种声明。
TYPE_DECLARATION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)

_KIND_BY_DECLARATION: Final[dict[str, str]] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}

_KEYWORD_BY_DECLARATION: Final[dict[str, str]] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "@interface",
}

_METHOD_DECLARATIONS: Final[frozenset[str]] = frozenset(
    {"method_declaration", "constructor_declaration"}
)
_FIELD_DECLARATIONS: Final[frozenset[str]] = frozenset(
    {"field_declaration", "constant_declaration"}
)
# 类体级别的可执行块：字段初始化器与实例/静态初始化块，其引用归属到 `<clinit>`。
_INITIALIZERS: Final[frozenset[str]] = frozenset({"static_initializer", "block"})
_ANNOTATION_KINDS: Final[frozenset[str]] = frozenset({"marker_annotation", "annotation"})
_PRIMITIVE_TYPES: Final[frozenset[str]] = frozenset(
    {"integral_type", "floating_point_type", "boolean_type", "void_type"}
)
# `_type_names` 只在这些节点里继续下钻，避免把变量名当成类型名。
_TYPE_CONTAINERS: Final[frozenset[str]] = frozenset(
    {"generic_type", "array_type", "annotated_type", "type_arguments", "wildcard", "type_list"}
)

# 类体级别引用挂到的伪方法名（规格 §5）。
CLINIT_NAME: Final = "<clinit>"


class JavaParseError(RuntimeError):
    """源码无法解析：tree-sitter 在语法树上留下了 ERROR 节点。"""


@dataclass(frozen=True)
class ParsedRef:
    """一条启发式引用（`refs` 表的一行，尚未绑定 `from_method_id`）。"""

    target_name: str
    kind: str
    line: int


@dataclass
class ParsedMethod:
    """`methods` 表的一行；`refs` 是该方法的引用（含签名里的类型引用）。"""

    name: str
    signature: str | None
    return_type: str | None
    params: list[str]
    modifiers: list[str]
    annotations: list[str]
    start_line: int
    end_line: int
    refs: list[ParsedRef] = field(default_factory=list)


@dataclass
class ParsedField:
    """`fields` 表的一行。"""

    name: str
    type: str | None
    modifiers: list[str]
    annotations: list[str]
    start_line: int
    end_line: int


@dataclass
class ParsedClass:
    """`classes` 表的一行 + 它的方法与字段；`class_refs` 是类体级别的引用。"""

    fqn: str
    simple: str
    kind: str
    signature: str | None
    superclass: str | None
    interfaces: list[str]
    annotations: list[str]
    start_line: int
    end_line: int
    methods: list[ParsedMethod] = field(default_factory=list)
    fields: list[ParsedField] = field(default_factory=list)
    class_refs: list[ParsedRef] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedFile:
    """一个 `.java` 文件的解析结果。"""

    path: str
    package: str | None
    imports: list[str]
    classes: list[ParsedClass]


_parser_local = threading.local()


def _parser() -> Parser:
    """线程内复用 `Parser`：tree-sitter 的 `Parser` 不能跨线程并发使用。"""
    parser: Parser | None = getattr(_parser_local, "parser", None)

    if parser is None:
        parser = Parser(_LANGUAGE)
        _parser_local.parser = parser

    return parser


def parse_java(text: str, *, path: str) -> ParsedFile:
    """解析一份 Java 源码；语法树里有 ERROR 节点时抛 `JavaParseError`（规格 §4）。"""
    root = _parser().parse(text.encode("utf-8")).root_node

    if root.has_error:
        raise JavaParseError("tree-sitter 在语法树上留下了 ERROR 节点")

    package = _package_name(root)
    classes: list[ParsedClass] = []

    for child in root.named_children:
        if child.type in TYPE_DECLARATION_KINDS:
            _collect_class(child, parent_fqn=None, package=package, out=classes)

    return ParsedFile(path=path, package=package, imports=_imports(root), classes=classes)


def parse_imports(text: str) -> list[str]:
    """只取 import 列表（`/v1/outline` 用）。语法有错也照样返回已读到的部分。"""
    return _imports(_parser().parse(text.encode("utf-8")).root_node)


# ----------------------------------------------------------------------
# 节点工具
# ----------------------------------------------------------------------


def _text(node: Node | None) -> str:
    if node is None:
        return ""

    raw = node.text

    return "" if raw is None else raw.decode("utf-8")


def _normalize(text: str) -> str:
    """把多行/多空白的源码片段压成单行，签名里不出现换行。"""
    return " ".join(text.split())


def _line(node: Node) -> int:
    """1-based 起始行。"""
    return node.start_point.row + 1


def _end_line(node: Node) -> int:
    """1-based 结束行（含）。"""
    return node.end_point.row + 1


def _first_child_of_type(node: Node, *kinds: str) -> Node | None:
    for child in node.named_children:
        if child.type in kinds:
            return child

    return None


def _modifiers_node(node: Node) -> Node | None:
    for child in node.children:
        if child.type == "modifiers":
            return child

    return None


def _split_modifiers(node: Node) -> tuple[list[str], list[str]]:
    """返回 (修饰符关键字, 注解名)。

    注解只取名字（不含 `@` 与参数），与 `superclass`/`interfaces` 一样是裸名字。
    """
    modifiers_node = _modifiers_node(node)

    if modifiers_node is None:
        return [], []

    keywords: list[str] = []
    annotations: list[str] = []

    for child in modifiers_node.children:
        # 注解是具名节点；修饰符关键字（public/static/…）是匿名词法节点。
        if child.is_named and child.type in _ANNOTATION_KINDS:
            name = child.child_by_field_name("name")

            if name is None:
                name = _first_child_of_type(child, "identifier")

            if name is not None:
                annotations.append(_text(name))
        elif not child.is_named:
            keywords.append(_text(child))

    return keywords, annotations


def _type_names(node: Node | None) -> list[str]:
    """类型节点里出现的类型名（去掉包前缀，泛型参数里的类型也算）。"""
    if node is None:
        return []

    kind = node.type

    if kind == "type_identifier":
        return [_text(node)]

    if kind == "scoped_type_identifier":
        # `java.util.Date` → 只取最后一段 `Date`，不把 `java`/`util` 当类型。
        segments = [child for child in node.children if child.type == "type_identifier"]

        return [_text(segments[-1])] if segments else []

    if kind in _PRIMITIVE_TYPES or kind in _ANNOTATION_KINDS or kind == "modifiers":
        return []

    if kind in _TYPE_CONTAINERS:
        names: list[str] = []

        for child in node.named_children:
            names.extend(_type_names(child))

        return names

    return []


def _base_type_name(node: Node | None) -> str | None:
    """类型节点的主类型名：`List<T>` → `List`，`a.b.C` → `C`。"""
    names = _type_names(node)

    return names[0] if names else None


def _type_text(node: Node | None) -> str | None:
    """类型节点的源码文本（保留泛型），压成单行。"""
    if node is None:
        return None

    text = _normalize(_text(node))

    return text or None


def _superclass_node(node: Node) -> Node | None:
    superclass = node.child_by_field_name("superclass")

    if superclass is None:
        return None

    return _first_child_of_type(
        superclass, "type_identifier", "scoped_type_identifier", "generic_type"
    )


def _interface_nodes(node: Node) -> list[Node]:
    interfaces = node.child_by_field_name("interfaces")

    if interfaces is None:
        return []

    type_list = _first_child_of_type(interfaces, "type_list")

    if type_list is None:
        return []

    return [
        child
        for child in type_list.named_children
        if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}
    ]


def _package_name(root: Node) -> str | None:
    declaration = _first_child_of_type(root, "package_declaration")

    if declaration is None:
        return None

    name = _first_child_of_type(declaration, "scoped_identifier", "identifier")

    return _text(name) if name is not None else None


def _imports(root: Node) -> list[str]:
    """import 的名字列表：去掉 `import` 与 `;`，保留 `static`（如 `static a.b.max`）。"""
    imports: list[str] = []

    for child in root.named_children:
        if child.type != "import_declaration":
            continue

        text = _normalize(_text(child)).removeprefix("import ").removesuffix(";").strip()

        if text:
            imports.append(text)

    return imports


# ----------------------------------------------------------------------
# 符号收集
# ----------------------------------------------------------------------


def _collect_class(
    node: Node, *, parent_fqn: str | None, package: str | None, out: list[ParsedClass]
) -> None:
    """把一个类型声明收成 `ParsedClass`，并递归它的成员与内部类。"""
    simple = _text(node.child_by_field_name("name")) or "?"

    if parent_fqn:
        fqn = f"{parent_fqn}.{simple}"
    elif package:
        fqn = f"{package}.{simple}"
    else:
        fqn = simple

    modifiers, annotations = _split_modifiers(node)
    superclass_node = _superclass_node(node)
    interface_nodes = _interface_nodes(node)
    interfaces: list[str] = []

    for child in interface_nodes:
        name = _base_type_name(child)

        if name is not None:
            interfaces.append(name)

    parsed = ParsedClass(
        fqn=fqn,
        simple=simple,
        kind=_KIND_BY_DECLARATION[node.type],
        signature=_class_signature(node, modifiers, simple),
        superclass=_base_type_name(superclass_node),
        interfaces=interfaces,
        annotations=annotations,
        start_line=_line(node),
        end_line=_end_line(node),
    )
    out.append(parsed)

    # superclass / interfaces 的类型名是类体级别的 `type` 引用（规格 §5）。
    for type_node in [superclass_node, *interface_nodes]:
        if type_node is None:
            continue

        for name in _type_names(type_node):
            parsed.class_refs.append(ParsedRef(name, "type", _line(type_node)))

    if node.type == "record_declaration":
        _collect_record_components(node, parsed)

    body = node.child_by_field_name("body")

    if body is not None:
        for member in body.named_children:
            _collect_member(member, parsed, package, out)

    if parsed.class_refs:
        # 类体级别（字段初始化器、静态块、父类/接口类型）的引用挂到 `<clinit>` 伪方法；
        # 没有类体级别引用就不建这一行（规格 §5）。
        parsed.methods.append(
            ParsedMethod(
                name=CLINIT_NAME,
                signature="static {}",
                return_type=None,
                params=[],
                modifiers=["static"],
                annotations=[],
                start_line=parsed.start_line,
                end_line=parsed.start_line,
                refs=parsed.class_refs,
            )
        )


def _collect_member(
    member: Node, parsed: ParsedClass, package: str | None, out: list[ParsedClass]
) -> None:
    kind = member.type

    if kind in _METHOD_DECLARATIONS:
        method = _build_method(member, parsed)
        parsed.methods.append(method)
        _collect_signature_type_refs(member, method.refs)
        body = member.child_by_field_name("body")

        if body is not None:
            _collect_refs(body, method.refs)
    elif kind in _FIELD_DECLARATIONS:
        parsed.fields.extend(_build_fields(member))
        _collect_signature_type_refs(member, parsed.class_refs)
        _collect_refs(member, parsed.class_refs)
    elif kind == "annotation_type_element_declaration":
        method = _build_annotation_element(member)
        parsed.methods.append(method)
        _collect_signature_type_refs(member, method.refs)
    elif kind in _INITIALIZERS:
        _collect_refs(member, parsed.class_refs)
    elif kind in TYPE_DECLARATION_KINDS:
        _collect_class(member, parent_fqn=parsed.fqn, package=package, out=out)
    elif kind == "enum_constant":
        # 枚举常量的构造参数与匿名类体都是类体级别（规格 §5）。
        _collect_refs(member, parsed.class_refs)
    elif kind == "enum_body_declarations":
        for sub in member.named_children:
            _collect_member(sub, parsed, package, out)
    else:
        _collect_refs(member, parsed.class_refs)


def _build_method(node: Node, parsed_class: ParsedClass) -> ParsedMethod:
    modifiers, annotations = _split_modifiers(node)
    params_node = node.child_by_field_name("parameters")

    if node.type == "constructor_declaration":
        name = parsed_class.simple
        return_type = None
    else:
        name = _text(node.child_by_field_name("name"))
        return_type = _type_text(node.child_by_field_name("type"))

    return ParsedMethod(
        name=name,
        signature=_method_signature(node, modifiers, return_type, name, params_node),
        return_type=return_type,
        params=_param_types(params_node),
        modifiers=modifiers,
        annotations=annotations,
        start_line=_line(node),
        end_line=_end_line(node),
    )


def _build_annotation_element(node: Node) -> ParsedMethod:
    """`@interface` 的元素：`String value() default "x";` → 名为 `value` 的方法。"""
    modifiers, annotations = _split_modifiers(node)
    name = _text(node.child_by_field_name("name"))
    return_type = _type_text(node.child_by_field_name("type"))
    signature = _normalize(f"{return_type or ''} {name}()")

    return ParsedMethod(
        name=name,
        signature=signature,
        return_type=return_type,
        params=[],
        modifiers=modifiers,
        annotations=annotations,
        start_line=_line(node),
        end_line=_end_line(node),
    )


def _build_fields(node: Node) -> list[ParsedField]:
    modifiers, annotations = _split_modifiers(node)
    type_text = _type_text(node.child_by_field_name("type"))
    fields: list[ParsedField] = []

    for declarator in node.children_by_field_name("declarator"):
        name = _text(declarator.child_by_field_name("name"))

        if not name:
            continue

        fields.append(
            ParsedField(
                name=name,
                type=type_text,
                modifiers=modifiers,
                annotations=annotations,
                start_line=_line(node),
                end_line=_end_line(node),
            )
        )

    return fields


def _collect_record_components(node: Node, parsed: ParsedClass) -> None:
    """record 的组件就是它的状态，收成字段。"""
    params = node.child_by_field_name("parameters")

    if params is None:
        return

    for parameter in params.named_children:
        if parameter.type != "formal_parameter":
            continue

        name = _text(parameter.child_by_field_name("name"))

        if not name:
            continue

        modifiers, annotations = _split_modifiers(parameter)
        parsed.fields.append(
            ParsedField(
                name=name,
                type=_type_text(parameter.child_by_field_name("type")),
                modifiers=modifiers,
                annotations=annotations,
                start_line=_line(parameter),
                end_line=_end_line(parameter),
            )
        )


# ----------------------------------------------------------------------
# 签名
# ----------------------------------------------------------------------


def _class_signature(node: Node, modifiers: list[str], simple: str) -> str:
    head = _normalize(" ".join([*modifiers, _KEYWORD_BY_DECLARATION[node.type], simple]))
    type_params = node.child_by_field_name("type_parameters")

    if type_params is not None:
        head += _normalize(_text(type_params))

    if node.type == "record_declaration":
        components = node.child_by_field_name("parameters")

        if components is not None:
            head += _normalize(_text(components))

    parts = [head]
    superclass = node.child_by_field_name("superclass")

    if superclass is not None:
        parts.append(_normalize(_text(superclass)))

    interfaces = node.child_by_field_name("interfaces")

    if interfaces is not None:
        parts.append(_normalize(_text(interfaces)))

    return _normalize(" ".join(parts))


def _method_signature(
    node: Node, modifiers: list[str], return_type: str | None, name: str, params_node: Node | None
) -> str:
    parts = list(modifiers)
    type_params = node.child_by_field_name("type_parameters")

    if type_params is not None:
        parts.append(_normalize(_text(type_params)))

    if return_type:
        parts.append(return_type)

    parts.append(name)
    params = _normalize(_text(params_node)) if params_node is not None else "()"

    return _normalize(f"{' '.join(parts)}{params}")


def _param_types(params_node: Node | None) -> list[str]:
    if params_node is None:
        return []

    types: list[str] = []

    for parameter in params_node.named_children:
        text = _type_text(parameter.child_by_field_name("type"))

        if text is not None:
            types.append(text)

    return types


def _collect_signature_type_refs(node: Node, bucket: list[ParsedRef]) -> None:
    """返回类型与参数类型里的类型名（规格 §5 的第四类）。"""
    type_node = node.child_by_field_name("type")

    if type_node is not None:
        for name in _type_names(type_node):
            bucket.append(ParsedRef(name, "type", _line(type_node)))

    params_node = node.child_by_field_name("parameters")

    if params_node is None:
        return

    for parameter in params_node.named_children:
        param_type = parameter.child_by_field_name("type")

        if param_type is None:
            continue

        for name in _type_names(param_type):
            bucket.append(ParsedRef(name, "type", _line(param_type)))


# ----------------------------------------------------------------------
# 引用提取（规格 §5）
# ----------------------------------------------------------------------


def _collect_refs(node: Node, bucket: list[ParsedRef]) -> None:
    """递归收集 `node` 子树里的调用 / 字段访问 / 建对象引用。"""
    kind = node.type

    if kind == "method_invocation":
        name = node.child_by_field_name("name")

        if name is not None:
            bucket.append(ParsedRef(_text(name), "call", _line(node)))
    elif kind == "field_access":
        receiver = node.child_by_field_name("object")
        field_node = node.child_by_field_name("field")

        # 接收者是 `this` 时不算引用（规格 §5）。
        if field_node is not None and (receiver is None or receiver.type != "this"):
            bucket.append(ParsedRef(_text(field_node), "field", _line(node)))
    elif kind == "object_creation_expression":
        for type_name in _type_names(node.child_by_field_name("type")):
            bucket.append(ParsedRef(type_name, "type", _line(node)))

    for child in node.named_children:
        _collect_refs(child, bucket)
