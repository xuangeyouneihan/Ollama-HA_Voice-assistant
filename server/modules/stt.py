"""
Speech-to-Text module for HumbleVoice
Uses Wyoming ASR services (e.g., wyoming-faster-whisper)
"""
import asyncio
import logging
import re
from config_loader import get_config
from wyoming.client import AsyncTcpClient
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStop, AudioStart, AudioChunkConverter

logger = logging.getLogger(__name__)

cfg = get_config()
stt_cfg = cfg.get("stt", {}) if cfg else {}
TARGET_RATE = int(stt_cfg.get("target_sample_rate", 16000))
TARGET_WIDTH = int(stt_cfg.get("target_sample_width", 2))
TARGET_CHANNELS = int(stt_cfg.get("target_channels", 1))
CHUNK_MS = int(stt_cfg.get("chunk_ms", 30))
READ_TIMEOUT_S = float(stt_cfg.get("read_timeout_s", 1.2))
RETRY_ON_GARBLED = bool(stt_cfg.get("retry_on_garbled", True))


def _iter_audio_chunks(audio_bytes: bytes, bytes_per_chunk: int):
    if bytes_per_chunk <= 0:
        yield audio_bytes
        return
    for idx in range(0, len(audio_bytes), bytes_per_chunk):
        yield audio_bytes[idx : idx + bytes_per_chunk]


async def _transcribe(
    audio_data: bytes,
    sample_rate: int | None = None,
    sample_width: int | None = None,
    channels: int | None = None,
) -> str:
    host = stt_cfg.get("host", "127.0.0.1")
    port = int(stt_cfg.get("port", 10300))
    language = stt_cfg.get("language", "en")
    input_rate = int(sample_rate if sample_rate is not None else stt_cfg.get("sample_rate", TARGET_RATE))
    input_width = int(sample_width if sample_width is not None else stt_cfg.get("sample_width", TARGET_WIDTH))
    input_channels = int(channels if channels is not None else stt_cfg.get("channels", TARGET_CHANNELS))

    # 对齐 Home Assistant/Wyoming 常见链路：发送 16k/16bit/mono 的 PCM 流
    converter = AudioChunkConverter(
        rate=TARGET_RATE,
        width=TARGET_WIDTH,
        channels=TARGET_CHANNELS,
    )
    converted = converter.convert(
        AudioChunk(
            rate=input_rate,
            width=input_width,
            channels=input_channels,
            audio=audio_data,
        )
    )
    converted_audio = converted.audio

    bytes_per_chunk = max(
        int(TARGET_RATE * TARGET_WIDTH * TARGET_CHANNELS * (CHUNK_MS / 1000.0)),
        TARGET_WIDTH * TARGET_CHANNELS,
    )

    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(
            AudioStart(
                rate=TARGET_RATE,
                width=TARGET_WIDTH,
                channels=TARGET_CHANNELS,
            ).event()
        )

        for chunk_bytes in _iter_audio_chunks(converted_audio, bytes_per_chunk):
            await client.write_event(
                AudioChunk(
                    rate=TARGET_RATE,
                    width=TARGET_WIDTH,
                    channels=TARGET_CHANNELS,
                    audio=chunk_bytes,
                ).event()
            )

        await client.write_event(AudioStop().event())

        latest_text = ""
        while True:
            try:
                event = await asyncio.wait_for(client.read_event(), timeout=READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                # No more events for a while; use the latest transcript we have.
                break

            if event is None:
                break

            if Transcript.is_type(event.type):
                transcription = Transcript.from_event(event)
                text = (transcription.text or "").strip()
                if text:
                    latest_text = text

        return latest_text


def _looks_garbled(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False

    # Detect patterns like: "i 是 y y y y y" (many single-letter tokens).
    tokens = cleaned.split()
    single_alpha_tokens = [t for t in tokens if len(t) == 1 and t.isalpha()]
    if len(tokens) >= 5 and len(single_alpha_tokens) / max(len(tokens), 1) >= 0.45:
        return True

    # Excessive repeated same latin letter with separators.
    if re.search(r"\b([a-zA-Z])(?:\s+\1){4,}\b", cleaned):
        return True

    return False


def transcribe(
    audio_data: bytes,
    sample_rate: int | None = None,
    sample_width: int | None = None,
    channels: int | None = None,
) -> str:
    """Transcribe audio via Wyoming ASR service."""
    try:
        text = asyncio.run(
            _transcribe(
                audio_data,
                sample_rate=sample_rate,
                sample_width=sample_width,
                channels=channels,
            )
        )
        if RETRY_ON_GARBLED and _looks_garbled(text):
            logger.warning("Detected garbled STT transcript, retrying once")
            text_retry = asyncio.run(
                _transcribe(
                    audio_data,
                    sample_rate=sample_rate,
                    sample_width=sample_width,
                    channels=channels,
                )
            )
            if text_retry and not _looks_garbled(text_retry):
                return text_retry
        return text
    except Exception as exc:  # Wyoming connection or protocol errors
        logger.error(f"STT error: {exc}")
        return f"STT Error: {exc}"