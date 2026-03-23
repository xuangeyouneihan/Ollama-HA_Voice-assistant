from fastapi import FastAPI, HTTPException, WebSocket
from pydantic import BaseModel
import logging
from contextlib import asynccontextmanager

from config_loader import get_config
from modules.automation_manager import AutomationError, AutomationManager
from modules.ha_fallback import request_ha_with_fallback, run_assist_pipeline_with_fallback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

cfg = get_config()
audio_cfg = cfg.get("audio", {}) if cfg else {}
ASSIST_INPUT_SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))
automation_manager: AutomationManager | None = None


def get_automation_manager() -> AutomationManager:
    global automation_manager
    if automation_manager is None:
        automation_manager = AutomationManager(request_with_fallback=request_ha_with_fallback)
    return automation_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        manager = get_automation_manager()
        result = manager.run_conversation_self_check()
        if bool(result.get("uses_default_agent", False)):
            logger.warning(
                "automation_conversation_agent is not configured; using default conversation agent: %s",
                result.get("agent"),
            )

        if bool(result.get("ok", False)):
            logger.info("Conversation self-check passed: %s", result.get("message"))
        else:
            logger.warning("Conversation self-check failed: %s", result.get("message"))
    except Exception as exc:
        logger.warning("Conversation self-check skipped due to error: %s", exc)

    yield


app = FastAPI(title="HumbleVoice Server (ESP32 Bridge)", lifespan=lifespan)


class CreateAutomationRequest(BaseModel):
    text: str
    language: str | None = None


class ManageAutomationRequest(BaseModel):
    text: str
    expected_operation: str
    language: str | None = None
    confirmation_id: str | None = None
    confirm: bool = False


@app.websocket("/audio")
async def audio_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("ESP32 client connected")

    try:
        while True:
            audio_data = await websocket.receive_bytes()
            logger.info("Received %s bytes of audio data", len(audio_data))

            flow = await run_assist_pipeline_with_fallback(
                audio_data,
                sample_rate=ASSIST_INPUT_SAMPLE_RATE,
            )
            if not bool(flow.get("ok", False)):
                message = str(flow.get("message") or "assist pipeline request failed")
                logger.error("Assist request failed: %s", message)
                await websocket.send_text("Assist request failed")
                continue

            transcript = str(flow.get("transcript") or "").strip()
            if transcript:
                logger.info("Assist transcript: %s", transcript)

            response_text = str(flow.get("response_text") or "").strip()
            if response_text:
                logger.info("Assist response: %s", response_text)

            audio_response = flow.get("tts_audio") or b""
            if bool(flow.get("has_playable_tts", False)):
                await websocket.send_bytes(audio_response)
                logger.info("Sent %s bytes of Assist TTS audio response", len(audio_response))
            elif response_text:
                await websocket.send_text(response_text)
                logger.warning("Assist returned no playable TTS audio, sent text response instead")
            else:
                await websocket.send_text(str(flow.get("fallback_text") or "Okay"))
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


@app.post("/ha-tasks/from-text")
async def create_automation_from_text(req: CreateAutomationRequest):
    try:
        manager = get_automation_manager()
        result = manager.create_from_text(text=req.text, language=req.language)
        return result
    except AutomationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to create automation from text")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


@app.post("/ha-tasks/manage-from-text")
async def manage_automation_from_text(req: ManageAutomationRequest):
    op = (req.expected_operation or "").strip()
    if op not in {"task_update", "task_delete"}:
        raise HTTPException(status_code=400, detail="expected_operation must be task_update or task_delete")

    try:
        manager = get_automation_manager()
        if req.confirm:
            if not req.confirmation_id:
                raise HTTPException(status_code=400, detail="confirmation_id required when confirm=true")
            return manager.confirm_manage(req.confirmation_id)

        if req.confirmation_id:
            return manager.confirm_manage(req.confirmation_id)

        if manager.is_confirmation_text(req.text):
            return manager.confirm_latest_manage(op)

        return manager.prepare_manage(
            text=req.text,
            expected_operation=op,
            language=req.language,
        )
    except AutomationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to manage automation from text")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
