"""Shared Home Assistant fallback helpers.

This module centralizes HA HTTP request fallback for name resolution issues.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def _candidate_ha_base_urls(base_url: str) -> list[str]:
    primary = str(base_url or "").rstrip("/")
    candidates: list[str] = []
    if primary:
        candidates.append(primary)

    parsed = urlparse(primary)
    if parsed.hostname == "homeassistant.local":
        candidates.extend([
            "http://localhost:8123",
            "http://127.0.0.1:8123",
            "http://homeassistant:8123",
        ])

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _is_name_resolution_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "name or service not known" in message
        or "failed to resolve" in message
        or "name resolution" in message
    )


def request_ha_with_fallback(
    method: str,
    base_url: str,
    path: str,
    headers: dict[str, str],
    timeout: float,
    json_body: dict[str, Any] | None = None,
) -> requests.Response:
    candidates = _candidate_ha_base_urls(base_url)
    last_error: Exception | None = None
    for idx, base in enumerate(candidates):
        url = f"{base}{path}"
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
            if idx > 0:
                logger.warning("HA API fallback succeeded via %s", base)
            return resp
        except requests.RequestException as exc:
            last_error = exc
            if idx == 0 and _is_name_resolution_error(exc) and len(candidates) > 1:
                logger.warning(
                    "HA API primary host resolve failed via %s, trying fallbacks: %s",
                    base,
                    ", ".join(candidates[1:]),
                )
            logger.warning("HA API request failed via %s: %s", base, exc)

    raise requests.RequestException(last_error)


