"""
Text-to-Speech module for HumbleVoice
Uses Wyoming TTS services (e.g., wyoming-piper)
"""
import asyncio
import logging
from config_loader import get_config
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize
from wyoming.audio import AudioChunk, AudioStop

logger = logging.getLogger(__name__)

cfg = get_config()
tts_cfg = cfg.get("tts", {}) if cfg else {}


async def _synthesize(text: str) -> bytes:
    host = tts_cfg.get("host", "127.0.0.1")
    port = int(tts_cfg.get("port", 10200))
    voice = tts_cfg.get("voice", "en_US-lessac-medium")

    audio_out = bytearray()
    async with AsyncTcpClient(host, port) as client:
        await client.write(Synthesize(text=text, voice=voice).to_message())

        async for message in client:
            if AudioChunk.is_type(message.type):
                chunk = AudioChunk.from_message(message)
                audio_out.extend(chunk.data)
            elif AudioStop.is_type(message.type):
                break

    return bytes(audio_out)


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