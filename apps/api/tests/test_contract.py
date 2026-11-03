"""契约一致性：**运行时** `app.openapi()` 必须与 `docs/openapi.yaml` 对齐。

刻意不比对 yaml 文本，而是取 FastAPI 实际生成的 spec，防止「文档写了、代码没做」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from openapi_spec_validator import validate

from mc_librarian import __version__
from mc_librarian.config import find_repo_root
from mc_librarian.main import GLOBAL_SECURITY, SECURITY_SCHEMES, SERVERS, create_app


def _contract() -> dict[str, Any]:
    root = find_repo_root()
    assert root is not None, "测试应在仓库内运行（package.json + apps/api 哨兵可见）"
    with Path(root / "docs" / "openapi.yaml").open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


def _runtime() -> dict[str, Any]:
    return create_app().openapi()


def _operation(spec: dict[str, Any], path: str, method: str = "get") -> dict[str, Any]:
    operation: dict[str, Any] = spec["paths"][path][method]
    return operation


def _request_schema(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    schema: dict[str, Any] = _operation(spec, path, method)["requestBody"]["content"][
        "application/json"
    ]["schema"]
    return _deref(spec, schema)


def _deref(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """跟随 `$ref`，否则 FastAPI 生成的是 `#/components/schemas/<Model>`。"""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema

    *_, name = ref.split("/")

    resolved: dict[str, Any] = spec["components"]["schemas"][name]
    return resolved


def _non_null(schema: dict[str, Any]) -> dict[str, Any]:
    """可空字段在 FastAPI 里是 `anyOf: [..., {type: null}]`，取出非 null 的那一支。"""
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            for branch in schema[keyword]:
                if branch.get("type") != "null":
                    resolved: dict[str, Any] = branch
                    return resolved

    return schema


def _response_schema(
    spec: dict[str, Any], path: str, method: str = "get", status: str = "200"
) -> dict[str, Any]:
    schema: dict[str, Any] = _operation(spec, path, method)["responses"][status]["content"][
        "application/json"
    ]["schema"]
    return _deref(spec, schema)


def test_contract_spec_is_valid() -> None:
    """契约本身必须过 OpenAPI 3.1 校验（openapi-spec-validator）。"""
    validate(_contract())


def test_runtime_spec_is_valid() -> None:
    """运行时 spec 也必须过校验，避免补 metadata 时写出非法结构。"""
    validate(_runtime())


def test_versions_match() -> None:
    assert __version__ == _contract()["info"]["version"]


def test_description_matches() -> None:
    contract = _contract()["info"]

    assert _runtime()["info"]["description"] == contract["description"]
    assert _runtime()["info"]["description"].strip(), "info.description 不能为空"


def test_tags_match() -> None:
    contract = _contract()["tags"]
    runtime = _runtime()["tags"]

    assert [tag["name"] for tag in runtime] == [tag["name"] for tag in contract]


def test_servers_match() -> None:
    assert _runtime()["servers"] == _contract()["servers"] == SERVERS


def test_global_security_matches() -> None:
    contract = _contract()

    assert _runtime()["security"] == contract["security"] == GLOBAL_SECURITY


def test_security_schemes_match() -> None:
    contract = _contract()["components"]["securitySchemes"]

    assert set(_runtime()["components"]["securitySchemes"]) == set(contract)
    assert set(SECURITY_SCHEMES) == set(contract)


def test_runtime_paths_are_declared_in_contract() -> None:
    """运行时已实现的 path 必须全部在契约里（其余端点由后续 ticket 补齐）。"""
    runtime_paths = set(_runtime()["paths"])
    contract_paths = set(_contract()["paths"])

    assert runtime_paths, "运行时 OpenAPI 不能没有任何 path"
    undeclared = runtime_paths - contract_paths

    assert runtime_paths <= contract_paths, f"未在契约中声明的 path：{undeclared}"
    assert "/v1/health" in runtime_paths


def test_health_response_schema_matches() -> None:
    runtime = _response_schema(_runtime(), "/v1/health")
    contract = _response_schema(_contract(), "/v1/health")

    assert runtime["required"] == contract["required"] == ["status", "version"]
    assert set(runtime["properties"]) == set(contract["properties"])
    assert runtime["properties"]["status"]["const"] == contract["properties"]["status"]["const"]


def test_health_operation_is_public() -> None:
    runtime = _operation(_runtime(), "/v1/health")
    contract = _operation(_contract(), "/v1/health")

    assert runtime["operationId"] == contract["operationId"] == "getHealth"
    assert runtime["security"] == contract["security"] == []


def test_auth_paths_are_implemented() -> None:
    """issue #3 把 setup 与三条 auth 端点从契约落到了实现。"""
    runtime_paths = set(_runtime()["paths"])

    assert {
        "/v1/setup",
        "/v1/auth/login",
        "/v1/auth/logout",
        "/v1/auth/me",
    } <= runtime_paths


@pytest.mark.parametrize(
    ("path", "method", "operation_id"),
    [
        ("/v1/setup", "get", "getSetupStatus"),
        ("/v1/setup", "post", "createFirstAdmin"),
        ("/v1/auth/login", "post", "login"),
        ("/v1/auth/logout", "post", "logout"),
        ("/v1/auth/me", "get", "getMe"),
    ],
)
def test_auth_operation_ids_match(path: str, method: str, operation_id: str) -> None:
    runtime = _operation(_runtime(), path, method)
    contract = _operation(_contract(), path, method)

    assert runtime["operationId"] == contract["operationId"] == operation_id
    assert runtime["tags"] == contract["tags"] == ["auth"]


def test_public_operations_declare_empty_security() -> None:
    """setup / login 是公开端点（前端未登录时也要能调）。"""
    for path, method in (
        ("/v1/setup", "get"),
        ("/v1/setup", "post"),
        ("/v1/auth/login", "post"),
    ):
        assert _operation(_runtime(), path, method)["security"] == []
        assert _operation(_contract(), path, method)["security"] == []


def test_session_operations_require_auth() -> None:
    """me / logout 不覆盖 security，因此继承全局 cookieAuth / bearerAuth。"""
    for path, method in (("/v1/auth/me", "get"), ("/v1/auth/logout", "post")):
        assert "security" not in _operation(_runtime(), path, method)
        assert "security" not in _operation(_contract(), path, method)


def test_token_paths_are_implemented() -> None:
    """issue #10 把三条 tokens 端点从契约落到了实现。"""
    runtime_paths = set(_runtime()["paths"])

    assert {"/v1/auth/tokens", "/v1/auth/tokens/{tokenId}"} <= runtime_paths


@pytest.mark.parametrize(
    ("path", "method", "operation_id"),
    [
        ("/v1/auth/tokens", "get", "listTokens"),
        ("/v1/auth/tokens", "post", "createToken"),
        ("/v1/auth/tokens/{tokenId}", "delete", "revokeToken"),
    ],
)
def test_token_operation_ids_match(path: str, method: str, operation_id: str) -> None:
    runtime = _operation(_runtime(), path, method)
    contract = _operation(_contract(), path, method)

    assert runtime["operationId"] == contract["operationId"] == operation_id
    assert runtime["tags"] == contract["tags"] == ["auth"]
    # 不覆盖 security：继承全局 cookieAuth / bearerAuth，PAT 与 cookie 都能用。
    assert "security" not in runtime


def test_setup_status_schema_matches() -> None:
    runtime = _response_schema(_runtime(), "/v1/setup")
    contract = _response_schema(_contract(), "/v1/setup")

    assert runtime["required"] == contract["required"] == ["required"]
    assert set(runtime["properties"]) == set(contract["properties"]) == {"required"}


def test_user_schema_matches() -> None:
    """setup 的 201、login 的 200、/auth/me 的 200 都是同一个 `User`。"""
    runtime = _response_schema(_runtime(), "/v1/setup", "post", "201")
    contract = _response_schema(_contract(), "/v1/setup", "post", "201")

    assert runtime["required"] == contract["required"] == ["id", "username", "role", "enabled"]
    assert set(runtime["properties"]) == set(contract["properties"])
    assert "password_hash" not in runtime["properties"]

    for path, method, status in (
        ("/v1/auth/login", "post", "200"),
        ("/v1/auth/me", "get", "200"),
    ):
        assert _response_schema(_runtime(), path, method, status) == runtime
        assert _response_schema(_contract(), path, method, status) == contract


def test_setup_and_login_request_schemas_match() -> None:
    for path, method in (("/v1/setup", "post"), ("/v1/auth/login", "post")):
        runtime = _request_schema(_runtime(), path, method)
        contract = _request_schema(_contract(), path, method)

        assert runtime["required"] == contract["required"] == ["username", "password"]
        assert set(runtime["properties"]) == set(contract["properties"])

        password = runtime["properties"]["password"]
        assert password["minLength"] == contract["properties"]["password"]["minLength"] == 8
        assert password["maxLength"] == contract["properties"]["password"]["maxLength"] == 128


def test_setup_completed_error_is_declared() -> None:
    """已初始化必须回 410，且契约里写明。"""
    runtime = _operation(_runtime(), "/v1/setup", "post")
    contract = _operation(_contract(), "/v1/setup", "post")

    assert "410" in runtime["responses"]
    assert contract["responses"]["410"]["$ref"].endswith("/SetupCompleted")


def test_unauthorized_error_is_declared_for_session_endpoints() -> None:
    for path, method in (
        ("/v1/auth/login", "post"),
        ("/v1/auth/logout", "post"),
        ("/v1/auth/me", "get"),
    ):
        assert "401" in _operation(_runtime(), path, method)["responses"]
        assert "401" in _operation(_contract(), path, method)["responses"]


def test_problem_schema_is_present_in_both_specs() -> None:
    """错误统一 `application/problem+json`，运行时要能 deref 到 `Problem`。"""
    for spec in (_runtime(), _contract()):
        problem = spec["components"]["schemas"]["Problem"]

        assert problem["type"] == "object"
        assert set(problem["properties"]) == {"type", "title", "status", "detail", "instance"}
        assert {"title", "status"} <= set(problem["required"])


def test_user_management_paths_are_implemented() -> None:
    """issue #9 把 users / audit / invites 从契约落到了实现。"""
    runtime_paths = set(_runtime()["paths"])

    assert {
        "/v1/admin/users",
        "/v1/admin/users/{userId}",
        "/v1/admin/audit",
        "/v1/invites/{token}",
        "/v1/invites/{token}/accept",
    } <= runtime_paths


@pytest.mark.parametrize(
    ("path", "method", "operation_id", "tag"),
    [
        ("/v1/admin/users", "get", "listUsers", "admin"),
        ("/v1/admin/users", "post", "createUser", "admin"),
        ("/v1/admin/users/{userId}", "patch", "patchUser", "admin"),
        ("/v1/admin/audit", "get", "listAudit", "admin"),
        ("/v1/invites/{token}", "get", "getInvite", "auth"),
        ("/v1/invites/{token}/accept", "post", "acceptInvite", "auth"),
    ],
)
def test_user_management_operation_ids_match(
    path: str, method: str, operation_id: str, tag: str
) -> None:
    runtime = _operation(_runtime(), path, method)
    contract = _operation(_contract(), path, method)

    assert runtime["operationId"] == contract["operationId"] == operation_id
    assert runtime["tags"] == contract["tags"] == [tag]


def test_invite_operations_are_public() -> None:
    """被邀请人还没有账号，邀请校验与接受必须无需会话。"""
    for path, method in (
        ("/v1/invites/{token}", "get"),
        ("/v1/invites/{token}/accept", "post"),
    ):
        assert _operation(_runtime(), path, method)["security"] == []
        assert _operation(_contract(), path, method)["security"] == []


def test_admin_operations_require_auth() -> None:
    """管理端点不覆盖 security，因此继承全局 cookieAuth / bearerAuth。"""
    for path, method in (
        ("/v1/admin/users", "get"),
        ("/v1/admin/users", "post"),
        ("/v1/admin/users/{userId}", "patch"),
        ("/v1/admin/audit", "get"),
    ):
        assert "security" not in _operation(_runtime(), path, method)
        assert "security" not in _operation(_contract(), path, method)


def test_create_user_request_has_no_password() -> None:
    """管理员不需要也不应该设置别人的密码：创建请求只有用户名与角色。"""
    runtime = _request_schema(_runtime(), "/v1/admin/users", "post")
    contract = _request_schema(_contract(), "/v1/admin/users", "post")

    assert set(runtime["properties"]) == set(contract["properties"]) == {"username", "role"}
    assert runtime["required"] == contract["required"] == ["username"]
    assert "password" not in runtime["properties"]


def test_create_user_response_returns_invite_url() -> None:
    runtime = _response_schema(_runtime(), "/v1/admin/users", "post", "201")
    contract = _response_schema(_contract(), "/v1/admin/users", "post", "201")

    assert set(runtime["properties"]) == set(contract["properties"]) == {"user", "invite_url"}
    assert runtime["required"] == contract["required"] == ["user", "invite_url"]


def test_patch_user_declares_conflict_and_not_found() -> None:
    for spec in (_runtime(), _contract()):
        responses = _operation(spec, "/v1/admin/users/{userId}", "patch")["responses"]

        assert "404" in responses
        assert "409" in responses


def test_invite_status_schema_matches() -> None:
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/invites/{token}")

        assert schema["required"] == ["valid"]
        assert set(schema["properties"]) == {"valid", "username"}


def test_accept_invite_request_schema_matches() -> None:
    runtime = _request_schema(_runtime(), "/v1/invites/{token}/accept", "post")
    contract = _request_schema(_contract(), "/v1/invites/{token}/accept", "post")

    assert runtime["required"] == contract["required"] == ["password"]
    assert runtime["properties"]["password"]["minLength"] == 8
    assert contract["properties"]["password"]["minLength"] == 8
    assert runtime["properties"]["password"]["maxLength"] == 128
    assert contract["properties"]["password"]["maxLength"] == 128


def test_audit_entry_schema_matches() -> None:
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/admin/audit")
        entry = _deref(spec, schema["properties"]["entries"]["items"])

        assert entry["required"] == ["id", "actor", "action", "created_at"]
        assert set(entry["properties"]) == {
            "id",
            "actor",
            "action",
            "target",
            "detail",
            "created_at",
            "ip",
        }
        assert entry["properties"]["detail"]["type"] == "object"
        assert entry["properties"]["detail"]["additionalProperties"] is True


def test_job_paths_are_implemented() -> None:
    """issue #4 把任务框架与版本注册从契约落到了实现。"""
    runtime_paths = set(_runtime()["paths"])

    assert {
        "/v1/versions",
        "/v1/admin/versions",
        "/v1/admin/jobs",
        "/v1/admin/jobs/{jobId}",
        "/v1/admin/jobs/{jobId}/logs",
        "/v1/admin/jobs/{jobId}/cancel",
        "/v1/admin/jobs/{jobId}/retry",
    } <= runtime_paths


def test_settings_path_is_implemented() -> None:
    """`GET /v1/admin/settings` 已实现（只读、配置派生）；`PUT` 仍待 #11。"""
    runtime = _operation(_runtime(), "/v1/admin/settings", "get")
    contract = _operation(_contract(), "/v1/admin/settings", "get")

    assert runtime["operationId"] == contract["operationId"] == "getSettings"
    assert "403" in runtime["responses"]
    assert "403" in contract["responses"]

    # 索引构建闸门 `settings.index.enabled` 两侧都要有（运行时从配置派生）。
    for spec in (_runtime(), _contract()):
        index = _deref(spec, spec["components"]["schemas"]["Settings"]["properties"]["index"])

        assert "enabled" in index["properties"]


@pytest.mark.parametrize(
    ("path", "method", "operation_id"),
    [
        ("/v1/versions", "get", "listVersions"),
        ("/v1/admin/versions", "post", "registerVersion"),
        ("/v1/admin/jobs", "get", "listJobs"),
        ("/v1/admin/jobs", "post", "createJob"),
        ("/v1/admin/jobs/{jobId}", "get", "getJob"),
        ("/v1/admin/jobs/{jobId}/logs", "get", "getJobLogs"),
        ("/v1/admin/jobs/{jobId}/cancel", "post", "cancelJob"),
        ("/v1/admin/jobs/{jobId}/retry", "post", "retryJob"),
    ],
)
def test_job_operation_ids_match(path: str, method: str, operation_id: str) -> None:
    runtime = _operation(_runtime(), path, method)
    contract = _operation(_contract(), path, method)

    assert runtime["operationId"] == contract["operationId"] == operation_id


def test_admin_operations_declare_forbidden() -> None:
    """`/v1/admin/*` 对已登录的非 admin 回 403，契约里写明。"""
    for path, method in (
        ("/v1/admin/versions", "post"),
        ("/v1/admin/jobs", "get"),
        ("/v1/admin/jobs", "post"),
        ("/v1/admin/jobs/{jobId}", "get"),
        ("/v1/admin/jobs/{jobId}/logs", "get"),
        ("/v1/admin/jobs/{jobId}/cancel", "post"),
        ("/v1/admin/jobs/{jobId}/retry", "post"),
    ):
        for spec in (_runtime(), _contract()):
            assert "403" in _operation(spec, path, method)["responses"]

        # 不覆盖 security：继承全局 cookieAuth / bearerAuth。
        assert "security" not in _operation(_runtime(), path, method)


def test_job_schema_matches() -> None:
    runtime = _response_schema(_runtime(), "/v1/admin/jobs/{jobId}")
    contract = _response_schema(_contract(), "/v1/admin/jobs/{jobId}")

    assert runtime["required"] == contract["required"] == ["id", "kind", "status", "created_at"]
    assert set(runtime["properties"]) == set(contract["properties"])

    for field in ("status", "stage"):
        runtime_field = _non_null(runtime["properties"][field])
        contract_field = _non_null(contract["properties"][field])

        assert runtime_field["enum"] == contract_field["enum"]


def test_job_create_schema_matches() -> None:
    runtime = _request_schema(_runtime(), "/v1/admin/jobs", "post")
    contract = _request_schema(_contract(), "/v1/admin/jobs", "post")

    assert runtime["required"] == contract["required"] == ["kind", "version"]
    assert set(runtime["properties"]) == set(contract["properties"])

    runtime_options = _deref(_runtime(), _non_null(runtime["properties"]["options"]))
    contract_options = _non_null(contract["properties"]["options"])

    assert set(runtime_options["properties"]) == set(contract_options["properties"])
    assert "source_dir" in runtime_options["properties"]
    # `options` 可省略；给了 options 但 `source_dir` 为空由接口层 422，故字段本身可选。
    assert runtime_options.get("required", []) == contract_options.get("required", []) == []


def test_job_log_and_version_schemas_match() -> None:
    runtime_logs = _response_schema(_runtime(), "/v1/admin/jobs/{jobId}/logs")
    contract_logs = _response_schema(_contract(), "/v1/admin/jobs/{jobId}/logs")

    runtime_line = _deref(_runtime(), runtime_logs["properties"]["lines"]["items"])
    contract_line = _deref(_contract(), contract_logs["properties"]["lines"]["items"])

    assert runtime_line["required"] == contract_line["required"] == [
        "id",
        "ts",
        "level",
        "message",
    ]
    assert set(runtime_line["properties"]) == set(contract_line["properties"])

    runtime_version = _response_schema(_runtime(), "/v1/admin/versions", "post", "201")
    contract_version = _response_schema(_contract(), "/v1/admin/versions", "post", "201")

    assert runtime_version["required"] == contract_version["required"] == [
        "version",
        "files",
        "tokens",
        "indexed_at",
    ]
    assert set(runtime_version["properties"]) == set(contract_version["properties"])

    runtime_list = _response_schema(_runtime(), "/v1/versions")
    contract_list = _response_schema(_contract(), "/v1/versions")

    assert set(runtime_list["properties"]) == set(contract_list["properties"]) == {"versions"}


# ----------------------------------------------------------------------
# issue #5：符号索引与精确检索（symbol / references / outline / read）
# ----------------------------------------------------------------------

RETRIEVAL_OPERATIONS: Final = (
    ("/v1/symbol", "get"),
    ("/v1/references", "get"),
    ("/v1/outline", "get"),
    ("/v1/read", "get"),
)


def test_retrieval_paths_are_implemented() -> None:
    """issue #5 把四个检索/阅读端点从契约落到了实现。"""
    runtime_paths = set(_runtime()["paths"])

    assert {"/v1/symbol", "/v1/references", "/v1/outline", "/v1/read"} <= runtime_paths


@pytest.mark.parametrize(
    ("path", "method", "operation_id", "tag"),
    [
        ("/v1/symbol", "get", "getSymbol", "retrieve"),
        ("/v1/references", "get", "getReferences", "retrieve"),
        ("/v1/outline", "get", "getOutline", "read"),
        ("/v1/read", "get", "readFile", "read"),
    ],
)
def test_retrieval_operation_ids_match(
    path: str, method: str, operation_id: str, tag: str
) -> None:
    runtime = _operation(_runtime(), path, method)
    contract = _operation(_contract(), path, method)

    assert runtime["operationId"] == contract["operationId"] == operation_id
    assert runtime["tags"] == contract["tags"] == [tag]


def test_retrieval_operations_require_login() -> None:
    """四个端点都只要求登录（`user` 即可），不覆盖 security，继承全局 cookieAuth / bearerAuth。"""
    for path, method in RETRIEVAL_OPERATIONS:
        for spec in (_runtime(), _contract()):
            assert "security" not in _operation(spec, path, method)


def test_retrieval_error_responses_are_declared() -> None:
    """未认证 / 版本或文件不存在 / 查询参数不合法都是 problem+json（规格 §8/§9/§16）。"""
    contract = _contract()
    runtime = _runtime()

    for path, method in RETRIEVAL_OPERATIONS:
        contract_responses = _operation(contract, path, method)["responses"]
        runtime_responses = _operation(runtime, path, method)["responses"]

        assert contract_responses["401"]["$ref"].endswith("/Unauthorized")
        assert contract_responses["404"]["$ref"].endswith("/NotFound")
        assert contract_responses["422"]["$ref"].endswith("/BadRequest")

        for status in ("401", "404", "422"):
            assert "application/problem+json" in runtime_responses[status]["content"]


def test_symbol_matches_schema_matches() -> None:
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/symbol")
        detail = _deref(spec, schema["properties"]["matches"]["items"])

        assert schema["required"] == ["matches"]
        assert detail["required"] == ["fqn", "kind", "path", "lines"]
        assert set(detail["properties"]) == {
            "fqn",
            "kind",
            "signature",
            "path",
            "lines",
            "superclass",
            "interfaces",
            "annotations",
            "summary",
            "members",
        }


def test_reference_response_schema_matches() -> None:
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/references")
        reference = _deref(spec, schema["properties"]["references"]["items"])

        assert schema["required"] == ["symbol", "version", "references"]
        assert reference["required"] == ["path", "lines"]


def test_outline_schema_matches() -> None:
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/outline")

        assert schema["required"] == ["path", "package", "classes"]
        assert set(schema["properties"]) == {"path", "package", "imports", "classes"}


def test_file_slice_schema_matches() -> None:
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/read")

        assert schema["required"] == [
            "path",
            "version",
            "start",
            "end",
            "content",
            "truncated",
        ]
        assert set(schema["properties"]) == {
            "path",
            "version",
            "start",
            "end",
            "total_lines",
            "content",
            "truncated",
        }


# ----------------------------------------------------------------------
# issue #6：混合检索（`POST /v1/search`）
# ----------------------------------------------------------------------


def test_search_path_is_implemented() -> None:
    """issue #6 把 `/v1/search` 从契约落到了实现。"""
    runtime = _operation(_runtime(), "/v1/search", "post")
    contract = _operation(_contract(), "/v1/search", "post")

    assert runtime["operationId"] == contract["operationId"] == "search"
    assert runtime["tags"] == contract["tags"] == ["retrieve"]
    # 不覆盖 security：继承全局 cookieAuth / bearerAuth。
    assert "security" not in runtime


def test_search_request_schema_matches() -> None:
    runtime = _request_schema(_runtime(), "/v1/search", "post")
    contract = _request_schema(_contract(), "/v1/search", "post")

    assert runtime["required"] == contract["required"] == ["query", "version"]
    assert set(runtime["properties"]) == set(contract["properties"]) == {
        "query",
        "version",
        "path_prefix",
        "kind",
        "detail",
        "limit",
        "max_tokens",
    }
    assert runtime["properties"]["detail"]["enum"] == ["locate", "snippet"]
    assert contract["properties"]["detail"]["enum"] == ["locate", "snippet"]
    assert runtime["properties"]["limit"]["default"] == 10
    assert contract["properties"]["limit"]["default"] == 10
    assert runtime["properties"]["max_tokens"]["default"] == 4000
    assert contract["properties"]["max_tokens"]["default"] == 4000


def test_search_response_schema_matches() -> None:
    runtime = _response_schema(_runtime(), "/v1/search", "post")
    contract = _response_schema(_contract(), "/v1/search", "post")

    assert runtime["required"] == contract["required"] == [
        "query",
        "version",
        "hits",
        "total",
        "took_ms",
    ]
    assert set(runtime["properties"]) == set(contract["properties"])

    for spec in (_runtime(), _contract()):
        hit = _deref(
            spec, _response_schema(spec, "/v1/search", "post")["properties"]["hits"]["items"]
        )

        assert hit["required"] == ["id", "type", "version", "path", "lines", "score", "why"]
        assert set(hit["properties"]) == {
            "id",
            "type",
            "version",
            "path",
            "symbol",
            "lines",
            "score",
            "why",
            "summary",
            "snippet",
            "tokens",
        }
        assert hit["properties"]["why"]["items"]["enum"] == [
            "summary",
            "symbol",
            "literal",
            "reference",
        ]


def test_search_error_responses_are_declared() -> None:
    """未认证 / 版本未注册 / 请求体不合法都是 problem+json（规格 §8）。"""
    for spec in (_runtime(), _contract()):
        responses = _operation(spec, "/v1/search", "post")["responses"]

        assert {"401", "404", "422"} <= set(responses)

    contract = _contract()["paths"]["/v1/search"]["post"]["responses"]

    assert contract["401"]["$ref"].endswith("/Unauthorized")
    assert contract["404"]["$ref"].endswith("/NotFound")
    assert contract["422"]["$ref"].endswith("/BadRequest")


# ----------------------------------------------------------------------
# issue #8：中文问答（`POST /v1/ask`）
# ----------------------------------------------------------------------


def test_ask_path_is_implemented() -> None:
    """issue #8 把 `/v1/ask` 从契约落到了实现。"""
    runtime = _operation(_runtime(), "/v1/ask", "post")
    contract = _operation(_contract(), "/v1/ask", "post")

    assert runtime["operationId"] == contract["operationId"] == "ask"
    assert runtime["tags"] == contract["tags"] == ["answer"]
    # 不覆盖 security：继承全局 cookieAuth / bearerAuth。
    assert "security" not in runtime


def test_ask_declares_both_response_content_types() -> None:
    """默认 SSE、`Accept: application/json` 一次性返回，两个 content type 都要声明。"""
    for spec in (_runtime(), _contract()):
        content = _operation(spec, "/v1/ask", "post")["responses"]["200"]["content"]

        assert set(content) == {"text/event-stream", "application/json"}
        assert content["text/event-stream"]["schema"]["$ref"].endswith("/AskEvent")
        assert content["application/json"]["schema"]["$ref"].endswith("/AskResponse")


def _error_response(spec: dict[str, Any], status: str) -> dict[str, Any]:
    """`/v1/ask` 的某个错误响应；契约里是 `$ref`，运行时是内联的 problem+json。"""
    response: dict[str, Any] = _operation(spec, "/v1/ask", "post")["responses"][status]
    ref = response.get("$ref")

    if isinstance(ref, str):
        *_, name = ref.split("/")
        resolved: dict[str, Any] = spec["components"]["responses"][name]
        return resolved

    return response


def test_ask_error_responses_are_declared() -> None:
    """未认证 / 版本未注册 / 请求体不合法 / 开关或并发 / 未配置模型都是 problem+json。"""
    for spec in (_runtime(), _contract()):
        responses = _operation(spec, "/v1/ask", "post")["responses"]

        assert {"200", "401", "404", "422", "429", "503"} <= set(responses)

        for status in ("401", "404", "422", "429", "503"):
            assert "application/problem+json" in _error_response(spec, status)["content"]


def _ask_event_branch(spec: dict[str, Any], kind: str) -> dict[str, Any]:
    """`AskEvent` 里 `type == kind` 的那一支（运行时的分支是 `$ref`）。"""
    schema = spec["components"]["schemas"]["AskEvent"]
    branches = [_deref(spec, branch) for branch in schema["oneOf"]]

    return next(
        branch for branch in branches if branch["properties"]["type"]["const"] == kind
    )


def _enum_values(spec: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """枚举取值；运行时可能内联（pydantic 对 Literal 不建组件），契约里是 `$ref`。"""
    ref = schema.get("$ref")

    if isinstance(ref, str):
        *_, name = ref.split("/")
        schema = spec["components"]["schemas"][name]

    values: list[str] = schema["enum"]

    return values


def test_ask_done_event_carries_a_reason() -> None:
    """`done` 必须带 `reason`（正常 / `max_steps` / 降级），契约与运行时都要有。"""
    for spec in (_runtime(), _contract()):
        done = _ask_event_branch(spec, "done")

        assert "reason" in done["required"]
        assert _enum_values(spec, done["properties"]["reason"]) == [
            "stop",
            "max_steps",
            "degraded",
        ]


def test_ask_step_event_carries_ok_and_hits() -> None:
    """`step` 有两种形态：调用前带 `args`，返回后带 `ok`/`hits`（规格 §4）。"""
    for spec in (_runtime(), _contract()):
        step = _ask_event_branch(spec, "step")

        assert set(step["properties"]) == {"type", "tool", "args", "ok", "hits"}
        assert step["required"] == ["type", "tool"]


def test_ask_request_schema_matches() -> None:
    runtime = _request_schema(_runtime(), "/v1/ask", "post")
    contract = _request_schema(_contract(), "/v1/ask", "post")

    assert runtime["required"] == contract["required"] == ["question", "version"]
    assert set(runtime["properties"]) == set(contract["properties"])


# ----------------------------------------------------------------------
# issue #12：用量聚合与告警（`GET /v1/admin/usage`）
# ----------------------------------------------------------------------


def test_usage_path_is_implemented() -> None:
    """issue #12 把 `/v1/admin/usage` 从契约落到了实现。"""
    runtime = _operation(_runtime(), "/v1/admin/usage", "get")
    contract = _operation(_contract(), "/v1/admin/usage", "get")

    assert runtime["operationId"] == contract["operationId"] == "getUsage"
    assert runtime["tags"] == contract["tags"] == ["admin"]
    # 不覆盖 security：继承全局 cookieAuth / bearerAuth。
    assert "security" not in runtime


def test_usage_query_parameters_match() -> None:
    runtime = _operation(_runtime(), "/v1/admin/usage", "get")["parameters"]
    contract = _operation(_contract(), "/v1/admin/usage", "get")["parameters"]

    assert [param["name"] for param in runtime] == [param["name"] for param in contract] == [
        "from",
        "to",
        "user_id",
        "group_by",
    ]

    for spec in (_runtime(), _contract()):
        parameters = {
            param["name"]: param
            for param in _operation(spec, "/v1/admin/usage", "get")["parameters"]
        }

        assert parameters["group_by"]["schema"]["enum"] == ["day", "user", "version", "model"]
        assert parameters["group_by"]["schema"]["default"] == "day"


def test_usage_report_schema_matches() -> None:
    """`totals` 是区间合计、`buckets` 是分组明细；`requests`/`cost_configured` 是页面要的标量。"""
    for spec in (_runtime(), _contract()):
        schema = _response_schema(spec, "/v1/admin/usage")

        assert schema["required"] == [
            "from",
            "to",
            "requests",
            "cost_configured",
            "totals",
            "buckets",
        ]
        assert set(schema["properties"]) == {
            "from",
            "to",
            "requests",
            "cost_configured",
            "totals",
            "buckets",
        }
        assert schema["properties"]["from"]["format"] == "date"
        assert schema["properties"]["to"]["format"] == "date"
        assert schema["properties"]["totals"]["$ref"].endswith("/Usage")

        bucket = _deref(spec, schema["properties"]["buckets"]["items"])

        assert bucket["required"] == [
            "key",
            "requests",
            "prompt_tokens",
            "completion_tokens",
            "cost_usd",
        ]


def test_usage_declares_problem_responses() -> None:
    """未认证 / 非 admin / 查询参数不合法都是 problem+json。"""
    for spec in (_runtime(), _contract()):
        responses = _operation(spec, "/v1/admin/usage", "get")["responses"]

        assert {"200", "401", "403", "422"} <= set(responses)

        for status in ("401", "403", "422"):
            assert "application/problem+json" in _resolved_response(
                spec, "/v1/admin/usage", "get", status
            )["content"]


def _resolved_response(
    spec: dict[str, Any], path: str, method: str, status: str
) -> dict[str, Any]:
    """某个响应；契约里是 `components.responses` 的 `$ref`，运行时是内联的 problem+json。"""
    response: dict[str, Any] = _operation(spec, path, method)["responses"][status]
    ref = response.get("$ref")

    if isinstance(ref, str):
        *_, name = ref.split("/")
        resolved: dict[str, Any] = spec["components"]["responses"][name]
        return resolved

    return response
