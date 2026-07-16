import pytest

from riot_api import RiotApiNotFoundError
from timeline_provider import (
    MatchTimelinePayload,
    PRIVATE_MATCH_V5_ENV,
    RiotMatchV5Provider,
    TimelineProviderUpstreamError,
    TimelineProviderValidationError,
    is_private_match_v5_enabled,
)


class FakeClient:
    def __init__(self, match_payload, timeline_payload):
        self._match_payload = match_payload
        self._timeline_payload = timeline_payload
        self.calls = []

    def get_match(self, match_id):
        self.calls.append(("match", match_id))
        if isinstance(self._match_payload, Exception):
            raise self._match_payload
        return self._match_payload

    def get_timeline(self, match_id):
        self.calls.append(("timeline", match_id))
        if isinstance(self._timeline_payload, Exception):
            raise self._timeline_payload
        return self._timeline_payload


def _match_payload(match_id="NA1_123456", participants=None):
    participants = participants or ["p1", "p2"]
    return {
        "metadata": {"matchId": match_id, "participants": participants},
        "info": {
            "participants": [
                {"puuid": participant}
                for participant in participants
            ]
        },
    }


def _timeline_payload(match_id="NA1_123456", participants=None):
    participants = participants or ["p1", "p2"]
    return {
        "metadata": {"matchId": match_id, "participants": participants},
        "info": {"frames": []},
    }


def test_provider_returns_complete_match_timeline_payload():
    client = FakeClient(_match_payload(), _timeline_payload())
    provider = RiotMatchV5Provider(client)

    payload = provider.fetch_match_timeline("na1_123456")

    assert isinstance(payload, MatchTimelinePayload)
    assert payload.source == "match_v5"
    assert payload.provenance.source == "match_v5"
    assert payload.provenance.match_id == "NA1_123456"
    assert payload.provenance.platform_id == "NA1"
    assert payload.provenance.regional_route == "AMERICAS"
    assert client.calls == [("match", "NA1_123456"), ("timeline", "NA1_123456")]


def test_provider_rejects_mismatched_metadata_match_id():
    client = FakeClient(_match_payload(match_id="NA1_999999"), _timeline_payload())
    provider = RiotMatchV5Provider(client)

    with pytest.raises(TimelineProviderValidationError):
        provider.fetch_match_timeline("NA1_123456")


def test_provider_rejects_participant_mismatch():
    client = FakeClient(
        _match_payload(participants=["p1", "p2"]),
        _timeline_payload(participants=["p1", "other"]),
    )
    provider = RiotMatchV5Provider(client)

    with pytest.raises(TimelineProviderValidationError):
        provider.fetch_match_timeline("NA1_123456")


def test_provider_wraps_riot_api_errors():
    client = FakeClient(RiotApiNotFoundError("missing"), _timeline_payload())
    provider = RiotMatchV5Provider(client)

    with pytest.raises(TimelineProviderUpstreamError) as exc:
        provider.fetch_match_timeline("NA1_123456")
    assert isinstance(exc.value.__cause__, RiotApiNotFoundError)


def test_feature_gate_requires_explicit_opt_in():
    assert is_private_match_v5_enabled({}) is False
    assert is_private_match_v5_enabled({PRIVATE_MATCH_V5_ENV: "0"}) is False
    assert is_private_match_v5_enabled({PRIVATE_MATCH_V5_ENV: "true"}) is True
    assert is_private_match_v5_enabled({PRIVATE_MATCH_V5_ENV: "YES"}) is True
