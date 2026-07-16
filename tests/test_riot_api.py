import json
import urllib.error

import pytest

from riot_api import (
    RiotApiAuthError,
    RiotApiClient,
    RiotApiNotFoundError,
    RiotApiPayloadError,
    RiotApiRateLimitError,
    RiotApiTransientError,
    RiotApiTransportError,
    build_match_v5_id,
    regional_route_for_platform,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _json_response(payload) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def _http_error(code: int, body: str = "error", headers=None) -> urllib.error.HTTPError:
    response_headers = headers or {}
    error = urllib.error.HTTPError(
        url="https://example.invalid",
        code=code,
        msg="error",
        hdrs=response_headers,
        fp=None,
    )
    error.read = lambda: body.encode("utf-8")
    return error


@pytest.mark.parametrize(
    ("platform_id", "expected_route"),
    [
        ("NA1", "AMERICAS"),
        ("BR1", "AMERICAS"),
        ("EUW1", "EUROPE"),
        ("TR1", "EUROPE"),
        ("KR", "ASIA"),
        ("JP1", "ASIA"),
        ("SG2", "SEA"),
        ("TW2", "SEA"),
    ],
)
def test_regional_route_mapping(platform_id, expected_route):
    assert regional_route_for_platform(platform_id) == expected_route


def test_build_match_v5_id_normalizes_and_validates():
    assert build_match_v5_id("na1", 123456) == "NA1_123456"
    with pytest.raises(ValueError):
        build_match_v5_id("unknown", 123456)
    with pytest.raises(ValueError):
        build_match_v5_id("NA1", 0)


def test_get_match_sends_riot_header_and_returns_payload():
    captured = {}

    def opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _json_response({"metadata": {"matchId": "NA1_123456"}})

    client = RiotApiClient(lambda: "test-token", opener=opener, timeout=7.5)
    payload = client.get_match("na1_123456")

    assert payload["metadata"]["matchId"] == "NA1_123456"
    assert captured["url"] == (
        "https://americas.api.riotgames.com/lol/match/v5/matches/NA1_123456"
    )
    assert captured["headers"]["X-riot-token"] == "test-token"
    assert captured["timeout"] == 7.5


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_errors_are_typed_and_redacted(status_code):
    token = "super-secret-token"
    client = RiotApiClient(
        lambda: token,
        opener=lambda request, timeout=None: (_ for _ in ()).throw(
            _http_error(status_code, body=token)
        ),
    )

    with pytest.raises(RiotApiAuthError) as exc:
        client.get_match("NA1_123456")
    assert token not in str(exc.value)


def test_not_found_is_typed():
    client = RiotApiClient(
        lambda: "token",
        opener=lambda request, timeout=None: (_ for _ in ()).throw(_http_error(404)),
    )

    with pytest.raises(RiotApiNotFoundError):
        client.get_match("NA1_123456")


def test_rate_limit_retries_with_retry_after():
    calls = {"count": 0}
    sleeps = []

    def opener(request, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429, headers={"Retry-After": "2"})
        return _json_response({"metadata": {"matchId": "NA1_123456"}})

    client = RiotApiClient(
        lambda: "token",
        opener=opener,
        sleep=sleeps.append,
        max_retries=1,
        max_retry_after=10,
    )

    payload = client.get_match("NA1_123456")

    assert payload["metadata"]["matchId"] == "NA1_123456"
    assert calls["count"] == 2
    assert sleeps == [2.0]


def test_rate_limit_exhaustion_raises_typed_error():
    client = RiotApiClient(
        lambda: "token",
        opener=lambda request, timeout=None: (_ for _ in ()).throw(
            _http_error(429, headers={"Retry-After": "9"})
        ),
        max_retries=0,
        max_retry_after=3,
    )

    with pytest.raises(RiotApiRateLimitError) as exc:
        client.get_match("NA1_123456")
    assert exc.value.retry_after == 3.0


def test_transient_server_error_is_typed():
    client = RiotApiClient(
        lambda: "token",
        opener=lambda request, timeout=None: (_ for _ in ()).throw(
            _http_error(503, headers={"Retry-After": "1"})
        ),
        max_retries=0,
    )

    with pytest.raises(RiotApiTransientError) as exc:
        client.get_timeline("KR_123456")
    assert exc.value.retry_after == 1.0


def test_malformed_json_and_payload_are_typed():
    json_client = RiotApiClient(
        lambda: "token",
        opener=lambda request, timeout=None: FakeResponse(b"{not-json"),
    )
    payload_client = RiotApiClient(
        lambda: "token",
        opener=lambda request, timeout=None: _json_response(["wrong-shape"]),
    )

    with pytest.raises(RiotApiPayloadError):
        json_client.get_match("NA1_123456")
    with pytest.raises(RiotApiPayloadError):
        payload_client.get_timeline("NA1_123456")


def test_transport_errors_are_redacted():
    token = "super-secret-token"
    client = RiotApiClient(
        lambda: token,
        opener=lambda request, timeout=None: (_ for _ in ()).throw(OSError(token)),
    )

    with pytest.raises(RiotApiTransportError) as exc:
        client.get_match("NA1_123456")
    assert token not in str(exc.value)
