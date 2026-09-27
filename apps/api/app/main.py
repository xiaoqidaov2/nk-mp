from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import auth, catalog, changes, dashboard, health, matrix, ops, scans, tasks
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
):
    app.include_router(router, prefix=API_PREFIX)
app.include_router(health.router, prefix=API_PREFIX)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误", "error": type(exc).__name__})
