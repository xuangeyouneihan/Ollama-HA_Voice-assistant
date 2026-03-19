"""Home Assistant Assist pipeline client (audio-only minimal module)."""

import asyncio
import io
import json
import logging
import socket
import wave
from urllib.parse import urlparse, urlunparse

import av
import numpy as np
import requests
import websockets

from config_loader import get_config

logger = logging.getLogger(__name__)

cfg = get_config()
audio_cfg = cfg.get("audio", {}) if cfg else {}
ha_cfg = cfg.get("home_assistant", {}) if cfg else {}

HA_URL = ha_cfg.get("url", "http://homeassistant.local:8123")
HA_TOKEN = ha_cfg.get("token", "YOUR_HA_TOKEN")
ASSIST_PIPELINE_ID = str(ha_cfg.get("assist_pipeline_id", "")).strip()
ASSIST_INPUT_SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))
ASSIST_AUDIO_TIMEOUT_S = float(ha_cfg.get("assist_audio_timeout_s", 45))


def _candidate_ha_base_urls() -> list[str]:
    primary = (HA_URL or "").rstrip("/")
    candidates = []
    if primary:
        candidates.append(primary)

    try:
        parsed = urlparse(primary)
    except Exception:
        parsed = None

    fallback_bases = [
        "http://localhost:8123",
        "http://127.0.0.1:8123",
        "http://homeassistant:8123",
    ]

    if parsed and parsed.hostname == "homeassistant.local":
        candidates.extend(fallback_bases)

    deduped = []
    seen = set()
    for url in candidates:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _is_name_resolution_error(exc: Exception) -> bool:
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == -2:
        return True
    message = str(exc).lower()
    return (
        "name or service not known" in message
        or "failed to resolve" in message
        or "name resolution" in message
    )


def _build_ws_url(base_http_url: str) -> str:
    parsed = urlparse((base_http_url or "").rstrip("/"))
    if not parsed.netloc:
        raise ValueError(f"invalid Home Assistant URL: {base_http_url}")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/api/websocket", "", "", ""))


def _build_candidate_tts_urls(tts_url: str) -> list[str]:
    raw = str(tts_url or "").strip()
    if not raw:
        return []

    parsed = urlparse(raw)
    candidates = []

    if parsed.scheme and parsed.netloc:
        candidates.append(raw)
        if parsed.hostname == "homeassistant.local":
            for base in _candidate_ha_base_urls():
                parsed_base = urlparse(base)
                if not parsed_base.scheme or not parsed_base.netloc:
                    continue
                alt = urlunparse(
                    (
                        parsed_base.scheme,
                        parsed_base.netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
                candidates.append(alt)
    else:
        for base in _candidate_ha_base_urls():
            candidates.append(f"{base.rstrip('/')}/{raw.lstrip('/')}")

    deduped = []
    seen = set()
    for url in candidates:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _extract_speech_text(intent_output: dict | None) -> str:
    intent_output = intent_output or {}
    response = intent_output.get("response") or {}
    speech = response.get("speech") or {}
    if not isinstance(speech, dict):
        return ""
    plain_raw = speech.get("plain")
    ssml_raw = speech.get("ssml")
    plain = plain_raw if isinstance(plain_raw, dict) else {}
    ssml = ssml_raw if isinstance(ssml_raw, dict) else {}
    return str(plain.get("speech") or ssml.get("speech") or "").strip()


def _decode_wav_if_possible(audio_bytes: bytes) -> tuple[bytes, int, int, int] | None:
    if not audio_bytes or len(audio_bytes) < 44:
        return None
    if not audio_bytes.startswith(b"RIFF"):
        return None

    with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        pcm = wf.readframes(wf.getnframes())
    return pcm, sample_rate, sample_width, channels


def _looks_like_mp3(audio_bytes: bytes) -> bool:
    if not audio_bytes:
        return False
    if audio_bytes.startswith(b"ID3"):
        return True
    if len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and (audio_bytes[1] & 0xE0) == 0xE0:
        return True
    return False


def _looks_like_ogg(audio_bytes: bytes) -> bool:
    return bool(audio_bytes and audio_bytes.startswith(b"OggS"))


def _layout_channel_count(layout_obj) -> int:
    if layout_obj is None:
        return 0
    channels_obj = getattr(layout_obj, "channels", None)
    if isinstance(channels_obj, int):
        return channels_obj
    if isinstance(channels_obj, (list, tuple)):
        return len(channels_obj)
    try:
        return int(channels_obj or 0)
    except Exception:
        return 0


def _decode_compressed_audio_if_possible(audio_bytes: bytes, mime_type: str = "") -> tuple[bytes, int, int, int] | None:
    if not audio_bytes:
        return None

    lower_mime = str(mime_type or "").lower()
    should_try = (
        "audio/mpeg" in lower_mime
        or "audio/mp3" in lower_mime
        or "audio/ogg" in lower_mime
        or "vorbis" in lower_mime
        or _looks_like_mp3(audio_bytes)
        or _looks_like_ogg(audio_bytes)
    )
    if not should_try:
        return None

    try:
        with av.open(io.BytesIO(audio_bytes), mode="r") as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                return None

            pcm_chunks = []
            sample_rate = int(getattr(stream, "rate", 0) or 0)
            channels = 0

            for frame in container.decode(stream):
                frame_rate = int(getattr(frame, "sample_rate", 0) or sample_rate or 44100)
                if frame_rate <= 0:
                    frame_rate = 44100

                frame_channels = _layout_channel_count(getattr(frame, "layout", None))
                if frame_channels <= 0:
                    frame_channels = _layout_channel_count(getattr(stream, "layout", None)) or 2

                target_layout = "mono" if frame_channels == 1 else "stereo"
                resampler = av.audio.resampler.AudioResampler(
                    format="s16",
                    layout=target_layout,
                    rate=frame_rate,
                )

                resampled = resampler.resample(frame)
                if resampled is None:
                    continue
                if not isinstance(resampled, list):
                    resampled = [resampled]

                for out in resampled:
                    arr = out.to_ndarray()
                    if arr is None:
                        continue

                    out_channels = _layout_channel_count(getattr(out, "layout", None)) or frame_channels
                    if out_channels <= 0:
                        out_channels = 2

                    if arr.dtype != np.int16:
                        if np.issubdtype(arr.dtype, np.floating):
                            arr = np.clip(arr, -1.0, 1.0)
                            arr = (arr * 32767.0).astype(np.int16)
                        else:
                            arr = arr.astype(np.int16)

                    if arr.ndim == 1:
                        channels = out_channels
                        pcm_chunks.append(arr.reshape(-1).astype(np.int16, copy=False).tobytes())
                        sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)
                        continue

                    if arr.ndim == 2 and arr.shape[0] == 1 and out_channels > 1:
                        channels = out_channels
                        pcm_chunks.append(arr.reshape(-1).astype(np.int16, copy=False).tobytes())
                        sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)
                        continue

                    if arr.ndim == 2:
                        channels = out_channels if out_channels > 0 else int(arr.shape[0])
                        pcm_chunks.append(arr.T.astype(np.int16, copy=False).tobytes())
                        sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)
                        continue

                    channels = out_channels
                    pcm_chunks.append(arr.reshape(-1).astype(np.int16, copy=False).tobytes())
                    sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)

            if not pcm_chunks:
                return None

            pcm = b"".join(pcm_chunks)
            if sample_rate <= 0:
                sample_rate = 44100
            if channels <= 0:
                channels = 2

        sample_width = 2
        return pcm, sample_rate, sample_width, channels
    except Exception as exc:
        logger.warning("Failed to decode compressed TTS audio with PyAV: %s", exc)
        return None


def _decode_tts_audio_if_possible(audio_bytes: bytes, mime_type: str = "") -> tuple[bytes, int, int, int] | None:
    decoded = _decode_wav_if_possible(audio_bytes)
    if decoded is not None:
        return decoded
    return _decode_compressed_audio_if_possible(audio_bytes, mime_type=mime_type)


def _download_tts_audio(url: str, timeout: int = 20) -> tuple[bytes, str]:
    last_error = None
    auth_headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    candidates = _build_candidate_tts_urls(url)
    for idx, candidate in enumerate(candidates):
        try:
            resp = requests.get(candidate, headers=auth_headers, timeout=timeout)
            resp.raise_for_status()
            if idx > 0:
                logger.warning("HA TTS download fallback succeeded via %s", candidate)
            return resp.content, str(resp.headers.get("Content-Type", "")).strip()
        except requests.RequestException as exc:
            last_error = exc
            if idx == 0 and _is_name_resolution_error(exc) and len(candidates) > 1:
                logger.warning(
                    "HA TTS primary host resolve failed via %s, trying fallbacks: %s",
                    candidate,
                    ", ".join(candidates[1:]),
                )
            logger.warning("Failed to download HA TTS audio via %s: %s", candidate, exc)
    raise last_error if last_error else RuntimeError("failed to download HA TTS audio")


async def process_audio_with_assist_pipeline(audio_data: bytes, sample_rate: int | None = None) -> dict:
    if not audio_data:
        return {
            "ok": False,
            "message": "empty audio input",
            "transcript": "",
            "response_text": "",
            "tts_audio": b"",
            "tts_mime_type": "",
        }

    run_payload = {
        "type": "assist_pipeline/run",
        "start_stage": "stt",
        "end_stage": "tts",
        "input": {
            "sample_rate": int(sample_rate if sample_rate is not None else ASSIST_INPUT_SAMPLE_RATE),
        },
    }
    if ASSIST_PIPELINE_ID:
        run_payload["pipeline"] = ASSIST_PIPELINE_ID

    run_id = 1
    transcript = ""
    response_text = ""
    tts_url = ""
    tts_mime_type = ""
    run_success = False
    last_error = None

    candidate_bases = _candidate_ha_base_urls()
    candidate_ws_urls = []
    for _base in candidate_bases:
        try:
            candidate_ws_urls.append(_build_ws_url(_base))
        except Exception:
            continue

    for idx, base in enumerate(candidate_bases):
        ws_url = _build_ws_url(base)
        try:
            if idx > 0:
                logger.warning("Assist pipeline fallback attempt via %s", ws_url)

            attempt_transcript = ""
            attempt_response_text = ""
            attempt_tts_url = ""
            attempt_tts_mime_type = ""
            stt_handler_id = None
            stt_started = False
            audio_sent = False
            saw_run_end = False

            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                auth_required = await asyncio.wait_for(ws.recv(), timeout=10)
                auth_required_msg = json.loads(auth_required)
                if auth_required_msg.get("type") != "auth_required":
                    raise RuntimeError("unexpected websocket auth challenge")

                await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                auth_result = await asyncio.wait_for(ws.recv(), timeout=10)
                auth_result_msg = json.loads(auth_result)
                if auth_result_msg.get("type") != "auth_ok":
                    raise RuntimeError(f"websocket auth failed: {auth_result_msg}")

                run_payload_with_id = dict(run_payload)
                run_payload_with_id["id"] = run_id
                await ws.send(json.dumps(run_payload_with_id))

                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=ASSIST_AUDIO_TIMEOUT_S)
                    if isinstance(raw, bytes):
                        continue

                    msg = json.loads(raw)
                    msg_type = msg.get("type")

                    if msg_type == "result" and msg.get("id") == run_id and not msg.get("success", False):
                        raise RuntimeError(f"assist pipeline run failed: {msg}")

                    if msg_type != "event" or msg.get("id") != run_id:
                        continue

                    event = msg.get("event") or {}
                    event_type = event.get("type")
                    data = event.get("data") or {}

                    if event_type == "run-start":
                        runner_data = data.get("runner_data") or {}
                        if stt_handler_id is None:
                            stt_handler_id = runner_data.get("stt_binary_handler_id")
                        tts_output = data.get("tts_output") or {}
                        if not attempt_tts_url:
                            attempt_tts_url = str(tts_output.get("url") or "").strip()
                        if not attempt_tts_mime_type:
                            attempt_tts_mime_type = str(tts_output.get("mime_type") or "").strip()

                    elif event_type == "stt-start":
                        stt_started = True

                    elif event_type == "stt-end":
                        stt_output = data.get("stt_output") or {}
                        attempt_transcript = str(stt_output.get("text") or "").strip()

                    elif event_type == "intent-end":
                        attempt_response_text = _extract_speech_text(data.get("intent_output"))

                    elif event_type == "tts-end":
                        attempt_tts_url = str(data.get("url") or attempt_tts_url or "").strip()
                        attempt_tts_mime_type = str(data.get("mime_type") or attempt_tts_mime_type or "").strip()

                    elif event_type == "error":
                        code = str(data.get("code") or "unknown")
                        message = str(data.get("message") or "")
                        raise RuntimeError(f"assist pipeline error [{code}]: {message}")

                    elif event_type == "run-end":
                        saw_run_end = True
                        break

                    if stt_started and stt_handler_id is not None and not audio_sent:
                        handler_byte = bytes([int(stt_handler_id)])
                        chunk_size = 2048
                        for i in range(0, len(audio_data), chunk_size):
                            await ws.send(handler_byte + audio_data[i : i + chunk_size])
                        await ws.send(handler_byte)
                        audio_sent = True

                if not saw_run_end:
                    raise RuntimeError("assist pipeline did not complete with run-end")

                transcript = attempt_transcript
                response_text = attempt_response_text
                tts_url = attempt_tts_url
                tts_mime_type = attempt_tts_mime_type
                if idx > 0:
                    logger.warning("Assist pipeline fallback succeeded via %s", ws_url)
                run_success = True
                break
        except Exception as exc:
            last_error = exc
            if idx == 0 and _is_name_resolution_error(exc) and len(candidate_ws_urls) > 1:
                logger.warning(
                    "Assist pipeline primary host resolve failed via %s, trying fallbacks: %s",
                    ws_url,
                    ", ".join(candidate_ws_urls[1:]),
                )
            logger.warning("Assist pipeline attempt failed via %s: %s", ws_url, exc)

    if not run_success:
        return {
            "ok": False,
            "message": f"assist pipeline request failed: {last_error or 'unknown error'}",
            "transcript": "",
            "response_text": "",
            "tts_audio": b"",
            "tts_mime_type": "",
        }

    tts_audio = b""
    tts_sample_rate = 0
    tts_sample_width = 0
    tts_channels = 0
    if tts_url:
        try:
            downloaded, downloaded_mime = _download_tts_audio(tts_url, timeout=20)
            if downloaded_mime and not tts_mime_type:
                tts_mime_type = downloaded_mime

            decoded = _decode_tts_audio_if_possible(downloaded, mime_type=tts_mime_type)
            if decoded is not None:
                tts_audio, tts_sample_rate, tts_sample_width, tts_channels = decoded
                tts_mime_type = "audio/pcm"
            else:
                tts_audio = downloaded
        except Exception as exc:
            logger.warning("Failed to fetch/decode HA TTS audio: %s", exc)

    return {
        "ok": True,
        "message": "ok",
        "transcript": transcript,
        "response_text": response_text,
        "tts_audio": tts_audio,
        "tts_mime_type": tts_mime_type,
        "tts_sample_rate": tts_sample_rate,
        "tts_sample_width": tts_sample_width,
        "tts_channels": tts_channels,
    }


def process_audio_with_assist_pipeline_sync(audio_data: bytes, sample_rate: int | None = None) -> dict:
    return asyncio.run(process_audio_with_assist_pipeline(audio_data, sample_rate=sample_rate))
