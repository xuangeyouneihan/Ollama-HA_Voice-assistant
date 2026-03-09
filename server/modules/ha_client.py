"""
Home Assistant integration module for HumbleVoice
Handles HA commands and entity control
"""
import requests
import json
import logging
from config_loader import get_config
from modules import llm

logger = logging.getLogger(__name__)

cfg = get_config()
ha_cfg = cfg.get("home_assistant", {}) if cfg else {}
HA_URL = ha_cfg.get("url", "http://homeassistant.local:8123")
HA_TOKEN = ha_cfg.get("token", "YOUR_HA_TOKEN")

headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json"
}

UNAVAILABLE_STATES = {"unavailable", "unknown"}


def _build_service_url(service: str) -> str:
    """Build Home Assistant service endpoint: /api/services/<domain>/<service>."""
    service = (service or "").strip()
    if not service:
        raise ValueError("service is empty")

    if "/" in service:
        domain, action = service.split("/", 1)
    elif "." in service:
        domain, action = service.split(".", 1)
    else:
        raise ValueError(f"invalid service format: {service}")

    domain = domain.strip()
    action = action.strip()
    if not domain or not action:
        raise ValueError(f"invalid service format: {service}")

    return f"{HA_URL.rstrip('/')}/api/services/{domain}/{action}"

def is_ha_command(text):
    """Backward-compatible keyword check. Not used in new main flow."""
    ha_keywords = [
        "turn on", "turn off", "light", "switch", "lamp", "fan", "ac", "heater", "thermostat",
        "打开", "关闭", "开灯", "关灯", "灯", "开关", "风扇", "空调", "暖气", "温控", "天气", "温度",
    ]
    text_lower = (text or "").lower()
    return any(keyword in text_lower for keyword in ha_keywords)


def handle_user_text(text: str) -> str:
    """Always route user text through LLM, then execute HA control/query when needed."""
    if not text or not text.strip():
        return "I didn't catch that."

    route = _route_with_llm(text)
    ha_related = bool(route.get("ha_related", False))
    intent = str(route.get("intent", "none")).lower().strip()

    if not ha_related:
        answer = (route.get("answer") or "").strip()
        return answer or llm.generate_response(text)

    states = discover_entities()
    if not states:
        return "Home Assistant Error: Unable to fetch entity states"

    if intent == "query":
        return _handle_query_intent(text, route, states)

    if intent == "control":
        return _handle_control_intent(text, route, states)

    # Fallback for malformed router output.
    if route.get("actions"):
        return _handle_control_intent(text, route, states)
    if route.get("query_entities"):
        return _handle_query_intent(text, route, states)

    answer = (route.get("answer") or "").strip()
    return answer or llm.generate_response(text)


def process_command(text):
    """Compatibility wrapper used by older call sites."""
    return handle_user_text(text)


def _handle_control_intent(text: str, route: dict, states: list[dict]) -> str:
    services = discover_services()
    service_map = _build_service_map(services)

    plan = {"actions": route.get("actions") or []}
    if not plan["actions"]:
        plan = _plan_ha_actions_with_llm(text, states, services)

    planned_actions = plan.get("actions", [])
    if not isinstance(planned_actions, list) or not planned_actions:
        return "I couldn't find a valid Home Assistant control action"

    state_map = {item.get("entity_id"): item for item in states if isinstance(item, dict) and item.get("entity_id")}
    execution_results = []

    for action in planned_actions:
        service = (action.get("service") or "").strip()
        if not service:
            continue

        # Skip actions not present in current HA services catalog.
        if service_map and service not in service_map:
            execution_results.append(
                {
                    "service": service,
                    "executed": [],
                    "skipped": [],
                    "response": "Service not available in Home Assistant",
                    "ok": False,
                }
            )
            continue

        target = action.get("target") or {}
        if not isinstance(target, dict):
            target = {}

        # Backward compatibility with older schema: entity_ids at action root.
        root_entity_ids = action.get("entity_ids") or []
        target_entity_ids = target.get("entity_id")
        if isinstance(target_entity_ids, str):
            target_entity_ids = [target_entity_ids]
        elif not isinstance(target_entity_ids, list):
            target_entity_ids = []

        if not target_entity_ids and isinstance(root_entity_ids, list):
            target_entity_ids = root_entity_ids

        service_data = action.get("service_data") or {}
        if not isinstance(service_data, dict):
            service_data = {}

        # Build final payload supported by HA service API.
        payload = {}
        if target_entity_ids:
            payload["entity_id"] = target_entity_ids
        if target.get("area_id"):
            payload["area_id"] = target.get("area_id")
        if target.get("device_id"):
            payload["device_id"] = target.get("device_id")
        payload.update(service_data)

        # If there is no entity target, call directly (some services operate without entity_id).
        if not target_entity_ids:
            ok, response_msg = _call_ha_service(service, payload)
            execution_results.append(
                {
                    "service": service,
                    "executed": ["<no-entity-target>"] if ok else [],
                    "skipped": [],
                    "response": response_msg,
                    "ok": ok,
                }
            )
            continue

        valid_entities = []
        skipped_entities = []
        for entity_id in target_entity_ids:
            state_obj = state_map.get(entity_id)
            if not state_obj:
                skipped_entities.append((entity_id, "not_found"))
                continue

            state = str(state_obj.get("state", "")).lower()
            if state in UNAVAILABLE_STATES:
                skipped_entities.append((entity_id, state))
                continue

            valid_entities.append(entity_id)

        if valid_entities:
            call_payload = dict(payload)
            call_payload["entity_id"] = valid_entities
            ok, response_msg = _call_ha_service(service, call_payload)
            execution_results.append(
                {
                    "service": service,
                    "executed": valid_entities,
                    "skipped": skipped_entities,
                    "response": response_msg,
                    "ok": ok,
                }
            )
        elif skipped_entities:
            execution_results.append(
                {
                    "service": service,
                    "executed": [],
                    "skipped": skipped_entities,
                    "response": "No available target entities",
                    "ok": False,
                }
            )

    if not execution_results:
        return "No available target entities found in Home Assistant"

    success_count = sum(1 for result in execution_results if result.get("ok"))
    skipped_count = sum(len(result.get("skipped", [])) for result in execution_results)
    if success_count > 0 and skipped_count == 0:
        return "Okay, I've done that"
    if success_count > 0:
        return f"Done for available entities. Skipped {skipped_count} unavailable or missing entities."
    return "I found target entities, but could not execute actions successfully"


def _handle_query_intent(text: str, route: dict, states: list[dict]) -> str:
    query_entities = route.get("query_entities") or []
    if not isinstance(query_entities, list) or not query_entities:
        query_plan = _plan_ha_queries_with_llm(text, states)
        query_entities = query_plan.get("query_entities") or []

    if not query_entities:
        return "I couldn't identify which Home Assistant entity to query"

    state_map = {item.get("entity_id"): item for item in states if isinstance(item, dict) and item.get("entity_id")}
    query_results = []
    missing = []
    for entity_id in query_entities:
        state_obj = state_map.get(entity_id)
        if not state_obj:
            missing.append(entity_id)
            continue
        query_results.append(
            {
                "entity_id": entity_id,
                "state": state_obj.get("state"),
                "friendly_name": state_obj.get("attributes", {}).get("friendly_name", ""),
                "unit": state_obj.get("attributes", {}).get("unit_of_measurement", ""),
            }
        )

    if not query_results:
        return "I couldn't find those entities in Home Assistant"

    summary = _summarize_query_results_with_llm(text, query_results)
    if missing:
        return f"{summary} (Missing entities: {', '.join(missing)})"
    return summary


def _plan_ha_actions_with_llm(text: str, entities: list[dict], services: list[dict]) -> dict:
    entity_summaries = []
    for item in entities:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        friendly_name = item.get("attributes", {}).get("friendly_name", "")
        state = item.get("state", "")
        entity_summaries.append(
            {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "state": state,
            }
        )

    # Keep context bounded for local models.
    entity_summaries = entity_summaries[:300]

    service_summaries = _summarize_services_for_prompt(services)

    prompt = (
        "You are an intent parser for Home Assistant control.\n"
        "Given the user command, entities, and available services, return ONLY JSON with schema:\n"
        "{\"actions\": [{\"service\": \"domain.service\", \"target\": {\"entity_id\": [\"domain.name\"], \"area_id\": [], \"device_id\": []}, \"service_data\": {}}]}\n"
        "Rules:\n"
        "1) service MUST be chosen from provided services list.\n"
        "2) entity_id MUST be chosen from provided entities list when needed.\n"
        "3) Use service_data for parameters like volume_level, hvac_mode, temperature.\n"
        "4) If no confident mapping, return {\"actions\": []}.\n"
        f"User command: {text}\n"
        f"Entities JSON: {json.dumps(entity_summaries, ensure_ascii=False)}\n"
        f"Services JSON: {json.dumps(service_summaries, ensure_ascii=False)}"
    )

    raw = llm.generate_response(prompt)
    return _extract_json_object(raw)


def _plan_ha_queries_with_llm(text: str, entities: list[dict]) -> dict:
    entity_summaries = []
    for item in entities:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        friendly_name = item.get("attributes", {}).get("friendly_name", "")
        state = item.get("state", "")
        entity_summaries.append(
            {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "state": state,
            }
        )

    entity_summaries = entity_summaries[:300]
    prompt = (
        "You map user query to Home Assistant entities.\n"
        "Return ONLY JSON with schema: {\"query_entities\": [\"domain.name\"]}.\n"
        "Rules:\n"
        "1) Select only from provided entity list.\n"
        "2) Prefer weather.*, sensor.*, climate.* when user asks weather/temperature.\n"
        "3) If no confident mapping, return {\"query_entities\": []}.\n"
        f"User command: {text}\n"
        f"Entities JSON: {json.dumps(entity_summaries, ensure_ascii=False)}"
    )
    raw = llm.generate_response(prompt)
    return _extract_json_object(raw)


def _route_with_llm(text: str) -> dict:
    prompt = (
        "You are a router for a voice assistant with optional Home Assistant integration.\n"
        "Return ONLY JSON with schema:\n"
        "{\"ha_related\": bool, \"intent\": \"none|control|query\", \"answer\": string, "
        "\"actions\": [{\"service\": \"domain.service\", \"target\": {\"entity_id\": [\"domain.name\"], \"area_id\": [], \"device_id\": []}, \"service_data\": {}}], "
        "\"query_entities\": [\"domain.name\"]}\n"
        "Rules:\n"
        "1) If user asks general knowledge/chitchat, set ha_related=false, intent=none, and provide answer.\n"
        "2) If user wants to control HA entities, set ha_related=true, intent=control.\n"
        "3) If user asks HA state/weather/temperature from local entities, set ha_related=true, intent=query.\n"
        "4) If unknown but likely non-HA, set ha_related=false.\n"
        f"User text: {text}"
    )
    raw = llm.generate_response(prompt)
    result = _extract_json_object(raw)
    if not isinstance(result, dict):
        return {}
    return result


def _summarize_query_results_with_llm(user_text: str, query_results: list[dict]) -> str:
    prompt = (
        "You are a smart home assistant. Summarize queried Home Assistant states in concise Chinese.\n"
        "If there are units, include them.\n"
        f"User question: {user_text}\n"
        f"Query results JSON: {json.dumps(query_results, ensure_ascii=False)}"
    )
    summary = llm.generate_response(prompt)
    return (summary or "").strip() or "I have fetched the entity states from Home Assistant"


def _build_service_map(services: list[dict]) -> dict:
    service_map = {}
    for item in services:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain")
        svc = item.get("service")
        if not domain or not svc:
            continue
        key = f"{domain}.{svc}"
        service_map[key] = item
    return service_map


def _summarize_services_for_prompt(services: list[dict]) -> list[dict]:
    summaries = []
    for item in services[:400]:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain")
        service = item.get("service")
        if not domain or not service:
            continue
        target = item.get("target") or {}
        fields = item.get("fields") or {}
        summaries.append(
            {
                "service": f"{domain}.{service}",
                "target": {
                    "entity": bool(target.get("entity")),
                    "device": bool(target.get("device")),
                    "area": bool(target.get("area")),
                },
                "fields": list(fields.keys())[:12],
            }
        )
    return summaries


def _extract_json_object(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        return {}

    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    candidate = text[start:end + 1]
    try:
        result = json.loads(candidate)
        if isinstance(result, dict):
            return result
    except Exception:
        return {}
    return {}


def _call_ha_service(service, data):
    try:
        url = _build_service_url(service)
    except ValueError as exc:
        logger.error("Invalid HA service format: %s", exc)
        return False, f"Home Assistant Error: {exc}"

    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        if response.status_code == 200:
            logger.info("HA service %s called successfully", service)
            return True, "Okay, I've done that"
        logger.error("HA service error: %s - %s", response.status_code, response.text)
        return False, f"Home Assistant Error: {response.status_code}"
    except requests.exceptions.ConnectionError:
        logger.error("Cannot connect to Home Assistant")
        return False, "Home Assistant Error: Cannot connect to server"
    except Exception as e:
        logger.error("HA service call error: %s", e)
        return False, f"Home Assistant Error: {str(e)}"

def call_ha_service(service, data):
    """
    Call a Home Assistant service
    
    Args:
        service (str): Service name (e.g., 'light.turn_on')
        data (dict): Service data
    
    Returns:
        str: Response message
    """
    _, msg = _call_ha_service(service, data)
    return msg

def discover_entities():
    """
    Discover available entities from Home Assistant
    
    Returns:
        list: List of available entities
    """
    url = f"{HA_URL}/api/states"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            entities = response.json()
            logger.info(f"Discovered {len(entities)} entities")
            return entities
        else:
            logger.error(f"Entity discovery error: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Entity discovery error: {e}")
        return []


def discover_services():
    """Discover available Home Assistant services."""
    url = f"{HA_URL.rstrip('/')}/api/services"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.error("Service discovery error: %s", response.status_code)
            return []

        raw = response.json()
        results = []
        if not isinstance(raw, list):
            return results

        for domain_block in raw:
            if not isinstance(domain_block, dict):
                continue
            domain = domain_block.get("domain")
            services = domain_block.get("services") or {}
            if not domain or not isinstance(services, dict):
                continue

            for svc_name, svc_detail in services.items():
                svc_detail = svc_detail or {}
                if not isinstance(svc_detail, dict):
                    svc_detail = {}
                results.append(
                    {
                        "domain": domain,
                        "service": svc_name,
                        "target": svc_detail.get("target") or {},
                        "fields": svc_detail.get("fields") or {},
                    }
                )

        logger.info("Discovered %s services", len(results))
        return results
    except Exception as e:
        logger.error("Service discovery error: %s", e)
        return []