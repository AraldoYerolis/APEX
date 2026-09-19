"""First-party, read-only async HTTPS transport for the GMGN research
pilot.

Fixed to `https://openapi.gmgn.ai` only — there is no configurable host.
Every request is checked against `contract.ALLOWED_ENDPOINTS` (the exact
method/path allowlist) before any I/O; anything else raises without ever
touching the network. Redirects are never followed and any redirect
response is treated as a failure. This client never implements signature
support (vendor requests here are API-key-only: `X-APIKEY` header plus a
`timestamp` and fresh UUID `client_id`), and it refuses to be constructed
against an API-key environment-variable name that implies private/signing/
secret key material.

`http_client`, `clock`, `sleep`, and `uuid_factory` are all injectable so
tests are fully deterministic and never touch a real socket, a real clock,
or the real environment/credentials.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Optional, Protocol

import httpx

from apex.research.gmgn.contract import ALLOWED_ENDPOINTS, GET, POST

GMGN_BASE_URL = "https://openapi.gmgn.ai"
USER_AGENT = "APEX-Research-GMGN-Pilot/0.1 (+read-only)"

DEFAULT_API_KEY_ENV_VAR = "GMGN_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_MAX_STARTS_PER_SECOND = 2
DEFAULT_RETRY_DELAY_SECONDS = 0.5
_MAX_ATTEMPTS = 2  # exactly one bounded retry, never a retry loop
_TRANSIENT_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

# Defense in depth: refuse to source the API key from an environment
# variable whose *name* implies private/signing/secret key material — this
# client only ever sends a plain vendor API key header and never implements
# signature support.
_DISALLOWED_ENV_NAME_MARKERS: tuple[str, ...] = (
    "PRIVATE",
    "SIGN",
    "SECRET",
    "WALLET",
    "SEED",
    "MNEMONIC",
    "PASSPHRASE",
)


class GmgnTransportError(RuntimeError):
    """A request failed at the transport level (connection/timeout) after
    the single bounded retry was exhausted."""


class GmgnHttpError(RuntimeError):
    """A request received a non-2xx HTTP status after the single bounded
    retry (for 429/transient 5xx) was exhausted, or a non-retryable
    non-2xx status."""

    def __init__(self, status_code: Optional[int]) -> None:
        super().__init__(f"GMGN request failed with HTTP status {status_code}")
        self.status_code = status_code


class GmgnRedirectResponseError(RuntimeError):
    """The vendor responded with a redirect. Redirects are never followed
    and are always treated as a failure, never retried."""


class GmgnMalformedResponseError(RuntimeError):
    """The response body was oversized, not valid JSON, not a JSON object,
    or otherwise did not match the documented envelope shape."""


class GmgnVendorError(RuntimeError):
    """The vendor's own envelope reported a nonzero `code`.

    Vendor-controlled message text (the envelope's `msg` field) is never
    retained or echoed — only the numeric `code` and a fixed generic
    message are exposed, so a vendor response can never smuggle secret
    material (e.g. an API key reflected back by the vendor) into an
    exception string or attribute.
    """

    def __init__(self, code: int, message: Optional[str]) -> None:
        super().__init__(f"GMGN vendor error code={code}")
        self.code = code


class GmgnEndpointNotAllowedError(ValueError):
    """The requested method/path is not in the exact allowlist. Raised
    before any I/O is attempted."""


class GmgnCredentialConfigurationError(ValueError):
    """Refused to source API key material because the configured
    environment-variable name implies private/signing/secret key material,
    or because no API key was configured at all."""


def _validate_api_key_env_var_name(env_var_name: str) -> None:
    upper = env_var_name.upper()
    for marker in _DISALLOWED_ENV_NAME_MARKERS:
        if marker in upper:
            raise GmgnCredentialConfigurationError(
                "refusing to source the GMGN API key from an environment variable name "
                f"implying private/signing/secret key material: {env_var_name!r}"
            )


def _refuse_disallowed_gmgn_environment_variable_names() -> None:
    """Inspects only environment variable *names* (never values) and refuses
    construction if any present `GMGN_*` variable name implies private/
    signing/wallet/seed/mnemonic/passphrase/secret key material — even if
    that variable is not the one configured as the API key source. This is
    a defense-in-depth check distinct from `_validate_api_key_env_var_name`,
    which only checks the *chosen* variable name."""
    for name in os.environ:
        if not name.startswith("GMGN_"):
            continue
        _validate_api_key_env_var_name(name)


class SupportsGmgnHttpRequest(Protocol):
    """Minimal shape required of an injected HTTP client: a single async
    `request` method returning an object with `status_code`, `headers`,
    and `content` (bytes)."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
        json: Optional[dict[str, Any]],
    ) -> Any: ...


class GmgnRateLimiter:
    """Conservative rolling-window limiter: no more than
    `max_starts_per_second` request starts within any rolling 1-second
    window. `clock`/`sleep` are injectable so tests never depend on real
    wall-clock time."""

    def __init__(
        self,
        max_starts_per_second: int = DEFAULT_MAX_STARTS_PER_SECOND,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._max = max_starts_per_second
        self._clock = clock
        self._sleep = sleep
        self._starts: list[float] = []
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        async with self._get_lock():
            while True:
                now = self._clock()
                self._starts = [t for t in self._starts if now - t < 1.0]
                if len(self._starts) < self._max:
                    self._starts.append(now)
                    return
                wait_for = max(1.0 - (now - self._starts[0]), 0.0)
                await self._sleep(wait_for)


def _is_redirect(response: Any, status_code: Optional[int]) -> bool:
    if getattr(response, "is_redirect", False):
        return True
    return status_code in _REDIRECT_STATUS_CODES


class GmgnResearchTransport:
    """Small first-party async HTTPS client fixed to `https://openapi.gmgn.ai`."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_key_env_var: str = DEFAULT_API_KEY_ENV_VAR,
        http_client: Optional[SupportsGmgnHttpRequest] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uuid_factory: Callable[[], Any] = uuid.uuid4,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        limiter: Optional[GmgnRateLimiter] = None,
    ) -> None:
        _refuse_disallowed_gmgn_environment_variable_names()
        _validate_api_key_env_var_name(api_key_env_var)
        if api_key is not None:
            # Sealed rule: an explicit api_key is only accepted alongside an
            # injected http_client (tests). Production construction (no
            # injected client) must source the key only from the
            # configured environment variable.
            if http_client is None:
                raise GmgnCredentialConfigurationError(
                    "an explicit api_key is only accepted alongside an injected http_client; "
                    f"production construction must source the API key from the {api_key_env_var} "
                    "environment variable"
                )
            resolved_key = api_key
        else:
            resolved_key = os.environ.get(api_key_env_var)
        if not resolved_key:
            raise GmgnCredentialConfigurationError("no GMGN API key configured")
        self._api_key = resolved_key
        self._http_client = http_client
        self._clock = clock
        self._sleep = sleep
        self._uuid_factory = uuid_factory
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._retry_delay_seconds = retry_delay_seconds
        self._limiter = limiter if limiter is not None else GmgnRateLimiter(sleep=sleep)

    def __repr__(self) -> str:
        # Never reveal the API key, even in repr.
        return f"{type(self).__name__}(base_url={GMGN_BASE_URL!r})"

    async def get(self, path: str, *, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return await self._request(GET, path, params=params)

    async def post(self, path: str, *, json_body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return await self._request(POST, path, json_body=json_body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise GmgnEndpointNotAllowedError(
                f"{method} {path} is not an allowlisted GMGN research endpoint"
            )
        url = f"{GMGN_BASE_URL}{path}"
        last_transport_exc: Optional[BaseException] = None
        last_http_exc: Optional[GmgnHttpError] = None

        for attempt in range(_MAX_ATTEMPTS):
            await self._limiter.acquire()
            query = dict(params or {})
            query["timestamp"] = str(int(self._clock()))
            query["client_id"] = str(self._uuid_factory())
            headers = {
                "X-APIKEY": self._api_key,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }

            try:
                response = await self._send_once(method, url, query, json_body, headers)
            except Exception as exc:  # transport-level failure (connection, timeout, ...)
                last_transport_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await self._sleep(self._retry_delay_seconds)
                    continue
                raise GmgnTransportError("GMGN request failed at the transport level") from None

            status_code = getattr(response, "status_code", None)
            if _is_redirect(response, status_code):
                # Never retried: a redirect is a policy failure, not a
                # transient condition.
                raise GmgnRedirectResponseError(
                    f"unexpected redirect response (status={status_code})"
                )
            if status_code in _TRANSIENT_RETRYABLE_STATUS_CODES:
                last_http_exc = GmgnHttpError(status_code)
                if attempt < _MAX_ATTEMPTS - 1:
                    await self._sleep(self._retry_delay_seconds)
                    continue
                raise last_http_exc
            if status_code is None or not (200 <= status_code < 300):
                raise GmgnHttpError(status_code)

            return self._parse_envelope(response)

        # Unreachable in practice (every loop iteration either returns or
        # raises), kept only so a type checker sees an exhaustive return.
        if last_http_exc is not None:
            raise last_http_exc
        raise GmgnTransportError("GMGN request failed at the transport level") from last_transport_exc

    async def _send_once(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        json_body: Optional[dict[str, Any]],
        headers: dict[str, str],
    ) -> Any:
        # The timeout is enforced here uniformly for both the production
        # httpx path and any injected test client, so a slow/hanging
        # injected client is testable without needing a real socket.
        if self._http_client is not None:
            request_coro = self._http_client.request(
                method, url, params=params, headers=headers, json=json_body
            )
        else:
            request_coro = self._send_via_httpx(method, url, params, json_body, headers)
        return await asyncio.wait_for(request_coro, timeout=self._timeout_seconds)

    async def _send_via_httpx(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        json_body: Optional[dict[str, Any]],
        headers: dict[str, str],
    ) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False) as client:
            return await client.request(method, url, params=params, headers=headers, json=json_body)

    def _parse_envelope(self, response: Any) -> dict[str, Any]:
        content = getattr(response, "content", None)
        if not isinstance(content, (bytes, bytearray)):
            text = getattr(response, "text", None)
            content = text.encode("utf-8") if isinstance(text, str) else b""
        if len(content) > self._max_response_bytes:
            raise GmgnMalformedResponseError("response body exceeds the maximum allowed size")
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise GmgnMalformedResponseError("response body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise GmgnMalformedResponseError("response envelope is not a JSON object")
        code = payload.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise GmgnMalformedResponseError("response envelope is missing an integer 'code'")
        if code != 0:
            message = payload.get("msg")
            raise GmgnVendorError(code, message if isinstance(message, str) else None)
        return payload
