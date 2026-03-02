"""
Text-to-Speech module for HumbleVoice
Uses Wyoming TTS services (e.g., wyoming-piper)
"""
import asyncio
import logging
from config_loader import get_config
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice, SynthesizeStopped
from wyoming.audio import AudioChunk, AudioStop, AudioStart

logger = logging.getLogger(__name__)

cfg = get_config()
tts_cfg = cfg.get("tts", {}) if cfg else {}


async def _synthesize_with_format(text: str) -> tuple[bytes, int, int, int]:
    host = tts_cfg.get("host", "127.0.0.1")
    port = int(tts_cfg.get("port", 10200))
    voice_name = tts_cfg.get("voice", "en_US-lessac-medium")
    sample_rate = int(tts_cfg.get("sample_rate", 22050))
    sample_width = int(tts_cfg.get("sample_width", 2))
    channels = int(tts_cfg.get("channels", 1))

    audio_out = bytearray()
    async with AsyncTcpClient(host, port) as client:
        await client.write_event(
            Synthesize(text=text, voice=SynthesizeVoice(name=voice_name)).event()
        )

        while True:
            event = await client.read_event()
            if event is None:
                break
            if AudioStart.is_type(event.type):
                audio_start = AudioStart.from_event(event)
                sample_rate = int(audio_start.rate)
                sample_width = int(audio_start.width)
                channels = int(audio_start.channels)
            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                # 部分服务不会单独发 AudioStart，这里兜底读取 chunk 格式
                sample_rate = int(chunk.rate)
                sample_width = int(chunk.width)
                channels = int(chunk.channels)
                audio_out.extend(chunk.audio)
            elif AudioStop.is_type(event.type) or SynthesizeStopped.is_type(event.type):
                break

    return bytes(audio_out), sample_rate, sample_width, channels


async def _synthesize(text: str) -> bytes:
    audio, _, _, _ = await _synthesize_with_format(text)
    return audio


def synthesize(text: str) -> bytes:
    """Convert text to audio via Wyoming TTS service."""
    if not text or text.strip() == "":
        logger.warning("Empty text provided to TTS")
        return b""

    try:
        return asyncio.run(_synthesize(text))
    except Exception as exc:
        logger.error(f"TTS error: {exc}")
        return b""


def synthesize_with_format(text: str) -> tuple[bytes, int, int, int]:
    """Convert text to audio and return (audio_bytes, sample_rate, sample_width, channels)."""
    if not text or text.strip() == "":
        logger.warning("Empty text provided to TTS")
        return b"", int(tts_cfg.get("sample_rate", 22050)), int(tts_cfg.get("sample_width", 2)), int(tts_cfg.get("channels", 1))

    try:
        return asyncio.run(_synthesize_with_format(text))
    except Exception as exc:
        logger.error(f"TTS error: {exc}")
        return b"", int(tts_cfg.get("sample_rate", 22050)), int(tts_cfg.get("sample_width", 2)), int(tts_cfg.get("channels", 1))