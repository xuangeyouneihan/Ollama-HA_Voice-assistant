from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import logging
from modules import stt, tts, llm, ha_client, audio_stream

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HumbleVoice Server")

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
            
            # Process the text and generate response
            if ha_client.is_ha_command(text):
                # Handle Home Assistant commands
                response = ha_client.process_command(text)
                logger.info(f"HA response: {response}")
            else:
                # Handle general queries with LLM
                response = llm.generate_response(text)
                logger.info(f"LLM response: {response}")
            
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")