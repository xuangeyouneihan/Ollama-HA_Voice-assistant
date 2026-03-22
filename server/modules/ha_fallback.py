"""Shared Home Assistant fallback helpers.

This module centralizes:
- HA HTTP request fallback for name resolution issues
- Assist pipeline output normalization and fallback policy
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import requests

from modules import ha_client

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


def normalize_assist_result(assist_result: dict[str, Any]) -> dict[str, Any]:
    ok = bool(assist_result.get("ok", False))
    message = str(assist_result.get("message") or "").strip()
    transcript = str(assist_result.get("transcript") or "").strip()
    response_text = str(assist_result.get("response_text") or "").strip()

    tts_audio = assist_result.get("tts_audio") or b""
    tts_rate = int(assist_result.get("tts_sample_rate") or 0)
    tts_width = int(assist_result.get("tts_sample_width") or 0)
    tts_channels = int(assist_result.get("tts_channels") or 0)

    has_playable_tts = bool(tts_audio and tts_rate > 0 and tts_width > 0 and tts_channels > 0)
    fallback_text = response_text if response_text else "Okay"

    return {
        "ok": ok,
        "message": message,
        "transcript": transcript,
        "response_text": response_text,
        "tts_audio": tts_audio,
        "tts_sample_rate": tts_rate,
        "tts_sample_width": tts_width,
        "tts_channels": tts_channels,
        "has_playable_tts": has_playable_tts,
        "fallback_text": fallback_text,
    }


async def run_assist_pipeline_with_fallback(audio_data: bytes, sample_rate: int) -> dict[str, Any]:
    assist_result = await ha_client.process_audio_with_assist_pipeline(
        audio_data,
        sample_rate=sample_rate,
    )
    return normalize_assist_result(assist_result)
