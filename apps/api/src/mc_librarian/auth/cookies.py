"""会话 cookie 的读写。

- 名字固定 `mc_librarian_session`（与契约的 `cookieAuth` 一致）；
- `HttpOnly` + `SameSite=Lax`；
- `Secure` 由配置 `MC_LIBRARIAN_COOKIE_SECURE` 控制——本地 http 下设为 true 会让浏览器
  直接丢掉 cookie，所以默认 false，生产在 HTTPS 边界后面部署时设 true。
"""

from __future__ import annotations

from typing import Final

from fastapi import Response

from mc_librarian.config import SESSION_COOKIE_NAME, Settings

COOKIE_PATH: Final = "/"


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
