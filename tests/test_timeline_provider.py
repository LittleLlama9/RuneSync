import pytest

from lcu import LCUConnectionError
from riot_api import RiotApiNotFoundError
from timeline_provider import (
    LcuTimelineProvider,
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


class FakeLcuClient:
    def __init__(self, match_payload, timeline_payload):
        self._match_payload = match_payload
        self._timeline_payload = timeline_payload
        self.calls = []

    def get_match_details(self, game_id):
        self.calls.append(("match", game_id))
        if isinstance(self._match_payload, Exception):
            raise self._match_payload
        return self._match_payload

    def get_match_timeline(self, game_id):
        self.calls.append(("timeline", game_id))
        if isinstance(self._timeline_payload, Exception):
            raise self._timeline_payload
        return self._timeline_payload


def _lcu_match_payload(game_id=123456, duration=120):
    return {
        "gameId": game_id,
        "gameDuration": duration,
        "participants": [
            {"participantId": participant_id}
            for participant_id in range(1, 11)
        ],
    }


def _lcu_timeline_payload(duration=120):
    participant_frames = {
        str(participant_id): {"participantId": participant_id}
        for participant_id in range(1, 11)
    }
    return {
        "frames": [
            {
                "timestamp": timestamp,
                "participantFrames": participant_frames,
                "events": [],
            }
            for timestamp in (0, 60000, duration * 1000)
        ],
    }


def test_lcu_provider_returns_validated_complete_timeline():
    client = FakeLcuClient(_lcu_match_payload(), _lcu_timeline_payload())
    provider = LcuTimelineProvider(client)

    payload = provider.fetch_match_timeline("NA1_123456")

    assert payload.source == "lcu_timeline"
    assert payload.provenance.match_id == "NA1_123456"
    assert payload.provenance.platform_id == "NA1"
    assert payload.provenance.regional_route == "LOCAL"
    assert payload.completeness == 1.0
    assert client.calls == [("match", 123456), ("timeline", 123456)]


def test_lcu_provider_reuses_supplied_match_payload():
    match_payload = _lcu_match_payload()
    client = FakeLcuClient(AssertionError("must not fetch match"), _lcu_timeline_payload())
    provider = LcuTimelineProvider(client)

    payload = provider.fetch_match_timeline(
        123456, match_payload=match_payload,
    )

    assert payload.provenance.match_id == "123456"
    assert client.calls == [("timeline", 123456)]


def test_lcu_provider_marks_missing_frames_as_partial():
    timeline = _lcu_timeline_payload()
    timeline["frames"].pop(1)
    provider = LcuTimelineProvider(
        FakeLcuClient(_lcu_match_payload(), timeline),
    )

    payload = provider.fetch_match_timeline(123456)

    assert 0 < payload.completeness < 1


def test_lcu_provider_rejects_mismatched_game_and_participants():
    provider = LcuTimelineProvider(
        FakeLcuClient(_lcu_match_payload(game_id=999), _lcu_timeline_payload()),
    )
    with pytest.raises(TimelineProviderValidationError):
        provider.fetch_match_timeline(123456)

    invalid_match = _lcu_match_payload()
    invalid_match["participants"].pop()
    provider = LcuTimelineProvider(
        FakeLcuClient(invalid_match, _lcu_timeline_payload()),
    )
    with pytest.raises(TimelineProviderValidationError):
        provider.fetch_match_timeline(123456)


def test_lcu_provider_wraps_lcu_errors():
    client = FakeLcuClient(
        LCUConnectionError("unavailable"), _lcu_timeline_payload(),
    )
    provider = LcuTimelineProvider(client)

    with pytest.raises(TimelineProviderUpstreamError) as exc:
        provider.fetch_match_timeline(123456)
    assert isinstance(exc.value.__cause__, LCUConnectionError)


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
