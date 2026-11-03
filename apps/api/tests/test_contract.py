"""契约一致性测试：`docs/openapi.yaml` 是唯一真源，代码不得与之漂移。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from mc_librarian import __version__
from mc_librarian.config import find_repo_root


def _load_contract() -> dict[str, Any]:
    root = find_repo_root()
    assert root is not None, "测试应在仓库内运行（docs/openapi.yaml 可见）"
    with Path(root / "docs" / "openapi.yaml").open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


def test_app_version_matches_contract() -> None:
    contract = _load_contract()

    assert __version__ == contract["info"]["version"]


def test_health_schema_matches_contract() -> None:
    contract = _load_contract()
    health = contract["paths"]["/v1/health"]["get"]

    assert health["operationId"] == "getHealth"
    assert health["security"] == []

    schema = health["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["status", "version"]
    assert schema["properties"]["status"]["const"] == "ok"


@pytest.mark.parametrize("key", ["status", "version"])
def test_health_response_fields_are_in_contract(client: Any, key: str) -> None:
    contract = _load_contract()
    schema = contract["paths"]["/v1/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert key in schema["properties"]
    assert key in client.get("/v1/health").json()
