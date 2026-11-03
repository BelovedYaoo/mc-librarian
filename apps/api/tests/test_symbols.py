"""Java 符号索引与四个检索端点（issue #5）。

解析 / 构建 / 查询都在临时数据根里跑：源码用仓库 fixture（5 个 `.java` + 1 个 `data` JSON），
索引库写在 `tmp_path`，不碰仓库里的真实数据（`mc-librarian-data/`）。
"""

from __future__ import annotations

import gc
import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from mc_librarian.config import Settings
from mc_librarian.db import Database, transaction
from mc_librarian.indexing import java, query
from mc_librarian.indexing.build import BuildError, build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"

VERSION = "26.3"
MOB_PATH = "net/minecraft/world/Mob.java"
WORLD_PATH = "net/minecraft/world/World.java"
CONTAINER_PATH = "net/minecraft/world/Container.java"

OK_JAVA = "package net;\n\npublic class {name} {{\n    void ping() {{}}\n}}\n"
BROKEN_JAVA = "public class Broken { void x( } }\n"


def silent(_level: str, _message: str) -> None:
    """`build_index` 的日志回调；不关心的测试用它吞掉输出。"""


# ----------------------------------------------------------------------
# helpers / fixtures
# ----------------------------------------------------------------------


def write_source_tree(root: Path, files: dict[str, str]) -> Path:
    """把 `{相对路径: 内容}` 写成一棵源码树。"""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    return root


def prepare_version(client: TestClient, settings: Settings, version: str = VERSION) -> Path:
    """就位 fixture 源码 → 建索引 → 通过管理员接口注册版本；返回该版本的源码根。"""
    root = source_root(settings.data_dir, version)
    place_source_tree(FIXTURE_SOURCE, root)
    build_index(root, index_path(settings.data_dir, version), log=silent)

    registered = client.post("/v1/admin/versions", json={"version": version, "obfuscated": False})

    assert registered.status_code == 201, registered.text

    return root


def prepare_version_direct(
    settings: Settings, conn: sqlite3.Connection, version: str = VERSION
) -> Path:
    """同上，但直接写控制面（给没有 admin 会话的测试用）。"""
    root = source_root(settings.data_dir, version)
    place_source_tree(FIXTURE_SOURCE, root)
    index = index_path(settings.data_dir, version)
    result = build_index(root, index, log=silent)
    store.register_version(
        conn,
        version=version,
        files=result.files,
        tokens=result.tokens,
        obfuscated=False,
        has_summaries=False,
        source_size_bytes=index.stat().st_size,
        db_path=str(index),
    )
    conn.commit()

    return root


@pytest.fixture
def indexed(admin_client: TestClient, settings: Settings) -> Path:
    """数据根里已就位源码、已建索引、已注册版本的 26.3。"""
    return prepare_version(admin_client, settings)


def get_symbol(client: TestClient, name: str, **params: object) -> list[dict[str, Any]]:
    response = client.get("/v1/symbol", params={"version": VERSION, "name": name, **params})

    assert response.status_code == 200, response.text

    return cast("list[dict[str, Any]]", response.json()["matches"])


def retrieval_requests() -> list[tuple[str, dict[str, object]]]:
    """四个端点的最小可用请求。"""
    return [
        ("/v1/symbol", {"version": VERSION, "name": "Mob"}),
        ("/v1/references", {"version": VERSION, "symbol": "tick"}),
        ("/v1/outline", {"version": VERSION, "path": MOB_PATH}),
        ("/v1/read", {"version": VERSION, "path": MOB_PATH, "start": 1, "end": 2}),
    ]


def assert_retrieval_status(client: TestClient, status: int) -> None:
    for path, params in retrieval_requests():
        response = client.get(path, params=params)

        assert response.status_code == status, response.text


# ----------------------------------------------------------------------
# tree-sitter 解析
# ----------------------------------------------------------------------


def test_parse_java_extracts_inheritance_and_annotations() -> None:
    parsed = java.parse_java((FIXTURE_SOURCE / MOB_PATH).read_text(encoding="utf-8"), path=MOB_PATH)
    mob = next(item for item in parsed.classes if item.simple == "Mob")

    assert parsed.package == "net.minecraft.world"
    assert mob.fqn == "net.minecraft.world.Mob"
    assert mob.kind == "class"
    assert mob.superclass == "Entity"
    assert mob.interfaces == ["Trackable"]
    # 注解是裸名字：类 / 方法 / 字段各一处。
    assert mob.annotations == ["Deprecated"]

    track = next(method for method in mob.methods if method.name == "track")
    assert track.annotations == ["Override"]
    assert track.return_type == "void"
    assert track.signature == "public void track()"

    name = next(field for field in mob.fields if field.name == "name")
    assert name.annotations == ["Nullable"]
    assert name.type == "String"


def test_parse_java_uses_one_based_closed_line_ranges() -> None:
    """行号 1-based 闭区间：类的 `end_line` 是右花括号行（规格 §2）。"""
    source = (FIXTURE_SOURCE / MOB_PATH).read_text(encoding="utf-8")
    parsed = java.parse_java(source, path=MOB_PATH)
    mob = next(item for item in parsed.classes if item.simple == "Mob")
    lines = source.splitlines()

    assert lines[mob.start_line - 1].startswith("@Deprecated")
    assert lines[mob.end_line - 1] == "}"

    track = next(method for method in mob.methods if method.name == "track")
    assert lines[track.start_line - 1].strip().startswith("@Override")
    assert lines[track.end_line - 1] == "    }"


def test_parse_java_extracts_generics_and_inner_class() -> None:
    source = (FIXTURE_SOURCE / CONTAINER_PATH).read_text(encoding="utf-8")
    parsed = java.parse_java(source, path=CONTAINER_PATH)

    assert parsed.imports == ["java.util.List"]

    container = next(item for item in parsed.classes if item.simple == "Container")
    assert container.signature == "public class Container<T extends Entity>"

    constructor = next(method for method in container.methods if method.name == "Container")
    assert constructor.params == ["List<T>"]

    slot = next(item for item in parsed.classes if item.simple == "Slot")
    assert slot.fqn == "net.minecraft.world.Container.Slot"


def test_parse_java_extracts_references() -> None:
    """引用四类：调用 / 字段访问 / 建对象 / 类型（规格 §5）。"""
    source = (FIXTURE_SOURCE / WORLD_PATH).read_text(encoding="utf-8")
    parsed = java.parse_java(source, path=WORLD_PATH)
    world = next(item for item in parsed.classes if item.simple == "World")
    spawn = next(method for method in world.methods if method.name == "spawn")
    refs = {(ref.target_name, ref.kind): ref.line for ref in spawn.refs}
    lines = source.splitlines()

    assert refs[("tick", "call")] == 7
    assert refs[("health", "field")] == 8
    assert refs[("getName", "call")] == 9
    assert refs[("get", "call")] == 10
    # 参数类型（含泛型参数）也是 `type` 引用。
    assert {("Mob", "type"), ("Container", "type"), ("Entity", "type")} <= set(refs)
    assert lines[refs[("tick", "call")] - 1] == "        mob.tick();"


def test_parse_java_ignores_this_receiver_field_access() -> None:
    """`this.bar` 不算字段引用（规格 §5）。"""
    source = (FIXTURE_SOURCE / MOB_PATH).read_text(encoding="utf-8")
    parsed = java.parse_java(source, path=MOB_PATH)
    mob = next(item for item in parsed.classes if item.simple == "Mob")
    track = next(method for method in mob.methods if method.name == "track")

    assert [(ref.target_name, ref.kind) for ref in track.refs] == [("tick", "call")]


def test_parse_java_attributes_class_level_refs_to_clinit() -> None:
    """字段初始化器与类体级别引用归属到 `<clinit>`（规格 §5）。"""
    source = (FIXTURE_SOURCE / MOB_PATH).read_text(encoding="utf-8")
    parsed = java.parse_java(source, path=MOB_PATH)
    mob = next(item for item in parsed.classes if item.simple == "Mob")
    clinit = next(method for method in mob.methods if method.name == "<clinit>")
    refs = {(ref.target_name, ref.kind) for ref in clinit.refs}

    assert {("Entity", "type"), ("Trackable", "type"), ("String", "type")} <= refs


def test_parse_java_rejects_broken_source() -> None:
    with pytest.raises(java.JavaParseError):
        java.parse_java(BROKEN_JAVA, path="Broken.java")


def test_parse_imports_reads_import_declarations() -> None:
    source = "import java.util.List;\nimport static java.lang.Math.max;\nimport java.util.*;\n"

    assert java.parse_imports(source) == [
        "java.util.List",
        "static java.lang.Math.max",
        "java.util.*",
    ]


def _synthetic_java(index: int, method_count: int, statements: int) -> str:
    """生成一份形状可变的合成 Java；树的形状/大小差异才逼得出绑定层的内存问题。"""
    lines = [f"package synth.p{index % 11};", "import java.util.List;", f"public class V{index} {{"]

    for method in range(method_count):
        lines.append(f"  public int m{method}(int a) {{")

        for statement in range(statements):
            lines.append(f"    int x{statement} = {statement} * {index} + helper({statement});")

        lines.append("    return a + x0;")
        lines.append("  }")

    lines.append("  private int helper(int v) { return v + 1; }")
    lines.append("}")

    return "\n".join(lines) + "\n"


def test_parse_java_walks_many_synthetic_files_without_corruption() -> None:
    """回归网：大输入下的解析结果必须正确，进程必须活着。

    400 组随机（方法数、语句数）的合成源码逐个断言包名、FQN 与方法数，兜住两件事：
    解析结果正确性、大输入下的稳定性。与 tree-sitter 0.26.0 绑定层的堆损坏无关——
    后续实验已证伪「`Tree` 生命周期」那条归因（持有 `Tree` 崩、丢弃 `Tree` 不崩），
    根因在绑定层且无法在代码层规避，由 `pyproject.toml` 的 `<0.26` 钉版挡住（见 issue #22），
    本测试全绿不代表 0.26 没问题。
    """
    rng = random.Random(20261103)
    cases = [(index, rng.randint(1, 12), rng.randint(1, 300)) for index in range(400)]

    for position, (index, method_count, statements) in enumerate(cases):
        parsed = java.parse_java(
            _synthetic_java(index, method_count, statements), path=f"V{index}.java"
        )

        assert parsed.package == f"synth.p{index % 11}"
        assert [item.fqn for item in parsed.classes] == [f"synth.p{index % 11}.V{index}"]
        # 每个方法 + `helper`。
        assert len(parsed.classes[0].methods) == method_count + 1
        # `parse_imports` 是同一个遍历模式，一并覆盖。
        assert java.parse_imports("import java.util.List;\n") == ["java.util.List"]

        if position % 20 == 0:
            gc.collect()


# ----------------------------------------------------------------------
# 索引构建
# ----------------------------------------------------------------------


def test_build_index_writes_the_specified_schema(tmp_path: Path) -> None:
    root = write_source_tree(tmp_path / "src", {"net/A.java": OK_JAVA.format(name="A")})
    db = tmp_path / "db" / "26.3.sqlite"
    result = build_index(root, db, log=silent)

    assert result.files == 1
    assert result.parsed == 1
    assert result.skipped == 0
    assert db.is_file()

    connection = query.open_readonly(db)

    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(classes)")
        }
        row = connection.execute(
            "SELECT fqn, simple, kind, start_line, end_line FROM classes"
        ).fetchone()
    finally:
        connection.close()

    assert {"files", "classes", "methods", "fields", "refs"} <= tables
    assert {"idx_classes_simple", "idx_methods_name", "idx_methods_class", "idx_fields_name",
            "idx_refs_target"} <= indexes
    assert columns == {
        "id",
        "file_id",
        "fqn",
        "simple",
        "kind",
        "signature",
        "superclass",
        "interfaces",
        "annotations",
        "start_line",
        "end_line",
    }
    assert row["fqn"] == "net.A"
    assert row["kind"] == "class"


def test_build_index_counts_only_java_and_data_json(tmp_path: Path) -> None:
    """白名单只有 `*.java` 与 `data/**/*.json`；`assets/` 与其它扩展名天然排除（规格 §3）。"""
    root = write_source_tree(
        tmp_path / "src",
        {
            "net/A.java": OK_JAVA.format(name="A"),
            "data/tags/a.json": "{}\n",
            "data/tags/notes.txt": "not indexed\n",
            "assets/a.json": "{}\n",
            "README.md": "# not indexed\n",
        },
    )
    db = tmp_path / "db" / "26.3.sqlite"
    build_index(root, db, log=silent)

    connection = query.open_readonly(db)

    try:
        paths = {row["path"] for row in connection.execute("SELECT path FROM files")}
    finally:
        connection.close()

    assert paths == {"net/A.java", "data/tags/a.json"}


def test_build_index_skips_broken_files_with_warning(tmp_path: Path) -> None:
    """单个文件解析失败只跳过并记 `warn`，整体继续（规格 §4）。"""
    files = {f"net/Ok{index}.java": OK_JAVA.format(name=f"Ok{index}") for index in range(4)}
    files["net/Broken.java"] = BROKEN_JAVA
    root = write_source_tree(tmp_path / "src", files)
    messages: list[tuple[str, str]] = []

    result = build_index(
        root,
        tmp_path / "db" / "26.3.sqlite",
        log=lambda level, message: messages.append((level, message)),
    )

    assert result.parsed == 4
    assert result.skipped == 1
    assert any(level == "warn" and "net/Broken.java" in message for level, message in messages)


def test_build_index_fails_when_too_many_files_fail(tmp_path: Path) -> None:
    """失败率 > 20% 说明环境有问题，整体失败（规格 §4）。"""
    files = {f"net/Ok{index}.java": OK_JAVA.format(name=f"Ok{index}") for index in range(3)}
    files["net/Broken1.java"] = BROKEN_JAVA
    files["net/Broken2.java"] = BROKEN_JAVA
    root = write_source_tree(tmp_path / "src", files)
    db = tmp_path / "db" / "26.3.sqlite"

    with pytest.raises(BuildError, match="解析失败率过高"):
        build_index(root, db, log=silent)

    assert not db.exists()


def test_build_index_is_atomic_and_keeps_the_previous_index_on_failure(tmp_path: Path) -> None:
    root = write_source_tree(tmp_path / "src", {"net/A.java": OK_JAVA.format(name="A")})
    db = tmp_path / "db" / "26.3.sqlite"
    build_index(root, db, log=silent)

    assert list(db.parent.glob("*.tmp")) == []

    # 重建：内容被整体替换，而不是叠加。
    write_source_tree(root, {"net/B.java": OK_JAVA.format(name="B")})
    build_index(root, db, log=silent)

    connection = query.open_readonly(db)

    try:
        paths = {row["path"] for row in connection.execute("SELECT path FROM files")}
    finally:
        connection.close()

    assert paths == {"net/A.java", "net/B.java"}

    # 失败的重建不碰已有索引，也不留临时文件。
    write_source_tree(
        root,
        {
            "net/Broken1.java": BROKEN_JAVA,
            "net/Broken2.java": BROKEN_JAVA,
            "net/Broken3.java": BROKEN_JAVA,
        },
    )

    with pytest.raises(BuildError):
        build_index(root, db, log=silent)

    assert list(db.parent.glob("*.tmp")) == []

    connection = query.open_readonly(db)

    try:
        paths = {row["path"] for row in connection.execute("SELECT path FROM files")}
    finally:
        connection.close()

    assert paths == {"net/A.java", "net/B.java"}


def test_place_source_tree_copies_and_rejects_nesting(tmp_path: Path) -> None:
    source = write_source_tree(tmp_path / "outside", {"net/A.java": OK_JAVA.format(name="A")})
    dest = tmp_path / "data" / "src" / "26.3"

    place_source_tree(source, dest)
    assert (dest / "net" / "A.java").is_file()

    # 已经是目标目录：幂等，不报错。
    place_source_tree(dest, dest)
    assert (dest / "net" / "A.java").is_file()

    # 目标在源里面：拒绝复制，否则 `copytree` 会把自己复制进自己。
    with pytest.raises(BuildError):
        place_source_tree(tmp_path, tmp_path / "data")


# ----------------------------------------------------------------------
# 端点：鉴权与版本解析
# ----------------------------------------------------------------------


def test_retrieval_endpoints_require_login(client: TestClient) -> None:
    assert_retrieval_status(client, 401)


def test_retrieval_endpoints_allow_plain_user(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection
) -> None:
    """`user` 角色即可访问四个端点（规格 §10）。"""
    prepare_version_direct(settings, conn)
    assert_retrieval_status(user_client, 200)


def test_retrieval_404_when_version_unregistered(admin_client: TestClient) -> None:
    response = admin_client.get("/v1/symbol", params={"version": "9.9", "name": "Mob"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "未注册" in response.json()["detail"]


def test_retrieval_404_when_db_path_is_empty(
    admin_client: TestClient, settings: Settings
) -> None:
    """版本已注册但 `db_path` 为空（历史遗留行）→ 404（规格 §9）。

    任务产出与管理员注册现在都会写 `db_path`，只有旧库升级上来的行可能为空；
    这里直接插一行模拟它，确认 `_require_index` 仍能区分两种 404。
    """
    database = Database.for_data_dir(settings.data_dir)

    with database.connect() as connection, transaction(connection):
        connection.execute(
            "INSERT INTO versions (version, files, tokens, indexed_at, obfuscated,"
            " has_summaries, source_size_bytes, db_path)"
            " VALUES (?, 1, 1, '2026-11-03T00:00:00Z', 0, 0, 0, NULL)",
            (VERSION,),
        )

    response = admin_client.get("/v1/symbol", params={"version": VERSION, "name": "Mob"})

    assert response.status_code == 404
    assert "尚无索引文件" in response.json()["detail"]


def test_retrieval_404_when_index_file_is_gone(
    admin_client: TestClient, settings: Settings, indexed: Path
) -> None:
    index_path(settings.data_dir, VERSION).unlink()

    response = admin_client.get("/v1/symbol", params={"version": VERSION, "name": "Mob"})

    assert response.status_code == 404
    assert "索引文件不存在" in response.json()["detail"]


# ----------------------------------------------------------------------
# `/v1/symbol`
# ----------------------------------------------------------------------


def test_symbol_lookup_falls_back_through_the_levels(
    admin_client: TestClient, indexed: Path
) -> None:
    """四级依次尝试，先命中先返回（规格 §6）。"""
    exact_fqn = get_symbol(admin_client, "net.minecraft.world.Mob")
    assert [match["fqn"] for match in exact_fqn] == ["net.minecraft.world.Mob"]
    assert exact_fqn[0]["kind"] == "class"
    assert exact_fqn[0]["lines"] == [3, 18]

    simple = get_symbol(admin_client, "Mob")
    assert [match["fqn"] for match in simple] == ["net.minecraft.world.Mob"]

    suffix = get_symbol(admin_client, "world.Mob")
    assert [match["fqn"] for match in suffix] == ["net.minecraft.world.Mob"]

    fuzzy = get_symbol(admin_client, "Mobb")
    assert [match["fqn"] for match in fuzzy] == ["net.minecraft.world.Mob"]


def test_symbol_lookup_without_fuzzy_only_matches_exactly(
    admin_client: TestClient, indexed: Path
) -> None:
    """`fuzzy=false` 只做「全限定名 → 简单名」两级（规格 §6）。"""
    assert get_symbol(admin_client, "Mob", fuzzy=False)
    assert get_symbol(admin_client, "world.Mob", fuzzy=False) == []
    assert get_symbol(admin_client, "Mobb", fuzzy=False) == []


def test_symbol_lookup_filters_by_kind(admin_client: TestClient, indexed: Path) -> None:
    assert [match["kind"] for match in get_symbol(admin_client, "tick")] == ["method"]
    assert [
        match["kind"] for match in get_symbol(admin_client, "health", kind="field")
    ] == ["field"]
    assert get_symbol(admin_client, "health", kind="class") == []


def test_symbol_detail_exposes_inheritance_and_annotations(
    admin_client: TestClient, indexed: Path
) -> None:
    match = get_symbol(admin_client, "net.minecraft.world.Mob")[0]

    assert match["superclass"] == "Entity"
    assert match["interfaces"] == ["Trackable"]
    assert match["annotations"] == ["Deprecated"]
    assert match["path"] == MOB_PATH

    members = {(member["kind"], member["name"]) for member in match["members"]}
    assert ("method", "track") in members
    assert ("field", "health") in members
    # `<clinit>` 是伪方法，不出现在成员列表里。
    assert all(member["name"] != "<clinit>" for member in match["members"])


def test_symbol_lookup_returns_empty_list_for_a_miss(
    admin_client: TestClient, indexed: Path
) -> None:
    assert get_symbol(admin_client, "NoSuchSymbolAnywhere") == []


# ----------------------------------------------------------------------
# `/v1/references`
# ----------------------------------------------------------------------


def test_references_find_every_call_site_with_enclosing_method(
    admin_client: TestClient, indexed: Path
) -> None:
    """fixture 里 `tick` 有两个调用点，各自标出所在方法（验收：反查引用）。"""
    response = admin_client.get("/v1/references", params={"version": VERSION, "symbol": "tick"})

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["symbol"] == "tick"
    assert body["version"] == VERSION
    assert [
        (item["path"], item["lines"], item["kind"], item["enclosing"])
        for item in body["references"]
    ] == [
        (MOB_PATH, [11, 11], "call", "net.minecraft.world.Mob#track"),
        (WORLD_PATH, [7, 7], "call", "net.minecraft.world.World#spawn"),
    ]


def test_references_find_field_access(admin_client: TestClient, indexed: Path) -> None:
    response = admin_client.get("/v1/references", params={"version": VERSION, "symbol": "health"})

    assert response.status_code == 200, response.text
    references = response.json()["references"]

    assert [(item["path"], item["lines"], item["kind"]) for item in references] == [
        (WORLD_PATH, [8, 8], "field")
    ]


def test_references_filter_by_qualifying_class(admin_client: TestClient, indexed: Path) -> None:
    """`Class#member` 按引用方所在类过滤。"""
    response = admin_client.get(
        "/v1/references", params={"version": VERSION, "symbol": "World#tick"}
    )

    assert response.status_code == 200, response.text
    assert [item["path"] for item in response.json()["references"]] == [WORLD_PATH]


def test_references_respect_limit(admin_client: TestClient, indexed: Path) -> None:
    response = admin_client.get(
        "/v1/references", params={"version": VERSION, "symbol": "tick", "limit": 1}
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["references"]) == 1


# ----------------------------------------------------------------------
# `/v1/outline`
# ----------------------------------------------------------------------


def test_outline_reports_package_imports_and_lines(admin_client: TestClient, indexed: Path) -> None:
    response = admin_client.get(
        "/v1/outline", params={"version": VERSION, "path": CONTAINER_PATH}
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["path"] == CONTAINER_PATH
    assert body["package"] == "net.minecraft.world"
    assert body["imports"] == ["java.util.List"]

    classes = {item["fqn"]: item for item in body["classes"]}
    assert set(classes) == {
        "net.minecraft.world.Container",
        "net.minecraft.world.Container.Slot",
    }

    container = classes["net.minecraft.world.Container"]
    assert container["kind"] == "class"
    assert container["lines"] == [5, 23]
    methods = {method["name"]: method for method in container["methods"]}
    assert methods["get"]["lines"] == [12, 14]
    assert methods["get"]["signature"] == "public T get(int index)"
    # 不含方法体、不含字段列表、不含 `<clinit>` 伪方法。
    assert "items" not in methods
    assert "<clinit>" not in methods

    slot = classes["net.minecraft.world.Container.Slot"]
    assert slot["kind"] == "class"
    assert [method["name"] for method in slot["methods"]] == ["owner"]


def test_outline_404_when_source_tree_is_missing(
    admin_client: TestClient, settings: Settings, indexed: Path
) -> None:
    """§16：源码缺失一律 404，detail 给出期望路径。"""
    shutil.rmtree(source_root(settings.data_dir, VERSION))

    response = admin_client.get("/v1/outline", params={"version": VERSION, "path": MOB_PATH})

    assert response.status_code == 404
    assert str(source_root(settings.data_dir, VERSION)) in response.json()["detail"]


def test_outline_404_for_unknown_path(admin_client: TestClient, indexed: Path) -> None:
    response = admin_client.get(
        "/v1/outline", params={"version": VERSION, "path": "net/minecraft/world/Nope.java"}
    )

    assert response.status_code == 404


def test_outline_rejects_path_traversal(admin_client: TestClient, indexed: Path) -> None:
    response = admin_client.get(
        "/v1/outline", params={"version": VERSION, "path": "../../../../etc/passwd"}
    )

    assert response.status_code == 404


# ----------------------------------------------------------------------
# `/v1/read`
# ----------------------------------------------------------------------


def test_read_requires_start_and_end(admin_client: TestClient, indexed: Path) -> None:
    missing_both = admin_client.get("/v1/read", params={"version": VERSION, "path": MOB_PATH})
    missing_end = admin_client.get(
        "/v1/read", params={"version": VERSION, "path": MOB_PATH, "start": 1}
    )

    assert missing_both.status_code == 422
    assert missing_both.headers["content-type"].startswith("application/problem+json")
    assert missing_end.status_code == 422


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 5), (5, 4), (1, 999)],
)
def test_read_rejects_out_of_range_lines(
    admin_client: TestClient, indexed: Path, start: int, end: int
) -> None:
    """越界 422，detail 里带上文件总行数（规格 §8）。"""
    response = admin_client.get(
        "/v1/read",
        params={"version": VERSION, "path": MOB_PATH, "start": start, "end": end},
    )

    assert response.status_code == 422
    assert "18 行" in response.json()["detail"]


def test_read_returns_the_requested_slice_with_line_numbers(
    admin_client: TestClient, indexed: Path
) -> None:
    response = admin_client.get(
        "/v1/read", params={"version": VERSION, "path": MOB_PATH, "start": 9, "end": 13}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    source_lines = (FIXTURE_SOURCE / MOB_PATH).read_text(encoding="utf-8").splitlines()

    assert body["path"] == MOB_PATH
    assert body["version"] == VERSION
    assert body["start"] == 9
    assert body["end"] == 13
    assert body["total_lines"] == 18
    assert body["truncated"] is False
    assert body["content"].splitlines() == source_lines[8:13]


def test_read_truncates_from_the_tail_when_over_budget(
    admin_client: TestClient, settings: Settings, indexed: Path
) -> None:
    """超出 `max_tokens` 时按行从尾部截断，并把 `end` 下调到截断处（规格 §8）。"""
    relative = "net/minecraft/world/Big.java"
    big = source_root(settings.data_dir, VERSION) / relative
    big.write_text(
        "\n".join(f"// line {index:02d} " + "x" * 60 for index in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    params = {"version": VERSION, "path": relative, "start": 1, "end": 40}

    full = admin_client.get("/v1/read", params=params).json()
    trimmed = admin_client.get("/v1/read", params={**params, "max_tokens": 100}).json()

    assert full["truncated"] is False
    assert full["end"] == 40
    assert trimmed["truncated"] is True
    assert trimmed["end"] < 40
    assert trimmed["end"] >= trimmed["start"]
    assert full["content"].startswith(trimmed["content"])


def test_read_404_when_source_tree_is_missing(
    admin_client: TestClient, settings: Settings, indexed: Path
) -> None:
    shutil.rmtree(source_root(settings.data_dir, VERSION))

    response = admin_client.get(
        "/v1/read", params={"version": VERSION, "path": MOB_PATH, "start": 1, "end": 2}
    )

    assert response.status_code == 404
    assert str(source_root(settings.data_dir, VERSION)) in response.json()["detail"]


def test_read_can_slice_data_files(admin_client: TestClient, indexed: Path) -> None:
    """`data/` 下的 JSON 只进 files，但仍可被 `read` 切片。"""
    response = admin_client.get(
        "/v1/read",
        params={
            "version": VERSION,
            "path": "data/minecraft/tags/mob_category.json",
            "start": 1,
            "end": 3,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"].startswith("{\n  \"values\": [")
