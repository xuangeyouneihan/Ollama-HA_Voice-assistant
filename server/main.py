from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import logging
from modules import stt, tts, ha_client, audio_stream, presets
from config_loader import get_config

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HumbleVoice Server")
preset_store = presets.build_store_from_config()
cfg = get_config()
audio_cfg = cfg.get("audio", {}) if cfg else {}
ha_cfg = cfg.get("home_assistant", {}) if cfg else {}
ASSIST_AUDIO_MODE = bool(ha_cfg.get("assist_audio_mode", False))
ASSIST_INPUT_SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/audio")
async def audio_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for audio streaming
    Receives audio data from ESP32, processes it, and sends back responses
    """
    await websocket.accept()
    logger.info("ESP32 client connected")
    
    try:
        while True:
            # Receive audio data from ESP32 client
            audio_data = await websocket.receive_bytes()
            logger.info(f"Received {len(audio_data)} bytes of audio data")

            if ASSIST_AUDIO_MODE:
                logger.info("Assist audio mode enabled: forwarding raw audio to Home Assistant Assist pipeline")
                assist_result = await ha_client.process_audio_with_assist_pipeline(
                    audio_data,
                    sample_rate=ASSIST_INPUT_SAMPLE_RATE,
                )
                if not bool(assist_result.get("ok", False)):
                    message = str(assist_result.get("message") or "assist pipeline request failed")
                    logger.error(f"Assist request failed: {message}")
                    await websocket.send_text("Assist request failed")
                    continue

                transcript = str(assist_result.get("transcript") or "").strip()
                if transcript:
                    logger.info(f"Assist transcript: {transcript}")
                response_text = str(assist_result.get("response_text") or "").strip()
                if response_text:
                    logger.info(f"Assist response: {response_text}")

                audio_response = assist_result.get("tts_audio") or b""
                if audio_response:
                    await websocket.send_bytes(audio_response)
                    logger.info(f"Sent {len(audio_response)} bytes of Assist TTS audio response")
                elif response_text:
                    await websocket.send_text(response_text)
                    logger.warning("Assist returned no playable TTS audio, sent text response instead")
                else:
                    await websocket.send_text("Okay")
                    logger.warning("Assist returned neither audio nor text, sent fallback acknowledgment")
                continue
            
            # Convert audio to text using STT
            text = stt.transcribe(audio_data)
            logger.info(f"Transcribed text: {text}")
            
            if not text or text.strip() == "":
                continue  # Skip empty transcriptions
            
            # Route all text through LLM first, then execute HA actions/queries only when needed.
            response = ha_client.handle_user_text(text)
            logger.info(f"Assistant response: {response}")
            
            # Convert response text to audio using TTS
            audio_response = tts.synthesize(response)
            
            if audio_response:
                # Send audio response back to ESP32 for playback
                await websocket.send_bytes(audio_response)
                logger.info(f"Sent {len(audio_response)} bytes of audio response")
            else:
                # Send a simple acknowledgment if TTS fails
                await websocket.send_text("Okay")
                logger.warning("TTS failed, sent text acknowledgment")
            
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await websocket.close()
        logger.info("ESP32 client disconnected")

@app.get("/")
async def root():
    """
    Health check endpoint
    """
    return {"message": "HumbleVoice Server Running", "status": "ok"}

@app.get("/health")
async def health_check():
    """
    Detailed health check
    """
    return {
        "status": "healthy",
        "service": "humblevoice-server",
        "version": "1.0.0"
    }


@app.get("/presets")
async def list_presets():
    return {"items": preset_store.list_presets()}


@app.get("/presets/{preset_id}")
async def get_preset(preset_id: str):
    item = preset_store.get_preset(preset_id)
    if not item:
        raise HTTPException(status_code=404, detail="Preset not found")
    return item


@app.post("/presets")
async def create_preset(payload: dict):
    try:
        created = preset_store.create_preset(payload)
        return created
    except presets.PresetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/presets/{preset_id}")
async def update_preset(preset_id: str, payload: dict):
    try:
        updated = preset_store.update_preset(preset_id, payload)
        if not updated:
            raise HTTPException(status_code=404, detail="Preset not found")
        return updated
    except presets.PresetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/presets/{preset_id}")
async def delete_preset(preset_id: str):
    ok = preset_store.delete_preset(preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"deleted": True}


@app.post("/presets/{preset_id}/compile-ha")
async def compile_preset_ha(preset_id: str):
    item = preset_store.get_preset(preset_id)
    if not item:
        raise HTTPException(status_code=404, detail="Preset not found")
    return presets.compile_preset_to_ha(item)


@app.post("/presets/{preset_id}/simulate")
async def simulate_preset(preset_id: str):
    item = preset_store.get_preset(preset_id)
    if not item:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Device-agnostic simulation: returns the execution plan only.
    return {
        "preset_id": preset_id,
        "name": item.get("name"),
        "enabled": item.get("enabled", True),
        "trigger": item.get("trigger", {}),
        "conditions": item.get("conditions", []),
        "actions": item.get("actions", []),
        "note": "Simulation mode only. No real service call was executed.",
    }


@app.post("/ha-tasks/from-text")
async def create_ha_task_from_text(payload: dict):
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    result = ha_client.create_ha_task_from_text(text)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "failed to create HA task")
    return result


@app.post("/ha-tasks/manage-from-text")
async def manage_ha_task_from_text(payload: dict):
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    expected_operation = payload.get("expected_operation")
    if expected_operation is not None:
        expected_operation = str(expected_operation).strip().lower()
        if expected_operation not in {"task_update", "task_delete"}:
            raise HTTPException(status_code=400, detail="expected_operation must be task_update or task_delete")

    result = ha_client.manage_ha_task_from_text(text, expected_operation=expected_operation)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "failed to manage HA task")
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")