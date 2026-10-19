"""The Command Center API: uvicorn app.main:app"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .bootstrap import bootstrap
from .config import get_settings
from .db import dispose_engine
from .security import LoginThrottle
from .tasks import TaskError


def create_app(run_bootstrap: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_bootstrap:
            await bootstrap(settings)
        yield
        await dispose_engine()
        from .credit_repair import store

        await store.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.environment != "production" else None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
        redoc_url=None,
    )
    app.state.login_throttle = LoginThrottle(settings.login_max_failures, settings.login_lockout_minutes * 60)

    @app.exception_handler(TaskError)
    async def task_error(_: Request, exc: TaskError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    from .api import admin, auth, credit_repair, integrations, records, servers, sites, stats, supervisor, system, work

    for module in (auth, system, work, stats, supervisor, sites, servers, integrations, admin, records, credit_repair):
        app.include_router(module.router)
    return app
