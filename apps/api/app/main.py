from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import (
    auth,
    catalog,
    changes,
    dashboard,
    health,
    matrix,
    nike_credentials,
    notifications,
    ops,
    scans,
    tasks,
)
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="Nike 门店监控平台 API",
    version="0.1.0",
    description="门店价格与尺码库存监控服务。所有采集均通过只读接口完成。",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

API_PREFIX = "/api/v1"

for router in (
    auth.router,
    catalog.router,
    tasks.router,
    scans.router,
    matrix.router,
    changes.router,
    dashboard.router,
    ops.router,
    nike_credentials.router,
    notifications.router,
):
    app.include_router(router, prefix=API_PREFIX)
app.include_router(health.router, prefix=API_PREFIX)

SELF_SERVICE_PREFIX = f"{API_PREFIX}/me/nike"


@app.exception_handler(RequestValidationError)
async def scoped_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Whitelist validation output on self-service routes.

    The raw pydantic payload embeds the rejected input (phone numbers, codes)
    and is fine to expose only in the generic shape; on ``/me/nike*`` we emit
    ``loc``/``msg``/``type`` and nothing else.
    """
    if request.url.path.startswith(SELF_SERVICE_PREFIX):
        errors = [
            {
                "loc": [str(part) for part in (item.get("loc") or [])],
                "msg": str(item.get("msg") or ""),
                "type": str(item.get("type") or ""),
            }
            for item in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误", "error": type(exc).__name__})
