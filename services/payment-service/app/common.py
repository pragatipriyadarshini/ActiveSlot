"""Cross-cutting helpers shared by every service (copied, not imported, on purpose:
each service must build and run on its own, with no shared library).

Contents
  * structured JSON logging that carries a correlation (request) ID
  * pure-ASGI middleware that reads / creates X-Correlation-ID for every request
  * one consistent JSON error format  {"error": {"code", "message", "correlation_id", "details"}}
  * database engine factory (PostgreSQL in normal use, SQLite for tests / quick mode)
  * small time helpers (all timestamps are UTC)
"""
import contextvars
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import PlainSerializer
from sqlalchemy import create_engine, event, text
from starlette.exceptions import HTTPException as StarletteHTTPException

SERVICE_NAME = os.environ.get("SERVICE_NAME", "service")
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


# --------------------------------------------------------------------------- time
def utcnow() -> datetime:
    """Naive UTC 'now'. We store naive-UTC everywhere so PostgreSQL and SQLite behave the same."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else to_naive_utc(dt).isoformat(timespec="seconds") + "Z"


# datetime that is rendered as ISO-8601 with a trailing Z in JSON responses
UtcDateTime = Annotated[
    datetime, PlainSerializer(lambda d: iso(d), return_type=str, when_used="json")
]


# --------------------------------------------------------------------------- logging
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "correlation_id": correlation_id_var.get(),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(SERVICE_NAME)


log = setup_logging()


# --------------------------------------------------------------------------- correlation id
class CorrelationIdMiddleware:
    """Pure ASGI middleware: reuse the caller's X-Correlation-ID or create one, expose it to
    log lines (contextvar) and echo it on the response. Outbound calls forward it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        cid = headers.get(b"x-correlation-id", b"").decode() or str(uuid.uuid4())
        token = correlation_id_var.set(cid)
        started = time.perf_counter()
        result = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-correlation-id", cid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["path"] != "/health":
                log.info(
                    "request",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": result["status"],
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            correlation_id_var.reset(token)


def outbound_headers(extra: Optional[dict] = None) -> dict:
    """Headers for a service-to-service call: forwards the correlation ID."""
    headers = {"X-Correlation-ID": correlation_id_var.get()}
    if extra:
        headers.update(extra)
    return headers


# --------------------------------------------------------------------------- error format
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(f"{code}: {message}")


def error_body(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message,
                      "correlation_id": correlation_id_var.get(), "details": details}}


def error_response(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, details))


ERROR_RESPONSES = {
    400: {"description": "Bad request"}, 404: {"description": "Not found"},
    409: {"description": "Conflict with current state"}, 422: {"description": "Validation error"},
    503: {"description": "Dependency unavailable"},
}

_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def install(app: FastAPI) -> None:
    """Attach middleware and the consistent error handlers to a FastAPI app."""
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return error_response(exc.status_code, _HTTP_CODES.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return error_response(422, "VALIDATION_ERROR", "Request validation failed", details)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled_error")
        return error_response(500, "INTERNAL_ERROR", "Unexpected server error")


# --------------------------------------------------------------------------- database
def make_engine():
    """Engine from the DATABASE_URL environment variable (no default: fail loudly)."""
    url = os.environ["DATABASE_URL"]
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30,
                                             "isolation_level": None})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=30000")

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):  # take the write lock up front: no 'database is locked' surprises
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        return engine
    return create_engine(url, pool_size=20, max_overflow=20, pool_pre_ping=True)


def wait_for_db(engine, attempts: int = 30, delay: float = 1.0) -> None:
    for i in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("db_not_ready", extra={"attempt": i, "error": str(exc)[:120]})
            time.sleep(delay)
    raise RuntimeError("database not reachable")
