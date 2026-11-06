"""`/v1/admin/users` 与 `/v1/admin/audit`：用户管理、最后管理员保护、审计落库。"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from conftest import ADMIN_USERNAME, join_via_invite
from mc_librarian.auth.passwords import UNUSABLE_PASSWORD_HASH

MEMBER_PASSWORD = "member-password-1"


def test_admin_endpoints_require_authentication(admin_client: TestClient) -> None:
    admin_client.cookies.clear()

    for method, path in (
        ("get", "/v1/admin/users"),
        ("post", "/v1/admin/users"),
        ("patch", "/v1/admin/users/whatever"),
        ("get", "/v1/admin/audit"),
    ):
        response = admin_client.request(method.upper(), path, json={})

        assert response.status_code == 401, f"{method.upper()} {path} 应该要求登录"
        assert response.headers["content-type"].startswith("application/problem+json")


def test_non_admin_is_forbidden_on_admin_endpoints(
    admin_client: TestClient, other_client: TestClient
) -> None:
    created = join_via_invite(
        admin_client, other_client, username="member", password=MEMBER_PASSWORD
    )
    user_id = created["user"]["id"]

    for method, path in (
        ("get", "/v1/admin/users"),
        ("post", "/v1/admin/users"),
        ("patch", f"/v1/admin/users/{user_id}"),
        ("get", "/v1/admin/audit"),
    ):
        response = other_client.request(method.upper(), path, json={})

        assert response.status_code == 403, f"{method.upper()} {path} 应该拒绝普通用户"
        assert response.headers["content-type"].startswith("application/problem+json")


def test_list_users_returns_everyone(admin_client: TestClient, other_client: TestClient) -> None:
    join_via_invite(admin_client, other_client, username="member", password=MEMBER_PASSWORD)

    response = admin_client.get("/v1/admin/users")

    assert response.status_code == 200
    body = response.json()
    assert {user["username"] for user in body["users"]} == {ADMIN_USERNAME, "member"}
    assert all("password_hash" not in user for user in body["users"])
    # 邀请链接只在创建时返回一次，列表里不该出现。
    assert "invite_url" not in response.text


def test_create_user_returns_one_time_invite_link(admin_client: TestClient) -> None:
    response = admin_client.post("/v1/admin/users", json={"username": "alice"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "user"
    assert body["user"]["enabled"] is True
    assert "password" not in body["user"]
    assert body["invite_url"].startswith("/invite/")


def test_created_user_has_no_usable_password(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    """管理员建号时不给密码：占位哈希不是合法 argon2 值，任何密码都登录不进。"""
    admin_client.post("/v1/admin/users", json={"username": "alice"})

    row = conn.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()

    assert row["password_hash"] == UNUSABLE_PASSWORD_HASH


def test_created_user_cannot_login_before_accepting_invite(
    admin_client: TestClient, other_client: TestClient
) -> None:
    admin_client.post("/v1/admin/users", json={"username": "alice"})

    response = other_client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": MEMBER_PASSWORD},
    )

    assert response.status_code == 401


def test_create_user_with_duplicate_username_is_409(admin_client: TestClient) -> None:
    admin_client.post("/v1/admin/users", json={"username": "alice"})

    response = admin_client.post("/v1/admin/users", json={"username": "alice"})

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


def test_patch_promotes_and_demotes(admin_client: TestClient, other_client: TestClient) -> None:
    created = join_via_invite(
        admin_client, other_client, username="member", password=MEMBER_PASSWORD
    )
    user_id = created["user"]["id"]

    promoted = admin_client.patch(f"/v1/admin/users/{user_id}", json={"role": "admin"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

    demoted = admin_client.patch(f"/v1/admin/users/{user_id}", json={"role": "user"})
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "user"


def test_patch_without_changes_returns_current_user(admin_client: TestClient) -> None:
    me = admin_client.get("/v1/auth/me").json()

    response = admin_client.patch(f"/v1/admin/users/{me['id']}", json={})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["enabled"] is True


def test_patch_unknown_user_is_404(admin_client: TestClient) -> None:
    response = admin_client.patch("/v1/admin/users/does-not-exist", json={"enabled": False})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_last_enabled_admin_cannot_be_demoted(admin_client: TestClient) -> None:
    me = admin_client.get("/v1/auth/me").json()

    response = admin_client.patch(f"/v1/admin/users/{me['id']}", json={"role": "user"})

    assert response.status_code == 409
    assert admin_client.get("/v1/auth/me").json()["role"] == "admin"


def test_last_enabled_admin_cannot_be_disabled(admin_client: TestClient) -> None:
    me = admin_client.get("/v1/auth/me").json()

    response = admin_client.patch(f"/v1/admin/users/{me['id']}", json={"enabled": False})

    assert response.status_code == 409
    # 409 不落库：账号仍然启用、会话仍然有效。
    assert admin_client.get("/v1/auth/me").status_code == 200


def test_admin_can_be_demoted_when_another_admin_exists(
    admin_client: TestClient, other_client: TestClient
) -> None:
    join_via_invite(
        admin_client,
        other_client,
        username="second-admin",
        password=MEMBER_PASSWORD,
        role="admin",
    )
    me = admin_client.get("/v1/auth/me").json()

    response = admin_client.patch(f"/v1/admin/users/{me['id']}", json={"role": "user"})

    assert response.status_code == 200
    assert response.json()["role"] == "user"


def test_last_admin_guard_ignores_disabled_admins(
    admin_client: TestClient, other_client: TestClient
) -> None:
    """被停用的 admin 不算「启用的管理员」，所以唯一启用的那个仍受保护。"""
    created = join_via_invite(
        admin_client,
        other_client,
        username="second-admin",
        password=MEMBER_PASSWORD,
        role="admin",
    )
    assert (
        admin_client.patch(
            f"/v1/admin/users/{created['user']['id']}", json={"enabled": False}
        ).status_code
        == 200
    )

    me = admin_client.get("/v1/auth/me").json()
    response = admin_client.patch(f"/v1/admin/users/{me['id']}", json={"enabled": False})

    assert response.status_code == 409


def test_disabling_user_revokes_all_sessions(
    admin_client: TestClient, other_client: TestClient
) -> None:
    created = join_via_invite(
        admin_client, other_client, username="member", password=MEMBER_PASSWORD
    )
    user_id = created["user"]["id"]

    assert other_client.get("/v1/auth/me").status_code == 200

    response = admin_client.patch(f"/v1/admin/users/{user_id}", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    # 禁用即踢下线：会话行被删，旧 cookie 立刻 401。
    assert other_client.get("/v1/auth/me").status_code == 401


def test_disabled_user_cannot_log_in_again(
    admin_client: TestClient, other_client: TestClient
) -> None:
    created = join_via_invite(
        admin_client, other_client, username="member", password=MEMBER_PASSWORD
    )
    admin_client.patch(f"/v1/admin/users/{created['user']['id']}", json={"enabled": False})
    other_client.cookies.clear()

    response = other_client.post(
        "/v1/auth/login",
        json={"username": "member", "password": MEMBER_PASSWORD},
    )

    assert response.status_code == 401


def test_re_enabling_user_allows_login_again(
    admin_client: TestClient, other_client: TestClient
) -> None:
    created = join_via_invite(
        admin_client, other_client, username="member", password=MEMBER_PASSWORD
    )
    user_id = created["user"]["id"]
    admin_client.patch(f"/v1/admin/users/{user_id}", json={"enabled": False})
    admin_client.patch(f"/v1/admin/users/{user_id}", json={"enabled": True})
    other_client.cookies.clear()

    response = other_client.post(
        "/v1/auth/login",
        json={"username": "member", "password": MEMBER_PASSWORD},
    )

    assert response.status_code == 200


def test_audit_records_user_and_invite_creation(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    admin = admin_client.get("/v1/auth/me").json()
    admin_client.post("/v1/admin/users", json={"username": "alice", "role": "admin"})

    rows = conn.execute("SELECT actor_id, action, target, detail_json FROM audit").fetchall()

    assert [row["action"] for row in rows] == ["user.created", "invite.created"]
    assert all(row["actor_id"] == admin["id"] for row in rows)
    assert all(row["target"] == "alice" for row in rows)
    assert rows[0]["detail_json"] == '{"role": "admin"}'


def test_audit_records_role_change_and_disable(
    admin_client: TestClient, other_client: TestClient
) -> None:
    created = join_via_invite(
        admin_client, other_client, username="member", password=MEMBER_PASSWORD
    )
    user_id = created["user"]["id"]

    admin_client.patch(f"/v1/admin/users/{user_id}", json={"role": "admin"})
    admin_client.patch(f"/v1/admin/users/{user_id}", json={"enabled": False})

    entries = admin_client.get("/v1/admin/audit").json()["entries"]
    actions = [entry["action"] for entry in entries]

    # 最新在前
    assert actions == [
        "user.disabled",
        "user.role_changed",
        "invite.accepted",
        "invite.created",
        "user.created",
    ]
    role_changed = entries[1]
    assert role_changed["actor"] == ADMIN_USERNAME
    assert role_changed["target"] == "member"
    assert role_changed["detail"] == {"from": "user", "to": "admin"}
    assert entries[0]["detail"] == {"sessions_revoked": 1}
    assert role_changed["ip"]


def test_audit_can_be_filtered_by_actor(
    admin_client: TestClient, other_client: TestClient
) -> None:
    join_via_invite(admin_client, other_client, username="member", password=MEMBER_PASSWORD)

    entries = admin_client.get("/v1/admin/audit", params={"actor": "member"}).json()["entries"]

    assert entries
    assert {entry["actor"] for entry in entries} == {"member"}
    assert {entry["action"] for entry in entries} == {"invite.accepted"}


def test_audit_respects_limit(admin_client: TestClient) -> None:
    admin_client.post("/v1/admin/users", json={"username": "alice"})

    entries = admin_client.get("/v1/admin/audit", params={"limit": 1}).json()["entries"]

    assert len(entries) == 1


def test_delete_user_removes_personal_data_but_keeps_audit(
    admin_client: TestClient, conn: Any
) -> None:
    """删除用户：会话/PAT/邀请随人删，审计保留但引用置空。"""
    import uuid

    from mc_librarian.auth import passwords, users
    from mc_librarian.db import utc_now

    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, enabled, created_at)"
        " VALUES (?, ?, ?, 'user', 1, ?)",
        (user_id, "to-delete", passwords.hash_password("password-1234"), utc_now()),
    )
    conn.execute(
        "INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, "hash-x", utc_now(), utc_now()),
    )
    conn.commit()

    response = admin_client.delete(f"/v1/admin/users/{user_id}")

    assert response.status_code == 204, response.text
    assert users.find_by_id(conn, user_id) is None
    # 会话（ON DELETE CASCADE）跟着走
    sessions_left = conn.execute(
        "SELECT count(*) FROM sessions WHERE user_id = ?", (user_id,)
    ).fetchone()[0]

    assert sessions_left == 0
    # 审计保留：`user.deleted` 记录在案，target 是用户名快照
    row = conn.execute(
        "SELECT action, target FROM audit WHERE action = 'user.deleted'"
        " ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row["target"] == "to-delete"


def test_delete_user_rejects_self(admin_client: TestClient, conn: Any) -> None:
    me = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()["id"]

    response = admin_client.delete(f"/v1/admin/users/{me}")

    assert response.status_code == 409
    assert "自己" in response.json()["detail"]


def test_delete_user_rejects_missing_user(admin_client: TestClient) -> None:
    response = admin_client.delete("/v1/admin/users/does-not-exist")

    assert response.status_code == 404
