from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import logging
from modules import stt, tts, ha_client, audio_stream, presets

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HumbleVoice Server")
preset_store = presets.build_store_from_config()

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")