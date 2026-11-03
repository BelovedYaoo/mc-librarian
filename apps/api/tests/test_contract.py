"""契约一致性：**运行时** `app.openapi()` 必须与 `docs/openapi.yaml` 对齐。

刻意不比对 yaml 文本，而是取 FastAPI 实际生成的 spec，防止「文档写了、代码没做」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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


def _operation(spec: dict[str, Any], path: str) -> dict[str, Any]:
    operation: dict[str, Any] = spec["paths"][path]["get"]
    return operation


def _deref(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """跟随 `$ref`，否则 FastAPI 生成的是 `#/components/schemas/<Model>`。"""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema

    *_, name = ref.split("/")

    resolved: dict[str, Any] = spec["components"]["schemas"][name]
    return resolved


def _response_schema(spec: dict[str, Any], path: str) -> dict[str, Any]:
    schema: dict[str, Any] = _operation(spec, path)["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    return _deref(spec, schema)


def test_versions_match() -> None:
    assert __version__ == _contract()["info"]["version"]


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
