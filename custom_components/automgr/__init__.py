from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_TIMEOUT
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEFAULT_LANGUAGE,
    CONF_SERVER_URL,
    DEFAULT_LANGUAGE,
    DEFAULT_SERVER_URL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_SERVER_URL, default=DEFAULT_SERVER_URL): cv.string,
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(CONF_DEFAULT_LANGUAGE, default=DEFAULT_LANGUAGE): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

MANAGE_SCHEMA = vol.Schema(
    {
        vol.Optional("text", default=""): cv.string,
        vol.Optional("language"): cv.string,
        vol.Optional("skip_confirmation", default=False): cv.boolean,
        vol.Optional("confirmation_id", default=""): cv.string,
        vol.Optional("confirm", default=False): cv.boolean,
    }
)

CREATE_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("language"): cv.string,
    }
)

CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required("confirmation_id"): cv.string,
        vol.Required("expected_operation"): vol.Any("task_update", "task_delete"),
        vol.Optional("language"): cv.string,
    }
)


def _strip(value: str | None) -> str:
    return str(value or "").strip()


async def _request_json(
    hass: HomeAssistant,
    method: str,
    url: str,
    timeout_s: int,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    try:
        async with session.request(method=method, url=url, json=json_body, timeout=timeout_s) as resp:
            status = int(resp.status)
            data: Any
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                data = {"raw": text}

            if not isinstance(data, dict):
                data = {"data": data}

            if status >= 400:
                message = ""
                if isinstance(data, dict):
                    detail = data.get("detail")
                    if isinstance(detail, dict):
                        message = str(detail.get("message") or detail)
                    else:
                        message = str(detail or data.get("message") or data)
                if not message:
                    message = f"HTTP {status}"
                raise HomeAssistantError(message)

            return data
    except HomeAssistantError:
        raise
    except Exception as exc:
        raise HomeAssistantError(f"request failed: {exc}") from exc


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    cfg = config.get(DOMAIN, {})
    server_url = _strip(cfg.get(CONF_SERVER_URL) or DEFAULT_SERVER_URL).rstrip("/")
    timeout_s = int(cfg.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
    default_language = _strip(cfg.get(CONF_DEFAULT_LANGUAGE) or DEFAULT_LANGUAGE) or DEFAULT_LANGUAGE

    hass.data[DOMAIN] = {
        CONF_SERVER_URL: server_url,
        CONF_TIMEOUT: timeout_s,
        CONF_DEFAULT_LANGUAGE: default_language,
        "prepare_context_cache": {},
        "manage_context_state": {},
    }

    def _runtime() -> tuple[str, int, str]:
        runtime = hass.data.get(DOMAIN, {})
        return (
            _strip(runtime.get(CONF_SERVER_URL) or server_url).rstrip("/"),
            int(runtime.get(CONF_TIMEOUT, timeout_s)),
            _strip(runtime.get(CONF_DEFAULT_LANGUAGE) or default_language) or DEFAULT_LANGUAGE,
        )

    def _prepare_key(resource_type: str, operation: str) -> str:
        return f"{resource_type}:{operation}"

    def _current_context_id(call: ServiceCall) -> str:
        return _strip(getattr(call.context, "id", ""))

    def _save_prepare_context(resource_type: str, operation: str, context_id: str) -> None:
        key = _prepare_key(resource_type, operation)
        if not key:
            return
        runtime = hass.data.get(DOMAIN, {})
        ctx_raw = runtime.get("prepare_context_cache")
        ctx_map = ctx_raw if isinstance(ctx_raw, dict) else {}
        ctx_map[key] = _strip(context_id)
        runtime["prepare_context_cache"] = ctx_map
        hass.data[DOMAIN] = runtime

    def _get_prepare_context(resource_type: str, operation: str) -> str:
        key = _prepare_key(resource_type, operation)
        runtime = hass.data.get(DOMAIN, {})
        ctx_raw = runtime.get("prepare_context_cache")
        ctx_map = ctx_raw if isinstance(ctx_raw, dict) else {}
        return _strip(ctx_map.get(key))

    def _consume_prepare_context(resource_type: str, operation: str) -> str:
        key = _prepare_key(resource_type, operation)
        runtime = hass.data.get(DOMAIN, {})
        ctx_raw = runtime.get("prepare_context_cache")
        ctx_map = ctx_raw if isinstance(ctx_raw, dict) else {}
        value = _strip(ctx_map.pop(key, ""))
        runtime["prepare_context_cache"] = ctx_map
        hass.data[DOMAIN] = runtime
        return value

    def _enforce_failed_context_lock(context_id: str, service_name: str) -> None:
        if not context_id:
            return
        runtime = hass.data.get(DOMAIN, {})
        state_raw = runtime.get("manage_context_state")
        state_map = state_raw if isinstance(state_raw, dict) else {}
        state = state_map.get(context_id)
        if not isinstance(state, dict):
            return
        if str(state.get("status") or "") != "failed":
            return
        last_service = _strip(state.get("service"))
        if last_service and last_service != service_name:
            raise HomeAssistantError(
                "previous manage script failed in this assistant turn; "
                "retry the same script only"
            )

    def _mark_context_result(context_id: str, service_name: str, ok: bool) -> None:
        if not context_id:
            return
        runtime = hass.data.get(DOMAIN, {})
        state_raw = runtime.get("manage_context_state")
        state_map = state_raw if isinstance(state_raw, dict) else {}
        state_map[context_id] = {
            "service": service_name,
            "status": "succeeded" if ok else "failed",
        }
        runtime["manage_context_state"] = state_map
        hass.data[DOMAIN] = runtime

    async def _call_create(call: ServiceCall, path: str) -> ServiceResponse:
        base_url, req_timeout, req_lang = _runtime()
        text = _strip(call.data.get("text"))
        language = _strip(call.data.get("language") or req_lang) or req_lang
        payload = {"text": text, "language": language}
        result = await _request_json(hass, "POST", f"{base_url}{path}", req_timeout, payload)
        return {
            "ok": True,
            "phase": "done",
            "service": call.service,
            "data": result,
            "message": str(result.get("message") or "ok"),
        }

    async def _call_manage(call: ServiceCall, path: str, operation: str, resource_type: str) -> ServiceResponse:
        base_url, req_timeout, req_lang = _runtime()
        text = _strip(call.data.get("text"))
        language = _strip(call.data.get("language") or req_lang) or req_lang
        skip_confirmation = bool(call.data.get("skip_confirmation", False))
        confirmation_id = _strip(call.data.get("confirmation_id"))
        confirm = bool(call.data.get("confirm", False))
        context_id = _current_context_id(call)

        _enforce_failed_context_lock(context_id, call.service)

        if confirm and not confirmation_id:
            raise HomeAssistantError("confirmation_id is required for confirmation")

        if confirm:
            prepared_context = _get_prepare_context(resource_type, operation)
            if prepared_context and context_id and prepared_context == context_id:
                raise HomeAssistantError(
                    "confirmation blocked in same assistant turn; please ask user to confirm in a new utterance"
                )

        payload: dict[str, Any] = {
            "text": text,
            "expected_operation": operation,
            "language": language,
            "skip_confirmation": skip_confirmation,
            "confirm": confirm,
        }

        if confirm and confirmation_id:
            payload["confirmation_id"] = confirmation_id

        try:
            result = await _request_json(hass, "POST", f"{base_url}{path}", req_timeout, payload)
        except Exception:
            _mark_context_result(context_id, call.service, ok=False)
            raise

        returned_confirmation_id = _strip(result.get("confirmation_id"))
        is_prepared = bool(result.get("needs_confirmation"))
        if not is_prepared and returned_confirmation_id:
            is_prepared = (not confirm) and (not skip_confirmation)

        if is_prepared and returned_confirmation_id:
            _save_prepare_context(resource_type, operation, context_id)
        if confirm and confirmation_id:
            _consume_prepare_context(resource_type, operation)

        _mark_context_result(context_id, call.service, ok=True)

        return {
            "ok": True,
            "phase": "prepare" if is_prepared else "done",
            "service": call.service,
            "confirmation_id": returned_confirmation_id,
            "data": result,
            "message": str(result.get("message") or "ok"),
        }

    async def _call_confirm(
        call: ServiceCall,
        manage_path: str,
        operation: str,
        resource_type: str,
    ) -> ServiceResponse:
        base_url, req_timeout, req_lang = _runtime()
        context_id = _current_context_id(call)
        _enforce_failed_context_lock(context_id, call.service)

        expected_operation = _strip(call.data.get("expected_operation"))
        if expected_operation != operation:
            raise HomeAssistantError(f"expected_operation must be {operation}")

        confirmation_id = _strip(call.data.get("confirmation_id"))
        if not confirmation_id:
            raise HomeAssistantError("confirmation_id is required")

        prepared_context = _get_prepare_context(resource_type, operation)
        if prepared_context and context_id and prepared_context == context_id:
            raise HomeAssistantError(
                "confirmation blocked in same assistant turn; please ask user to confirm in a new utterance"
            )

        language = _strip(call.data.get("language") or req_lang) or req_lang
        payload: dict[str, Any] = {
            "text": "",
            "expected_operation": operation,
            "language": language,
            "confirm": True,
            "confirmation_id": confirmation_id,
        }

        try:
            result = await _request_json(hass, "POST", f"{base_url}{manage_path}", req_timeout, payload)
        except Exception:
            _mark_context_result(context_id, call.service, ok=False)
            raise

        _consume_prepare_context(resource_type, operation)
        _mark_context_result(context_id, call.service, ok=True)
        return {
            "ok": True,
            "phase": "done",
            "service": call.service,
            "confirmation_id": confirmation_id,
            "data": result,
            "message": str(result.get("message") or "ok"),
        }

    async def handle_automation_create(call: ServiceCall) -> ServiceResponse:
        return await _call_create(call, "/ha-tasks/from-text")

    async def handle_automation_update(call: ServiceCall) -> ServiceResponse:
        return await _call_manage(call, "/ha-tasks/manage-from-text", "task_update", "automation")

    async def handle_automation_delete(call: ServiceCall) -> ServiceResponse:
        return await _call_manage(call, "/ha-tasks/manage-from-text", "task_delete", "automation")

    async def handle_automation_confirm(call: ServiceCall) -> ServiceResponse:
        expected_operation = _strip(call.data.get("expected_operation"))
        return await _call_confirm(call, "/ha-tasks/manage-from-text", expected_operation, "automation")

    async def handle_script_create(call: ServiceCall) -> ServiceResponse:
        return await _call_create(call, "/ha-scripts/from-text")

    async def handle_script_update(call: ServiceCall) -> ServiceResponse:
        return await _call_manage(call, "/ha-scripts/manage-from-text", "task_update", "script")

    async def handle_script_delete(call: ServiceCall) -> ServiceResponse:
        return await _call_manage(call, "/ha-scripts/manage-from-text", "task_delete", "script")

    async def handle_script_confirm(call: ServiceCall) -> ServiceResponse:
        expected_operation = _strip(call.data.get("expected_operation"))
        return await _call_confirm(call, "/ha-scripts/manage-from-text", expected_operation, "script")

    hass.services.async_register(
        DOMAIN,
        "automation_create",
        handle_automation_create,
        schema=CREATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "automation_update",
        handle_automation_update,
        schema=MANAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "automation_delete",
        handle_automation_delete,
        schema=MANAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "automation_confirm",
        handle_automation_confirm,
        schema=CONFIRM_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        "script_create",
        handle_script_create,
        schema=CREATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "script_update",
        handle_script_update,
        schema=MANAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "script_delete",
        handle_script_delete,
        schema=MANAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "script_confirm",
        handle_script_confirm,
        schema=CONFIRM_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.info("automgr services registered, target server: %s", server_url)
    return True
