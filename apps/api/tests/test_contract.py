"""契约一致性：**运行时** `app.openapi()` 必须与 `docs/openapi.yaml` 对齐。

刻意不比对 yaml 文本，而是取 FastAPI 实际生成的 spec，防止「文档写了、代码没做」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
