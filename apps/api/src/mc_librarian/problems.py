"""RFC 7807 错误响应（`application/problem+json`）。

契约里所有错误响应都引用 `components.schemas.Problem`，所以这里统一把内部异常和
框架自己抛的 `HTTPException`（404 / 405 / …）都翻成 `Problem`，避免 FastAPI 默认的
纯文本或 `{"detail": ...}` 与契约不一致。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"

# 框架自己抛的 HTTPException（404 / 405 等）的默认标题；未列出的用 HTTP 标准短语。
HTTP_TITLES: Final[dict[int, str]] = {
    400: "请求非法",
    401: "未认证",
    403: "无权访问",
    404: "资源不存在",
    405: "方法不允许",
    409: "冲突",
    415: "不支持的媒体类型",
    422: "请求不合法",
    429: "请求过于频繁",
    500: "服务器内部错误",
    503: "服务不可用",
}

# API 层常用的两个标题，直接从 `HTTP_TITLES` 取——各模块不要再各写一份字面量。
TITLE_BAD_REQUEST: Final = HTTP_TITLES[422]
TITLE_NOT_FOUND: Final = HTTP_TITLES[404]
TITLE_CONFLICT: Final = HTTP_TITLES[409]


class Problem(BaseModel):
    """契约 `components.schemas.Problem`。"""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


class ProblemException(Exception):
    """路由/依赖里 raise 它，handler 统一转成 `application/problem+json`。"""

    def __init__(self, status: int, title: str, detail: str | None = None) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail


def _problem_response(request: Request, problem: Problem) -> JSONResponse:
    payload = problem.model_dump(exclude_none=True)
    payload.setdefault("instance", request.url.path)

    return JSONResponse(
        status_code=problem.status,
        content=payload,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def problem_response(description: str) -> dict[str, Any]:
    """FastAPI `responses=` 里的一项：声明 `application/problem+json` 错误体。"""
    return {
        "description": description,
        "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
    }


def component_schema() -> dict[str, Any]:
    """`Problem` 的 OpenAPI schema；由 `create_app` 挂进 `components.schemas`。"""
    return Problem.model_json_schema(ref_template="#/components/schemas/{model}")


def _http_phrase(status: int) -> str | None:
    """HTTP 标准短语（Starlette 抛 404/405 时的默认 detail）。"""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return None


def _status_title(status: int) -> str:
    """状态码 → 中文标题；不认识的码退回 HTTP 标准短语。"""
    if status in HTTP_TITLES:
        return HTTP_TITLES[status]

    return _http_phrase(status) or "请求失败"


def install_exception_handlers(app: FastAPI) -> None:
    """注册异常处理器（在 `create_app` 里调用）。"""

    @app.exception_handler(ProblemException)
    async def _handle_problem(request: Request, exc: ProblemException) -> JSONResponse:
        return _problem_response(
            request,
            Problem(status=exc.status, title=exc.title, detail=exc.detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架自己抛的 HTTPException（404 / 405 / …）也统一成 problem+json。

        保留原 status code 与语义头（405 的 `Allow` 必须留着，否则调用方不知道该用什么方法）。
        """
        raw_detail = exc.detail if isinstance(exc.detail, str) else None
        title = _status_title(exc.status_code)

        # FastAPI 的 404 默认 detail 是 "Not Found"，与标题重复时丢掉。
        detail = None if raw_detail in {None, title, _http_phrase(exc.status_code)} else raw_detail

        response = _problem_response(
            request,
            Problem(status=exc.status_code, title=title, detail=detail),
        )

        for key, value in (exc.headers or {}).items():
            response.headers[key] = value

        return response

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 只回字段路径与原因，不回 input：请求体里可能有明文密码。
        detail = "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )

        return _problem_response(
            request,
            Problem(status=422, title="请求不合法", detail=detail or None),
        )
