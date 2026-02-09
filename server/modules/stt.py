"""
Speech-to-Text module for HumbleVoice
Uses Wyoming ASR services (e.g., wyoming-faster-whisper)
"""
import asyncio
import logging
from config_loader import get_config
from wyoming.client import AsyncTcpClient
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStop, AudioStart

logger = logging.getLogger(__name__)

cfg = get_config()
stt_cfg = cfg.get("stt", {}) if cfg else {}


async def _transcribe(audio_data: bytes) -> str:
    host = stt_cfg.get("host", "127.0.0.1")
    port = int(stt_cfg.get("port", 10300))
    language = stt_cfg.get("language", "en")
    sample_rate = int(stt_cfg.get("sample_rate", 16000))
    sample_width = int(stt_cfg.get("sample_width", 2))
    channels = int(stt_cfg.get("channels", 1))

    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(
            AudioStart(
                rate=sample_rate,
                width=sample_width,
                channels=channels,
            ).event()
        )
        await client.write_event(
            AudioChunk(
                rate=sample_rate,
                width=sample_width,
                channels=channels,
                audio=audio_data,
            ).event()
        )
        await client.write_event(AudioStop().event())

        while True:
            event = await client.read_event()
            if event is None:
                break
            if Transcript.is_type(event.type):
                transcription = Transcript.from_event(event)
                return transcription.text
    return ""


def transcribe(audio_data: bytes) -> str:
    """Transcribe audio via Wyoming ASR service."""
    try:
        return asyncio.run(_transcribe(audio_data))
    except Exception as exc:  # Wyoming connection or protocol errors
        logger.error(f"STT error: {exc}")
        return f"STT Error: {exc}"