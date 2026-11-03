"""RFC 7807 错误响应（`application/problem+json`）。

契约里所有错误响应都引用 `components.schemas.Problem`，所以这里统一把内部异常翻成
`Problem`，避免 FastAPI 默认的 `{"detail": ...}` 与契约不一致。
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"


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


def install_exception_handlers(app: FastAPI) -> None:
    """注册异常处理器（在 `create_app` 里调用）。"""

    @app.exception_handler(ProblemException)
    async def _handle_problem(request: Request, exc: ProblemException) -> JSONResponse:
        return _problem_response(
            request,
            Problem(status=exc.status, title=exc.title, detail=exc.detail),
        )

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
