"""Protected Live Opportunity Board — a separate, board-only FastAPI process.

Architecture and safety boundary
---------------------------------
This module runs as its own OS process, entirely separate from the main APEX
application (`apex.app`, `apex.main`). It must never import `apex.app`,
`apex.main`, `apex.config`, `apex.db.connection`, `apex.actions`,
`apex.scheduler`, `apex.notifications`, any market/exchange client, or any
execution/trading module. It reuses only the existing, unmodified
`apex.opportunity.board.create_board_router` — the same read-only router the
main application already mounts in development — over a connection this
module opens itself, read-only.

Trust boundary — read this before changing anything
-----------------------------------------------------
This process trusts the `Tailscale-User-Login` request header ONLY because
the surrounding deployment architecture requires it to be reached exclusively
through a root-owned `tailscale serve` process forwarding to a
permission-restricted Unix domain socket (see
`docs/LIVE_OPPORTUNITY_BOARD_ACCESS.md` and
`deploy/apex-board.service.example`). An application-level header check is
NOT an origin/authentication boundary by itself — it is only meaningful
because the socket's filesystem permissions and `tailscale serve`'s identity
injection are the actual boundary. This module has no way to verify that
boundary at runtime; it is established entirely outside this code, by
systemd/socket permissions and Tailscale configuration that are explicitly
out of scope for this milestone.

Nothing in this module writes to the database, calls `apex.db.connection`,
binds a TCP listener, serves interactive API docs, or trusts any header other
than the one exact identity header described above.
"""
from __future__ import annotations

import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from apex.opportunity.board import create_board_router
from apex.opportunity.board_settings import BoardSettings

logger = logging.getLogger(__name__)

IDENTITY_HEADER_NAME = "Tailscale-User-Login"
_MAX_IDENTITY_HEADER_LENGTH = 256

_DEFAULT_BUSY_TIMEOUT_MS = 2000

RATE_LIMIT_CAPACITY = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0

_KNOWN_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})

_ROUTE_CLASS_BY_PATH = {
    "/healthz": "healthz",
    "/opportunities": "opportunities_html",
    "/opportunities/api": "opportunities_api",
}
_UNKNOWN_ROUTE_CLASS = "other"

_FORBIDDEN_BODY = {"error": "forbidden"}
_RATE_LIMITED_BODY = {"error": "rate limited"}
_SERVER_ERROR_BODY = {"error": "internal error"}

# Applied to every response — success, denial, and error alike. Compatible
# with the existing board HTML's inline <style> block; no external resource
# is ever allowed to load.
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
        "font-src 'none'; connect-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "Permissions-Policy": (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=(), "
        "interest-cohort=()"
    ),
}


# ------------------------------------------------------------------ read-only DB access

def open_read_only_connection(
    db_path: Path,
    *,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open an existing SQLite file strictly read-only.

    Resolves the already-existing absolute file and connects via a
    `file:`-scheme URI (`Path.as_uri()`, which safely percent-encodes spaces,
    `?`, and `#`) with `mode=ro`. Never creates a directory or file: a
    missing path raises before any connection is attempted. Sets and
    verifies `PRAGMA query_only=ON` before returning, and never issues a
    write, schema change, or journal-mode change.
    """
    resolved = Path(db_path).resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA query_only=ON")
    verified = conn.execute("PRAGMA query_only").fetchone()
    if verified is None or verified[0] != 1:
        conn.close()
        raise RuntimeError("failed to enforce read-only SQLite connection")
    return conn


# ------------------------------------------------------------------ rate limiting

class _FixedWindowRateLimiter:
    """Bounded in-memory rate limiter for exactly one allowed identity in one
    process. A single fixed owner bucket — it never grows regardless of
    caller count, and it need not persist across a restart.
    """

    def __init__(
        self,
        *,
        capacity: int = RATE_LIMIT_CAPACITY,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._window_seconds = window_seconds
        self._clock = clock
        self._window_start = clock()
        self._count = 0

    def allow(self) -> bool:
        now = self._clock()
        if now - self._window_start >= self._window_seconds:
            self._window_start = now
            self._count = 0
        if self._count >= self._capacity:
            return False
        self._count += 1
        return True


# ------------------------------------------------------------------ identity gate

def _is_valid_identity(value: Optional[str], allowed_identity: str) -> bool:
    """Exact, constant-time match against the one configured identity.

    Missing, empty, overlong, or control-character-containing values are
    rejected before any comparison. This check is meaningful only because
    the deployment's Unix-socket + `tailscale serve` boundary guarantees the
    header could only have been set by that trusted local reverse proxy —
    see the module docstring.
    """
    if not value:
        return False
    if len(value) > _MAX_IDENTITY_HEADER_LENGTH:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    return hmac.compare_digest(value.encode("utf-8"), allowed_identity.encode("utf-8"))


def _classify_route(path: str) -> str:
    return _ROUTE_CLASS_BY_PATH.get(path, _UNKNOWN_ROUTE_CLASS)


def _sanitize_method(method: str) -> str:
    upper = (method or "").upper()
    return upper if upper in _KNOWN_METHODS else "OTHER"


def _generic_json_response(status_code: int, body: dict) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code)


def _apply_security_headers(response: Response) -> None:
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value


class _ProtectedBoardMiddleware(BaseHTTPMiddleware):
    """The single ASGI HTTP middleware gating every request.

    Order per request: classify the route/method for logging, check the
    identity header, then check the rate limiter — all before `call_next`
    (i.e. before routing or any database query runs). Every response —
    normal, denied, rate-limited, or a caught unexpected exception — gets
    the fixed security headers and exactly one sanitized access log line
    recorded only after the final status is known.
    """

    def __init__(self, app, *, allowed_identity: str) -> None:
        super().__init__(app)
        self._allowed_identity = allowed_identity
        self._rate_limiter = _FixedWindowRateLimiter()

    async def dispatch(self, request: Request, call_next):
        route_class = _classify_route(request.url.path)
        method = _sanitize_method(request.method)
        try:
            identity = request.headers.get(IDENTITY_HEADER_NAME)
            if not _is_valid_identity(identity, self._allowed_identity):
                response: Response = _generic_json_response(403, _FORBIDDEN_BODY)
            elif not self._rate_limiter.allow():
                response = _generic_json_response(429, _RATE_LIMITED_BODY)
            else:
                try:
                    response = await call_next(request)
                except Exception:
                    logger.error(
                        "board_access_error principal=owner route_class=%s method=%s",
                        route_class,
                        method,
                    )
                    response = _generic_json_response(500, _SERVER_ERROR_BODY)
        except Exception:
            logger.error(
                "board_access_error principal=owner route_class=%s method=%s",
                route_class,
                method,
            )
            response = _generic_json_response(500, _SERVER_ERROR_BODY)

        _apply_security_headers(response)
        logger.info(
            "board_access principal=owner route_class=%s method=%s status=%s",
            route_class,
            method,
            response.status_code,
        )
        return response


# ------------------------------------------------------------------ app factory

def create_protected_board_app(
    settings: BoardSettings,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> FastAPI:
    """Build the protected board FastAPI app.

    Fails closed: raises immediately if `settings.enabled` is false, before
    any route, socket, or connection is created. `conn`, if supplied, is used
    as-is and is never closed by this app's lifespan (intended for isolated
    tests); otherwise a fresh read-only connection is opened from
    `settings.db_path` and is owned (and closed) by this app.

    Mounts only `GET /healthz` and the two existing board routes
    (`/opportunities`, `/opportunities/api`) via the unmodified
    `create_board_router`. Interactive docs/OpenAPI are disabled.
    """
    if not settings.enabled:
        raise RuntimeError(
            "Protected Live Opportunity Board is disabled (APEX_BOARD_ENABLED is not set)"
        )

    owns_connection = conn is None
    board_conn = conn if conn is not None else open_read_only_connection(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if owns_connection:
                board_conn.close()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(_ProtectedBoardMiddleware, allowed_identity=settings.allowed_identity)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_board_router(board_conn))

    return app


# ------------------------------------------------------------------ UDS runner

def build_uvicorn_config(app: FastAPI, settings: BoardSettings) -> uvicorn.Config:
    """Build the Uvicorn config for serving `app` over a Unix domain socket.

    Never exposes a host/port (TCP) option. Fails without touching the
    filesystem if the socket's parent directory does not already exist, or
    if something already exists at the socket path — this never unlinks an
    arbitrary path; the restricted runtime directory itself is created later,
    by systemd, outside this milestone.

    Passes `access_log=False`, mirroring the main APEX server's precedent:
    Uvicorn's default access formatter writes the raw request line
    (`%(request_line)s`, including path and query string) to stdout, which
    under the systemd example would duplicate that raw data into journald and
    violate this process's sanitized, process-only audit-log invariant (the
    single `board_access`/`board_access_error` lines emitted by
    `_ProtectedBoardMiddleware` remain the only access log for this process).
    """
    socket_path = settings.socket_path
    parent = socket_path.parent
    if not parent.is_dir():
        raise RuntimeError(f"socket parent directory does not exist: {parent}")
    if socket_path.exists():
        raise RuntimeError(f"refusing to overwrite existing path at socket location: {socket_path}")

    return uvicorn.Config(
        app,
        uds=str(socket_path),
        workers=1,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


def run(settings: Optional[BoardSettings] = None) -> None:
    """Entry point: build the app once and serve it over the configured UDS only."""
    resolved_settings = settings or BoardSettings()
    app = create_protected_board_app(resolved_settings)
    config = build_uvicorn_config(app, resolved_settings)
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    run()
