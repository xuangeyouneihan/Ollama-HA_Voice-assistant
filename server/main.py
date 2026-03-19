from fastapi import FastAPI, WebSocket
import logging

from config_loader import get_config
from modules import ha_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HumbleVoice Server (ESP32 Bridge)")
cfg = get_config()
audio_cfg = cfg.get("audio", {}) if cfg else {}
ASSIST_INPUT_SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))


@app.websocket("/audio")
async def audio_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("ESP32 client connected")

    try:
        while True:
            audio_data = await websocket.receive_bytes()
            logger.info("Received %s bytes of audio data", len(audio_data))

            assist_result = await ha_client.process_audio_with_assist_pipeline(
                audio_data,
                sample_rate=ASSIST_INPUT_SAMPLE_RATE,
            )
            if not bool(assist_result.get("ok", False)):
                message = str(assist_result.get("message") or "assist pipeline request failed")
                logger.error("Assist request failed: %s", message)
                await websocket.send_text("Assist request failed")
                continue

            transcript = str(assist_result.get("transcript") or "").strip()
            if transcript:
                logger.info("Assist transcript: %s", transcript)

            response_text = str(assist_result.get("response_text") or "").strip()
            if response_text:
                logger.info("Assist response: %s", response_text)

            audio_response = assist_result.get("tts_audio") or b""
            if audio_response:
                await websocket.send_bytes(audio_response)
                logger.info("Sent %s bytes of Assist TTS audio response", len(audio_response))
            elif response_text:
                await websocket.send_text(response_text)
                logger.warning("Assist returned no playable TTS audio, sent text response instead")
            else:
                await websocket.send_text("Okay")
                logger.warning("Assist returned neither audio nor text, sent fallback acknowledgment")

    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        await websocket.close()
        logger.info("ESP32 client disconnected")


@app.get("/")
async def root():
    return {"message": "HumbleVoice ESP32 bridge running", "status": "ok"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "humblevoice-esp32-bridge"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
