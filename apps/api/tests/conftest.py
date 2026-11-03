"""测试公用 fixture。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_librarian.answer import client as answer_client
from mc_librarian.auth import passwords
from mc_librarian.config import SESSION_COOKIE_NAME, Settings
from mc_librarian.db import CONTROL_DB_NAME, utc_now
from mc_librarian.indexing import summarize
from mc_librarian.main import create_app

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password-1"
USER_USERNAME = "worker"
USER_PASSWORD = "worker-password-1"

# 测试用主密钥（`MC_LIBRARIAN_SECRET`）：`settings` 表里的密钥用 AES-GCM 加密，
# 缺它 `lifespan` 直接启动失败（issue #11 验收第 3 条），所以建 app 的测试都得给。
TEST_SECRET = "test-secret-0123456789abcdef"

NETWORK_ENV = "MC_LIBRARIAN_NETWORK"

# 假 LLM 生成的三行摘要模板：含游戏机制词（规格 §1 要求），且每个 fqn 输出确定。
FAKE_SUMMARY_TEMPLATE = (
    "刷怪与区块生成：{simple} 在游戏里负责生成与掉落相关的约束\n"
    "机制：按每刻预算与区块范围调度\n"
    "关键成员：{simple}"
)

_FQN_LINE = re.compile(r"^### (.+)$", re.MULTILINE)


class FakeLlm:
    """假 LLM：从提示词里取出本批的 fqn，回一个合法的 JSON 对象。

    `calls` 记录每次调用的 fqn 列表（幂等测试断言「类未变时不重复调用」）；`failures` 里的
    fqn 不返回摘要（验证「单类失败、其余照常」）；`overrides` 里的 fqn 换成指定文本
    （验证「格式不合格的摘要不进库」）；`raw_override` 直接指定原始响应文本
    （验证「解析失败按单类失败处理」）。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.failures: set[str] = set()
        self.overrides: dict[str, str] = {}
        self.raw_override: str | None = None

    @property
    def fqns(self) -> list[str]:
        """所有调用里请求过的 fqn（去重后仍保留调用顺序）。"""
        return [fqn for call in self.calls for fqn in call]

    def post(self, url: str, payload: dict[str, object], api_key: str, timeout: float) -> str:
        del url, api_key, timeout
        messages = payload["messages"]
        prompt = messages[-1]["content"]  # type: ignore[index]
        fqns = _FQN_LINE.findall(prompt)
        self.calls.append(list(fqns))

        if self.raw_override is not None:
            return self.raw_override

        return json.dumps(
            {
                fqn: self.overrides.get(
                    fqn, FAKE_SUMMARY_TEMPLATE.format(simple=fqn.rsplit(".", 1)[-1])
                )
                for fqn in fqns
                if fqn not in self.failures
            }
        )


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    """全局替换 LLM 的 HTTP 发送函数：测试不联网，也不碰真实 LLM。

    只换掉最外层的 `post_chat_completion`，因此客户端、提示词拼装、响应解析与入库都走真实
    代码路径；需要断言调用次数/注入失败的用例直接拿这个 fixture。
    """
    fake = FakeLlm()
    monkeypatch.setattr(summarize, "post_chat_completion", fake.post)

    return fake


class FakeChat:
    """假 LLM（问答用）：脚本化 chat completion 响应，不联网。

    与 `FakeLlm` 同一做法：只替换最外层的 `post_chat_completion`，客户端解析、工具循环、
    上下文预算与收尾都走真实代码路径。`script` 是待返回的 message 队列（按顺序弹出）；
    用完后：给了工具就回 `always_tools`（默认空 → 直接给答案），强制收尾（不给工具）那一次
    回 `default_answer`。`fail_with` 让下一次调用抛异常（验证降级）。
    """

    def __init__(self) -> None:
        self.script: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.always_tools: list[dict[str, Any]] = []
        self.default_answer = "这是基于检索结果的答案。"
        self.fail_with: Exception | None = None
        # 第几次调用开始失败（1-based）：验证「已经召回一部分后才失败」的降级路径。
        self.fail_from = 1
        self.usage: dict[str, int] = {"prompt_tokens": 11, "completion_tokens": 7}

    def post(
        self, url: str, payload: dict[str, object], api_key: str, timeout: float
    ) -> dict[str, Any]:
        del url, api_key, timeout
        self.calls.append(payload)

        if self.fail_with is not None and len(self.calls) >= self.fail_from:
            raise self.fail_with

        if self.script:
            message = self.script.pop(0)
        elif payload.get("tools"):
            message = {"role": "assistant", "content": None, "tool_calls": self.always_tools}
        else:
            message = {"role": "assistant", "content": self.default_answer}

        return {"choices": [{"message": message}], "usage": dict(self.usage)}


@pytest.fixture(autouse=True)
def fake_chat(monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    """全局替换问答的 LLM 发送函数：`/v1/ask` 的测试不联网。"""
    fake = FakeChat()
    monkeypatch.setattr(answer_client, "post_chat_completion", fake.post)

    return fake


def tool_call(name: str, **arguments: Any) -> dict[str, Any]:
    """构造一条「模型要求调用工具」的 assistant message。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{name}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def final_answer(text: str) -> dict[str, Any]:
    """构造一条「模型给出最终答案」的 assistant message。"""
    return {"role": "assistant", "content": text}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="运行标了 @pytest.mark.network 的联网测试（默认跳过）",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`network` 测试默认跳过：它们会真下载 jar 并跑真反编译，太慢且依赖外网。"""
    if config.getoption("--run-network") or os.environ.get(NETWORK_ENV) == "1":
        return

    skip = pytest.mark.skip(reason=f"默认跳过联网测试；加 --run-network 或 {NETWORK_ENV}=1 启用")

    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """临时数据根 + 假 LLM 配置：真实 LLM 调用由 `fake_llm` fixture 换掉（不联网）。"""
    return Settings(
        data_dir=tmp_path / "data",
        secret=TEST_SECRET,
        llm_base_url="http://fake-llm.invalid/v1",
        llm_model="fake-model",
        llm_api_key="fake-key",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def control_db(settings: Settings) -> Path:
    """控制面 sqlite 路径（lifespan 已建好）。"""
    return settings.data_dir / CONTROL_DB_NAME


@pytest.fixture
def conn(client: TestClient, control_db: Path) -> Iterator[sqlite3.Connection]:
    """直接读库用：测试要断言「库里存的不是明文」。`client` 保证库已被 lifespan 初始化。"""
    connection = sqlite3.connect(control_db)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    """已经建好管理员并登录（cookie 在 client 的 cookie jar 里）的客户端。"""
    created = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert created.status_code == 201, created.text

    return client


@pytest.fixture
def other_client(settings: Settings) -> Iterator[TestClient]:
    """第二个「浏览器」：同一数据根上的独立 cookie jar。

    用来验证「禁用即踢下线」与多账号场景（同一 client 的 cookie 会被后一次登录覆盖）。
    """
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def user_client(client: TestClient, conn: sqlite3.Connection) -> TestClient:
    """已登录的普通用户（role=user）：用来验证 `/v1/admin/*` 的 403。

    #9 落地用户管理接口前，直接往 `users` 表插一行再走正常登录。
    """
    created = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert created.status_code == 201, created.text

    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, enabled, created_at)"
        " VALUES (?, ?, ?, 'user', 1, ?)",
        ("user-1", USER_USERNAME, passwords.hash_password(USER_PASSWORD), utc_now()),
    )
    conn.commit()
    client.cookies.clear()

    logged_in = client.post(
        "/v1/auth/login",
        json={"username": USER_USERNAME, "password": USER_PASSWORD},
    )

    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()["role"] == "user"

    return client


def session_cookie(client: TestClient) -> str:
    """取当前 client 手上的会话 cookie 明文（等价于浏览器里的 cookie）。"""
    value = client.cookies.get(SESSION_COOKIE_NAME)

    assert value, "应该已经下发会话 cookie"

    return str(value)


def invite_token(invite_url: str) -> str:
    """从 `invite_url`（`/invite/<token>`）里取出明文 token。"""
    return invite_url.rsplit("/", 1)[-1]


def join_via_invite(
    admin_client: TestClient,
    browser: TestClient,
    *,
    username: str,
    password: str,
    role: str = "user",
) -> dict[str, Any]:
    """管理员建号 → 被邀请人在 `browser` 上接受邀请并登录；返回创建响应的 JSON。"""
    created = admin_client.post("/v1/admin/users", json={"username": username, "role": role})

    assert created.status_code == 201, created.text

    accepted = browser.post(
        f"/v1/invites/{invite_token(created.json()['invite_url'])}/accept",
        json={"password": password},
    )

    assert accepted.status_code == 201, accepted.text

    payload: dict[str, Any] = created.json()

    return payload
