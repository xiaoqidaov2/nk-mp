from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

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

logger = logging.getLogger("nike_mp.api")

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """平台钉钉机器人：绑定会话、关键字指令；未配置时什么都不做。"""
    task: asyncio.Task | None = None
    try:
        from app.services import dingtalk_bot

        task = asyncio.create_task(dingtalk_bot.run_forever(settings))
    except Exception:  # noqa: BLE001 - 机器人不在时 API 必须照常工作
        logger.exception("dingtalk bot did not start")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(
    title="Nike 门店监控平台 API",
    version="0.1.0",
    description="门店价格与尺码库存监控服务。所有采集均通过只读接口完成。",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
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
