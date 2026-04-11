from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import logging
from contextlib import asynccontextmanager

from modules.automation_manager import AutomationError, AutomationManager
from modules.ha_fallback import request_ha_with_fallback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

automation_manager: AutomationManager | None = None


def _log_bad_request(endpoint: str, detail: str | dict[str, object]) -> None:
    logger.warning("Returning 400 from %s with detail: %s", endpoint, detail)


def _model_to_payload(model: BaseModel) -> dict[str, object]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        payload = dump()
        return payload if isinstance(payload, dict) else {"value": payload}

    legacy = getattr(model, "dict", None)
    if callable(legacy):
        payload = legacy()
        return payload if isinstance(payload, dict) else {"value": payload}

    return {"value": str(model)}


def _log_incoming_request(endpoint: str, model: BaseModel) -> None:
    logger.info("Incoming POST %s payload: %s", endpoint, _model_to_payload(model))


def _log_json_response(endpoint: str, status_code: int, body: str | dict[str, object]) -> None:
    logger.info("Returning %s from %s with body: %s", status_code, endpoint, body)


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
        if bool(result.get("ok", False)):
            logger.info("Conversation self-check passed: %s", result.get("message"))
        else:
            logger.warning("Conversation self-check failed: %s", result.get("message"))
    except Exception as exc:
        logger.warning("Conversation self-check skipped due to error: %s", exc)

    yield


app = FastAPI(title="HumbleVoice Automation and Script Manager", lifespan=lifespan)


class CreateAutomationRequest(BaseModel):
    text: str
    language: str | None = None


class CreateScriptRequest(BaseModel):
    text: str
    language: str | None = None


class ManageAutomationRequest(BaseModel):
    text: str = ""
    expected_operation: str
    language: str | None = None
    skip_confirmation: bool = False
    confirmation_id: str | None = None
    confirm: bool = False


class ManageScriptRequest(BaseModel):
    text: str = ""
    expected_operation: str
    language: str | None = None
    skip_confirmation: bool = False
    confirmation_id: str | None = None
    confirm: bool = False


class ConfirmManageRequest(BaseModel):
    confirmation_id: str
    expected_operation: str | None = None


@app.get("/")
async def root():
    return {"message": "HumbleVoice automation/script manager running", "status": "ok"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "humblevoice-automation-script-manager"}


@app.post("/ha-tasks/from-text")
async def create_automation_from_text(req: CreateAutomationRequest):
    _log_incoming_request("/ha-tasks/from-text", req)
    try:
        manager = get_automation_manager()
        result = manager.create_from_text(text=req.text, language=req.language)
        _log_json_response("/ha-tasks/from-text", 200, result)
        return result
    except AutomationError as exc:
        detail: str | dict[str, object] = str(exc)
        debug = getattr(exc, "debug", None)
        if isinstance(debug, dict) and debug:
            detail = {
                "message": str(exc),
                "debug": debug,
            }
        _log_json_response("/ha-tasks/from-text", 400, detail)
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        logger.exception("Failed to create automation from text")
        _log_json_response("/ha-tasks/from-text", 500, f"internal error: {exc}")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


@app.post("/ha-scripts/from-text")
async def create_script_from_text(req: CreateScriptRequest):
    _log_incoming_request("/ha-scripts/from-text", req)
    try:
        manager = get_automation_manager()
        result = manager.create_script_from_text(text=req.text, language=req.language)
        _log_json_response("/ha-scripts/from-text", 200, result)
        return result
    except AutomationError as exc:
        detail: str | dict[str, object] = str(exc)
        debug = getattr(exc, "debug", None)
        if isinstance(debug, dict) and debug:
            detail = {
                "message": str(exc),
                "debug": debug,
            }
        _log_json_response("/ha-scripts/from-text", 400, detail)
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        logger.exception("Failed to create script from text")
        _log_json_response("/ha-scripts/from-text", 500, f"internal error: {exc}")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


@app.post("/ha-tasks/manage-from-text")
async def manage_automation_from_text(req: ManageAutomationRequest):
    _log_incoming_request("/ha-tasks/manage-from-text", req)
    op = (req.expected_operation or "").strip()
    if op not in {"task_update", "task_delete"}:
        _log_bad_request("/ha-tasks/manage-from-text", "expected_operation must be task_update or task_delete")
        _log_json_response("/ha-tasks/manage-from-text", 400, "expected_operation must be task_update or task_delete")
        raise HTTPException(status_code=400, detail="expected_operation must be task_update or task_delete")

    try:
        manager = get_automation_manager()
        if req.skip_confirmation:
            result = manager.manage_without_confirmation(
                text=req.text,
                expected_operation=op,
                language=req.language,
            )
            _log_json_response("/ha-tasks/manage-from-text", 200, result)
            return result

        if req.confirm:
            if not req.confirmation_id:
                _log_bad_request("/ha-tasks/manage-from-text", "confirmation_id required for confirmation")
                _log_json_response("/ha-tasks/manage-from-text", 400, "confirmation_id required for confirmation")
                raise HTTPException(status_code=400, detail="confirmation_id required for confirmation")
            result = manager.confirm_manage(
                req.confirmation_id,
                expected_operation=op,
            )
            _log_json_response("/ha-tasks/manage-from-text", 200, result)
            return result

        result = manager.prepare_manage(
            text=req.text,
            expected_operation=op,
            language=req.language,
        )
        _log_json_response("/ha-tasks/manage-from-text", 200, result)
        return result
    except AutomationError as exc:
        detail: str | dict[str, object] = str(exc)
        debug = getattr(exc, "debug", None)
        if isinstance(debug, dict) and debug:
            detail = {
                "message": str(exc),
                "debug": debug,
            }
        _log_bad_request("/ha-tasks/manage-from-text", detail)
        _log_json_response("/ha-tasks/manage-from-text", 400, detail)
        raise HTTPException(status_code=400, detail=detail) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to manage automation from text")
        _log_json_response("/ha-tasks/manage-from-text", 500, f"internal error: {exc}")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


@app.post("/ha-scripts/manage-from-text")
async def manage_script_from_text(req: ManageScriptRequest):
    _log_incoming_request("/ha-scripts/manage-from-text", req)
    op = (req.expected_operation or "").strip()
    if op not in {"task_update", "task_delete"}:
        _log_bad_request("/ha-scripts/manage-from-text", "expected_operation must be task_update or task_delete")
        _log_json_response("/ha-scripts/manage-from-text", 400, "expected_operation must be task_update or task_delete")
        raise HTTPException(status_code=400, detail="expected_operation must be task_update or task_delete")

    try:
        manager = get_automation_manager()
        if req.skip_confirmation:
            result = manager.manage_script_without_confirmation(
                text=req.text,
                expected_operation=op,
                language=req.language,
            )
            _log_json_response("/ha-scripts/manage-from-text", 200, result)
            return result

        if req.confirm:
            if not req.confirmation_id:
                _log_bad_request("/ha-scripts/manage-from-text", "confirmation_id required for confirmation")
                _log_json_response("/ha-scripts/manage-from-text", 400, "confirmation_id required for confirmation")
                raise HTTPException(status_code=400, detail="confirmation_id required for confirmation")
            result = manager.confirm_manage_script(
                req.confirmation_id,
                expected_operation=op,
            )
            _log_json_response("/ha-scripts/manage-from-text", 200, result)
            return result

        result = manager.prepare_manage_script(
            text=req.text,
            expected_operation=op,
            language=req.language,
        )
        _log_json_response("/ha-scripts/manage-from-text", 200, result)
        return result
    except AutomationError as exc:
        detail: str | dict[str, object] = str(exc)
        debug = getattr(exc, "debug", None)
        if isinstance(debug, dict) and debug:
            detail = {
                "message": str(exc),
                "debug": debug,
            }
        _log_bad_request("/ha-scripts/manage-from-text", detail)
        _log_json_response("/ha-scripts/manage-from-text", 400, detail)
        raise HTTPException(status_code=400, detail=detail) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to manage script from text")
        _log_json_response("/ha-scripts/manage-from-text", 500, f"internal error: {exc}")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


@app.post("/ha-tasks/confirm")
async def confirm_automation_manage(req: ConfirmManageRequest):
    _log_incoming_request("/ha-tasks/confirm", req)
    op = (req.expected_operation or "").strip() if req.expected_operation else ""
    if op and op not in {"task_update", "task_delete"}:
        _log_bad_request("/ha-tasks/confirm", "expected_operation must be task_update or task_delete")
        _log_json_response("/ha-tasks/confirm", 400, "expected_operation must be task_update or task_delete")
        raise HTTPException(status_code=400, detail="expected_operation must be task_update or task_delete")

    try:
        manager = get_automation_manager()
        result = manager.confirm_manage(
            req.confirmation_id,
            expected_operation=op or None,
        )
        _log_json_response("/ha-tasks/confirm", 200, result)
        return result
    except AutomationError as exc:
        detail: str | dict[str, object] = str(exc)
        debug = getattr(exc, "debug", None)
        if isinstance(debug, dict) and debug:
            detail = {
                "message": str(exc),
                "debug": debug,
            }
        _log_bad_request("/ha-tasks/confirm", detail)
        _log_json_response("/ha-tasks/confirm", 400, detail)
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        logger.exception("Failed to confirm automation manage action")
        _log_json_response("/ha-tasks/confirm", 500, f"internal error: {exc}")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


@app.post("/ha-scripts/confirm")
async def confirm_script_manage(req: ConfirmManageRequest):
    _log_incoming_request("/ha-scripts/confirm", req)
    op = (req.expected_operation or "").strip() if req.expected_operation else ""
    if op and op not in {"task_update", "task_delete"}:
        _log_bad_request("/ha-scripts/confirm", "expected_operation must be task_update or task_delete")
        _log_json_response("/ha-scripts/confirm", 400, "expected_operation must be task_update or task_delete")
        raise HTTPException(status_code=400, detail="expected_operation must be task_update or task_delete")

    try:
        manager = get_automation_manager()
        result = manager.confirm_manage_script(
            req.confirmation_id,
            expected_operation=op or None,
        )
        _log_json_response("/ha-scripts/confirm", 200, result)
        return result
    except AutomationError as exc:
        detail: str | dict[str, object] = str(exc)
        debug = getattr(exc, "debug", None)
        if isinstance(debug, dict) and debug:
            detail = {
                "message": str(exc),
                "debug": debug,
            }
        _log_bad_request("/ha-scripts/confirm", detail)
        _log_json_response("/ha-scripts/confirm", 400, detail)
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        logger.exception("Failed to confirm script manage action")
        _log_json_response("/ha-scripts/confirm", 500, f"internal error: {exc}")
        raise HTTPException(status_code=500, detail=f"internal error: {exc}") from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
