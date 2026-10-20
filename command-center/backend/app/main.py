"""The Command Center API: uvicorn app.main:app"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

from . import __version__
from .bootstrap import bootstrap
from .brain.store import BrainError
from .config import get_settings
from .db import describe_error, dispose_engine
from .security import LoginThrottle
from .tasks import TaskError

log = logging.getLogger(__name__)


class _NoQueryString(logging.Filter):
    """uvicorn's access log without query strings: the text of a JARVIS Brain search (GET /api/brain/search?q=...)
    never reaches the container log. uvicorn logs (client, method, path with query, HTTP version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


NO_QUERY_STRING = _NoQueryString()


def create_app(run_bootstrap: bool = True) -> FastAPI:
    settings = get_settings()
    # uvicorn has set up its loggers before it calls this factory; adding the same filter again changes nothing.
    logging.getLogger("uvicorn.access").addFilter(NO_QUERY_STRING)

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

    @app.exception_handler(BrainError)
    async def brain_error(_: Request, exc: BrainError) -> JSONResponse:
        # The memory service refused the request; its messages carry no stored text (app/brain/store.py).
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        # The database refused a statement. str(exc) carries PostgreSQL's DETAIL (the row's values: customer or
        # imported text), so the log and the reply name classes and the SQLSTATE only; the path has no query string.
        log.error("%s %s: %s", request.method, request.url.path, describe_error(exc))
        return JSONResponse({"detail": "the database refused this change; nothing was saved"},
                            status_code=409 if isinstance(exc, IntegrityError) else 500)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    from .api import (
        admin,
        auth,
        brain,
        credit_repair,
        integrations,
        records,
        research,
        servers,
        sites,
        stats,
        supervisor,
        system,
        work,
    )

    for module in (auth, system, work, stats, supervisor, sites, servers, integrations, admin, records, credit_repair,
                   brain, research):
        app.include_router(module.router)
    return app
