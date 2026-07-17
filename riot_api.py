"""Typed Riot Match-V5 client backed by urllib."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional


# Riot's edge (Cloudflare) rejects requests carrying urllib's default
# ``Python-urllib/x.y`` User-Agent with HTTP 403, even when the API key is
# valid. Sending an explicit User-Agent avoids the spurious auth failure.
USER_AGENT = "RuneSync/1.0 (+https://github.com/RuneSync)"


REGIONAL_ROUTES = {
    "BR1": "AMERICAS",
    "LA1": "AMERICAS",
    "LA2": "AMERICAS",
    "NA1": "AMERICAS",
    "OC1": "AMERICAS",
    "EUN1": "EUROPE",
    "EUW1": "EUROPE",
    "RU": "EUROPE",
    "TR1": "EUROPE",
    "JP1": "ASIA",
    "KR": "ASIA",
    "PH2": "SEA",
    "SG2": "SEA",
    "TH2": "SEA",
    "TW2": "SEA",
    "VN2": "SEA",
}


class RiotApiError(Exception):
    """Base class for sanitized Riot API failures."""

    def __init__(self, message: str, match_id: Optional[str] = None):
        super().__init__(message)
        self.match_id = match_id


class RiotApiConfigError(RiotApiError):
    """Raised when the client is misconfigured."""


class RiotApiAuthError(RiotApiError):
    """Raised for authentication or authorization failures."""


class RiotApiNotFoundError(RiotApiError):
    """Raised when a Match-V5 resource is missing."""


class RiotApiRateLimitError(RiotApiError):
    """Raised when the Riot API rate limit is exceeded."""

    def __init__(self, message: str, match_id: Optional[str] = None, retry_after: float = 0.0):
        super().__init__(message, match_id=match_id)
        self.retry_after = retry_after


class RiotApiTransientError(RiotApiError):
    """Raised for retryable upstream 5xx failures."""

    def __init__(self, message: str, match_id: Optional[str] = None, retry_after: float = 0.0):
        super().__init__(message, match_id=match_id)
        self.retry_after = retry_after


class RiotApiPayloadError(RiotApiError):
    """Raised when a response payload is malformed."""


class RiotApiTransportError(RiotApiError):
    """Raised for socket and urllib transport failures."""


def regional_route_for_platform(platform_id: str) -> str:
    normalized = (platform_id or "").strip().upper()
    try:
        return REGIONAL_ROUTES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported Riot platform ID: {platform_id!r}") from exc


def parse_match_v5_id(match_id: str) -> tuple[str, int]:
    if not isinstance(match_id, str):
        raise ValueError("Match ID must be a string.")
    platform_id, separator, game_id_text = match_id.strip().upper().partition("_")
    if separator != "_" or not game_id_text.isdigit():
        raise ValueError("Match ID must look like PLATFORM_123456789.")
    regional_route_for_platform(platform_id)
    game_id = int(game_id_text)
    if game_id <= 0:
        raise ValueError("Game ID must be positive.")
    return platform_id, game_id


def build_match_v5_id(platform_id: str, game_id: int) -> str:
    platform, numeric_game_id = parse_match_v5_id(f"{platform_id}_{game_id}")
    return f"{platform}_{numeric_game_id}"


class RiotApiClient:
    """Minimal Riot Match-V5 client with typed failures."""

    def __init__(
        self,
        key_supplier: Callable[[], str],
        opener: Optional[Callable[..., Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        timeout: float = 10.0,
        max_retries: int = 1,
        max_retry_after: float = 5.0,
    ):
        if not callable(key_supplier):
            raise TypeError("key_supplier must be callable.")
        self._key_supplier = key_supplier
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._max_retry_after = max(0.0, float(max_retry_after))

    def get_match(self, match_id: str) -> dict:
        return self._request_json(match_id, "match")

    def get_timeline(self, match_id: str) -> dict:
        return self._request_json(match_id, "timeline")

    def _request_json(self, match_id: str, resource: str) -> dict:
        normalized_match_id = build_match_v5_id(*parse_match_v5_id(match_id))
        platform_id, _ = parse_match_v5_id(normalized_match_id)
        route = regional_route_for_platform(platform_id).lower()
        url = f"https://{route}.api.riotgames.com/lol/match/v5/matches/{normalized_match_id}"
        if resource == "timeline":
            url = f"{url}/timeline"

        retries_remaining = self._max_retries
        while True:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "X-Riot-Token": self._get_key(),
                },
            )
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                retry_after = _extract_retry_after(exc, self._max_retry_after)
                if exc.code in (401, 403):
                    raise RiotApiAuthError(
                        f"Riot API authentication failed for {normalized_match_id}.",
                        match_id=normalized_match_id,
                    ) from None
                if exc.code == 404:
                    raise RiotApiNotFoundError(
                        f"Riot Match-V5 resource was not found for {normalized_match_id}.",
                        match_id=normalized_match_id,
                    ) from None
                if exc.code == 429:
                    if retries_remaining > 0:
                        retries_remaining -= 1
                        if retry_after > 0:
                            self._sleep(retry_after)
                        continue
                    raise RiotApiRateLimitError(
                        f"Riot API rate limit blocked {normalized_match_id}.",
                        match_id=normalized_match_id,
                        retry_after=retry_after,
                    ) from None
                if 500 <= exc.code <= 599:
                    if retries_remaining > 0:
                        retries_remaining -= 1
                        if retry_after > 0:
                            self._sleep(retry_after)
                        continue
                    raise RiotApiTransientError(
                        f"Riot Match-V5 is temporarily unavailable for {normalized_match_id}.",
                        match_id=normalized_match_id,
                        retry_after=retry_after,
                    ) from None
                raise RiotApiError(
                    f"Unexpected Riot API response {exc.code} for {normalized_match_id}.",
                    match_id=normalized_match_id,
                ) from None
            except (urllib.error.URLError, socket.timeout, OSError):
                raise RiotApiTransportError(
                    f"Transport error while requesting Riot Match-V5 for {normalized_match_id}.",
                    match_id=normalized_match_id,
                ) from None

            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RiotApiPayloadError(
                    f"Malformed Riot Match-V5 JSON for {normalized_match_id}.",
                    match_id=normalized_match_id,
                ) from None
            if not isinstance(decoded, dict):
                raise RiotApiPayloadError(
                    f"Malformed Riot Match-V5 payload for {normalized_match_id}.",
                    match_id=normalized_match_id,
                )
            return decoded

    def _get_key(self) -> str:
        key = self._key_supplier()
        if not isinstance(key, str) or not key.strip():
            raise RiotApiConfigError("Riot API key supplier returned no usable key.")
        return key.strip()


def _extract_retry_after(error: urllib.error.HTTPError, maximum: float) -> float:
    headers = getattr(error, "headers", None) or getattr(error, "hdrs", None) or {}
    raw_value = headers.get("Retry-After")
    if raw_value is None:
        return 0.0
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return 0.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(float(seconds), maximum))
