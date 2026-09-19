"""Tests for apex.research.gmgn.transport (first-party, read-only async HTTPS
client fixed to `https://openapi.gmgn.ai`).

Covers: the exact 17 method/path pairs plus representative denial from every
excluded endpoint family (trade/swap, quote/gas, order, signing/private-key,
wallet holdings/follow, token cooking) with zero I/O attempted; the
timestamp/client_id/API-key auth shape with no signature support; refusal of
disallowed GMGN private/signing/wallet/seed/mnemonic/passphrase/secret
environment variable names; API-key redaction; timeout enforcement for both
an injected client and the production httpx path; redirect failure without
retry; exactly one bounded retry for 429/transient 5xx and no retry for
other statuses; the rolling-window rate limiter; and oversized/malformed/
non-object/nonzero-code envelope rejection. Every test uses an injected
fake HTTP client (or a monkeypatched httpx.AsyncClient constructor) — no
real socket is ever opened.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from apex.research.gmgn import transport as transport_module
from apex.research.gmgn.contract import ALLOWED_ENDPOINTS, GET, POST
from apex.research.gmgn.transport import (
    GmgnCredentialConfigurationError,
    GmgnEndpointNotAllowedError,
    GmgnHttpError,
    GmgnMalformedResponseError,
    GmgnRateLimiter,
    GmgnRedirectResponseError,
    GmgnResearchTransport,
    GmgnTransportError,
    GmgnVendorError,
)

FIXED_UUID = "11111111-1111-1111-1111-111111111111"


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


async def _instant_sleep(_seconds: float) -> None:
    return None


def _instant_limiter() -> GmgnRateLimiter:
    return GmgnRateLimiter(sleep=_instant_sleep)


class FakeResponse:
    def __init__(self, status_code, payload=None, *, content=None, is_redirect=False):
        self.status_code = status_code
        self.is_redirect = is_redirect
        if content is not None:
            self.content = content
        elif payload is not None:
            self.content = json.dumps(payload).encode("utf-8")
        else:
            self.content = b""


class NetworkTouchedError(AssertionError):
    """Raised by NoIOHttpClient if a request is ever actually attempted."""


class NoIOHttpClient:
    async def request(self, *args, **kwargs):
        raise NetworkTouchedError("network was touched for a non-allowlisted endpoint")


class FakeHttpClient:
    def __init__(self, responses=None, *, delay: float = None):
        self._responses = list(responses) if responses is not None else None
        self._delay = delay
        self.calls: list[dict] = []

    async def request(self, method, url, *, params, headers, json):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params), "headers": dict(headers), "json": json}
        )
        if self._delay is not None:
            await asyncio.sleep(self._delay)
        response = self._responses.pop(0)
        return response


def _transport(*, api_key="test-key", http_client=None, clock=None, timeout_seconds=None, **overrides):
    kwargs = dict(
        api_key=api_key,
        http_client=http_client,
        clock=clock if clock is not None else FakeClock(),
        sleep=_instant_sleep,
        uuid_factory=lambda: FIXED_UUID,
        limiter=_instant_limiter(),
    )
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    kwargs.update(overrides)
    return GmgnResearchTransport(**kwargs)


class TestExactAllowlist:
    def test_allowlist_has_exactly_seventeen_pairs(self):
        assert len(ALLOWED_ENDPOINTS) == 17

    @pytest.mark.parametrize(
        "method,path",
        [
            (POST, "/v1/trade/swap"),
            (GET, "/v1/trade/quote"),
            (GET, "/v1/trade/gas"),
            (POST, "/v1/order/place"),
            (POST, "/v1/strategy/create"),
            (POST, "/v1/wallet/sign"),
            (POST, "/v1/wallet/private_key"),
            (GET, "/v1/wallet/holdings"),
            (GET, "/v1/user/follow"),
            (POST, "/v1/cooking/create"),
            (POST, "/v1/token/create"),
        ],
    )
    async def test_excluded_family_denied_before_any_io(self, method, path):
        client = NoIOHttpClient()
        transport = _transport(http_client=client)
        with pytest.raises(GmgnEndpointNotAllowedError):
            if method == GET:
                await transport.get(path)
            else:
                await transport.post(path)

    async def test_each_allowlisted_pair_is_accepted_before_io(self):
        for method, path in ALLOWED_ENDPOINTS:
            client = FakeHttpClient(responses=[FakeResponse(200, {"code": 0, "data": {}})])
            transport = _transport(http_client=client)
            if method == GET:
                result = await transport.get(path)
            else:
                result = await transport.post(path)
            assert result == {"code": 0, "data": {}}
            assert len(client.calls) == 1


class TestAuthShape:
    async def test_timestamp_client_id_api_key_present_and_no_signature(self):
        client = FakeHttpClient(responses=[FakeResponse(200, {"code": 0, "data": {}})])
        clock = FakeClock(1_700_000_123.0)
        transport = _transport(api_key="super-secret-value", http_client=client, clock=clock)
        await transport.get("/v1/token/info", params={"address": "Aabbcc"})
        call = client.calls[0]
        assert call["headers"]["X-APIKEY"] == "super-secret-value"
        assert call["params"]["timestamp"] == str(int(1_700_000_123.0))
        assert call["params"]["client_id"] == FIXED_UUID
        assert not any("sign" in key.lower() for key in call["headers"])
        assert not any("sign" in key.lower() for key in call["params"])

    def test_transport_exposes_no_signing_capability(self):
        assert not any("sign" in attr.lower() for attr in dir(GmgnResearchTransport))


class TestApiKeyRedaction:
    def test_api_key_never_appears_in_repr(self):
        transport = _transport(api_key="super-secret-value", http_client=NoIOHttpClient())
        assert "super-secret-value" not in repr(transport)

    def test_missing_api_key_raises_credential_error(self, monkeypatch):
        monkeypatch.delenv("GMGN_API_KEY", raising=False)
        with pytest.raises(GmgnCredentialConfigurationError):
            GmgnResearchTransport(api_key=None, http_client=NoIOHttpClient())


class TestDisallowedEnvironmentVariableNames:
    @pytest.mark.parametrize(
        "marker",
        ["PRIVATE", "SIGN", "SECRET", "WALLET", "SEED", "MNEMONIC", "PASSPHRASE"],
    )
    def test_disallowed_configured_env_var_name_refused(self, marker):
        with pytest.raises(GmgnCredentialConfigurationError):
            GmgnResearchTransport(api_key="k", api_key_env_var=f"GMGN_{marker}_KEY")

    def test_ambient_disallowed_gmgn_env_var_refused_even_if_not_selected(self, monkeypatch):
        monkeypatch.setenv("GMGN_WALLET_ADDRESS", "0xSomethingSensitive")
        with pytest.raises(GmgnCredentialConfigurationError) as exc_info:
            GmgnResearchTransport(api_key="k", api_key_env_var="GMGN_API_KEY")
        assert "0xSomethingSensitive" not in str(exc_info.value)

    def test_safe_env_var_name_is_accepted(self, monkeypatch):
        monkeypatch.delenv("GMGN_WALLET_ADDRESS", raising=False)
        # Must not raise: explicit api_key with the default safe env var name,
        # alongside an injected http_client (the sealed rule for explicit
        # api_key construction).
        GmgnResearchTransport(
            api_key="k", api_key_env_var="GMGN_API_KEY", http_client=NoIOHttpClient()
        )


class TestTimeoutEnforcement:
    async def test_injected_client_timeout_raises_transport_error_after_one_retry(self):
        client = FakeHttpClient(responses=[FakeResponse(200, {"code": 0}), FakeResponse(200, {"code": 0})], delay=0.2)
        transport = _transport(http_client=client, timeout_seconds=0.01)
        with pytest.raises(GmgnTransportError):
            await transport.get("/v1/token/info")
        assert len(client.calls) == 2

    async def test_production_path_passes_timeout_to_httpx_without_network(self, monkeypatch):
        captured: dict = {}

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, *, params, headers, json):
                return FakeResponse(200, {"code": 0, "data": {}})

        monkeypatch.setattr(transport_module.httpx, "AsyncClient", FakeAsyncClient)
        monkeypatch.setenv("GMGN_API_KEY", "test-key")
        transport = _transport(api_key=None, http_client=None, timeout_seconds=7.5)
        result = await transport.get("/v1/token/info")
        assert result == {"code": 0, "data": {}}
        assert captured["timeout"] == 7.5
        assert captured["follow_redirects"] is False


class TestRedirects:
    async def test_redirect_response_raises_without_retry(self):
        client = FakeHttpClient(responses=[FakeResponse(301, content=b"", is_redirect=True)])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnRedirectResponseError):
            await transport.get("/v1/token/info")
        assert len(client.calls) == 1


class TestRetryBehavior:
    async def test_transient_5xx_retried_once_then_succeeds(self):
        client = FakeHttpClient(responses=[FakeResponse(503), FakeResponse(200, {"code": 0, "data": {}})])
        transport = _transport(http_client=client)
        result = await transport.get("/v1/token/info")
        assert result == {"code": 0, "data": {}}
        assert len(client.calls) == 2

    async def test_429_retried_once_then_raises_if_still_failing(self):
        client = FakeHttpClient(responses=[FakeResponse(429), FakeResponse(429)])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnHttpError) as exc_info:
            await transport.get("/v1/token/info")
        assert exc_info.value.status_code == 429
        assert len(client.calls) == 2

    async def test_non_retryable_status_not_retried(self):
        client = FakeHttpClient(responses=[FakeResponse(404)])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnHttpError) as exc_info:
            await transport.get("/v1/token/info")
        assert exc_info.value.status_code == 404
        assert len(client.calls) == 1


class TestRateLimiter:
    async def test_third_acquire_within_window_waits(self):
        clock = FakeClock(0.0)
        sleep_calls: list[float] = []

        async def recording_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = GmgnRateLimiter(max_starts_per_second=2, clock=clock, sleep=recording_sleep)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()
        assert len(sleep_calls) == 1
        assert sleep_calls[0] > 0


class TestEnvelopeValidation:
    async def test_oversized_response_rejected(self):
        client = FakeHttpClient(responses=[FakeResponse(200, {"code": 0, "data": {}})])
        transport = _transport(http_client=client, max_response_bytes=5)
        with pytest.raises(GmgnMalformedResponseError):
            await transport.get("/v1/token/info")

    async def test_non_json_response_rejected(self):
        client = FakeHttpClient(responses=[FakeResponse(200, content=b"not json at all")])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnMalformedResponseError):
            await transport.get("/v1/token/info")

    async def test_non_object_json_response_rejected(self):
        client = FakeHttpClient(responses=[FakeResponse(200, content=b"[1,2,3]")])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnMalformedResponseError):
            await transport.get("/v1/token/info")

    async def test_missing_code_field_rejected(self):
        client = FakeHttpClient(responses=[FakeResponse(200, content=b'{"data":{}}')])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnMalformedResponseError):
            await transport.get("/v1/token/info")

    async def test_nonzero_code_raises_vendor_error(self):
        client = FakeHttpClient(responses=[FakeResponse(200, {"code": 1, "msg": "boom"})])
        transport = _transport(http_client=client)
        with pytest.raises(GmgnVendorError) as exc_info:
            await transport.get("/v1/token/info")
        assert exc_info.value.code == 1

    async def test_vendor_error_never_echoes_api_key_from_msg(self):
        secret = "super-secret-value"
        client = FakeHttpClient(
            responses=[FakeResponse(200, {"code": 1, "msg": f"your api key {secret} is invalid"})]
        )
        transport = _transport(api_key=secret, http_client=client)
        with pytest.raises(GmgnVendorError) as exc_info:
            await transport.get("/v1/token/info")
        exc = exc_info.value
        assert secret not in str(exc)
        assert secret not in repr(exc)
        assert secret not in "".join(str(v) for v in vars(exc).values())
