"""
Home Assistant integration module for HumbleVoice
Handles HA commands and entity control
"""
import requests
import json
import logging
import re
from urllib.parse import urlparse
from config_loader import get_config
from modules import llm

logger = logging.getLogger(__name__)

cfg = get_config()
ha_cfg = cfg.get("home_assistant", {}) if cfg else {}
HA_URL = ha_cfg.get("url", "http://homeassistant.local:8123")
HA_TOKEN = ha_cfg.get("token", "YOUR_HA_TOKEN")
DEFAULT_AREA = str(ha_cfg.get("default_area", "客厅")).strip()
AREA_ALIASES_CFG = ha_cfg.get("area_aliases") or {}
OUTPUT_LANGUAGE = str(ha_cfg.get("response_language", "zh-CN")).strip()

headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json"
}

UNAVAILABLE_STATES = {"unavailable", "unknown"}
UNKNOWN_FROM_HA_REPLY = "我不知道，Home Assistant 里没有足够的信息。"

IGNORED_ATTRIBUTE_KEYS = {
    "friendly_name",
    "icon",
    "entity_picture",
    "attribution",
    "unit_of_measurement",
    "device_class",
    "state_class",
    "supported_features",
    "restored",
    "editable",
    "assumed_state",
    "options",
    "temperature_unit",
    "pressure_unit",
    "wind_speed_unit",
    "visibility_unit",
    "precipitation_unit",
    "forecast",
}

STATE_LOCALIZATION = {
    "on": "开启",
    "off": "关闭",
    "open": "打开",
    "closed": "关闭",
    "locked": "已锁定",
    "unlocked": "未锁定",
    "home": "在家",
    "not_home": "离家",
    "playing": "播放中",
    "paused": "已暂停",
    "idle": "空闲",
    "unknown": "未知",
    "unavailable": "不可用",
    "clear-night": "晴夜",
    "cloudy": "多云",
    "fog": "有雾",
    "hail": "冰雹",
    "lightning": "雷电",
    "lightning-rainy": "雷阵雨",
    "partlycloudy": "局部多云",
    "pouring": "大雨",
    "rainy": "下雨",
    "snowy": "下雪",
    "snowy-rainy": "雨夹雪",
    "sunny": "晴朗",
    "windy": "有风",
    "windy-variant": "大风",
    "exceptional": "异常",
}

ATTRIBUTE_LOCALIZATION = {
    "temperature": "温度",
    "humidity": "湿度",
    "dew_point": "露点",
    "pressure": "气压",
    "wind_speed": "风速",
    "wind_bearing": "风向",
    "visibility": "能见度",
    "precipitation": "降水",
    "precipitation_probability": "降水概率",
    "apparent_temperature": "体感温度",
    "uv_index": "紫外线指数",
    "battery": "电量",
    "volume_level": "音量",
    "brightness": "亮度",
}

DOMAIN_LOCALIZATION_ZH = {
    "weather": "天气",
    "climate": "空调",
    "light": "灯光",
    "switch": "开关",
    "fan": "风扇",
    "cover": "窗帘",
    "media_player": "媒体播放器",
    "sensor": "传感器",
    "binary_sensor": "传感器",
    "lock": "门锁",
    "vacuum": "扫地机器人",
}

CONTROL_KEYWORDS = {
    "打开", "开启", "关", "关闭", "关掉", "切换", "toggle", "turn on", "turn off",
    "open", "close", "start", "stop", "set", "调到", "设置", "调高", "调低",
}

QUERY_KEYWORDS = {
    "?", "？", "什么", "多少", "几点", "几度", "天气", "温度", "湿度", "状态", "是否", "有没有",
    "what", "when", "where", "who", "which", "how", "temperature", "weather", "status",
}

HOME_CONTEXT_KEYWORDS = {
    "home assistant", "ha", "家里", "家中", "设备", "实体", "自动化", "场景",
    "灯", "开关", "空调", "风扇", "窗帘", "门锁", "温度", "湿度", "天气", "传感器",
}

DOMAIN_HINTS = {
    "天气": "weather",
    "weather": "weather",
    "温度": "sensor",
    "湿度": "sensor",
    "空调": "climate",
    "climate": "climate",
    "灯": "light",
    "light": "light",
    "开关": "switch",
    "switch": "switch",
    "窗帘": "cover",
    "cover": "cover",
    "风扇": "fan",
    "fan": "fan",
    "媒体": "media_player",
    "电视": "media_player",
    "播放器": "media_player",
}

DEVICE_CLASS_HINTS = {
    "温度": "temperature",
    "湿度": "humidity",
    "亮度": "illuminance",
    "门": "door",
    "窗": "window",
    "锁": "lock",
}

DEFAULT_AREA_ALIASES = {
    "客厅": ["客厅", "大厅", "living room", "livingroom"],
    "卧室": ["卧室", "主卧", "次卧", "bedroom"],
    "厨房": ["厨房", "kitchen"],
    "卫生间": ["卫生间", "厕所", "浴室", "bathroom"],
}


def _area_aliases() -> dict[str, list[str]]:
    merged = {k: list(v) for k, v in DEFAULT_AREA_ALIASES.items()}
    if isinstance(AREA_ALIASES_CFG, dict):
        for area_name, aliases in AREA_ALIASES_CFG.items():
            area = str(area_name).strip()
            if not area:
                continue
            values = []
            if isinstance(aliases, list):
                values = [str(a).strip().lower() for a in aliases if str(a).strip()]
            elif isinstance(aliases, str) and aliases.strip():
                values = [aliases.strip().lower()]
            if area not in merged:
                merged[area] = []
            merged[area].extend(values)

    # Normalize aliases and include area name itself.
    normalized = {}
    for area, aliases in merged.items():
        uniq = {area.lower()}
        uniq.update(a.lower() for a in aliases if a)
        normalized[area] = sorted(uniq)
    return normalized


def _detect_area_from_text(text: str) -> str | None:
    lowered = (text or "").lower()
    if not lowered:
        return None

    aliases = _area_aliases()
    for area, words in aliases.items():
        if any(w in lowered for w in words):
            return area
    return None


def _resolve_area_context(text: str) -> str:
    explicit = _detect_area_from_text(text)
    if explicit:
        return explicit
    return DEFAULT_AREA


def _entity_matches_area(entity: dict, area_name: str) -> bool:
    if not area_name:
        return False
    aliases = _area_aliases().get(area_name, [area_name.lower()])
    hay = _entity_search_text(entity)
    return any(alias in hay for alias in aliases)


def _area_context_instruction(text: str) -> str:
    area = _resolve_area_context(text)
    explicit = _detect_area_from_text(text)
    if explicit:
        return f"Area context: user explicitly mentioned area '{explicit}'. Prioritize this area."
    return (
        f"Area context: device is located in '{area}'. "
        "For generic home commands without explicit area, prioritize this area first."
    )


def _tokenize(text: str) -> list[str]:
    lowered = (text or "").lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    return [t for t in tokens if t]


def _entity_search_text(entity: dict) -> str:
    attrs = entity.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    parts = [
        str(entity.get("entity_id", "")),
        str(attrs.get("friendly_name", "")),
        str(attrs.get("device_class", "")),
        str(attrs.get("icon", "")),
    ]
    return " ".join(parts).lower()


def _infer_domain_hints(text: str) -> set[str]:
    lowered = (text or "").lower()
    hints = set()
    for key, domain in DOMAIN_HINTS.items():
        if key in lowered:
            hints.add(domain)
    return hints


def _infer_device_class_hints(text: str) -> set[str]:
    lowered = (text or "").lower()
    hints = set()
    for key, device_class in DEVICE_CLASS_HINTS.items():
        if key in lowered:
            hints.add(device_class)
    return hints


def _rank_entities(text: str, states: list[dict], limit: int = 60) -> list[dict]:
    tokens = _tokenize(text)
    domain_hints = _infer_domain_hints(text)
    area_context = _resolve_area_context(text)
    ranked = []

    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not entity_id:
            continue

        hay = _entity_search_text(item)
        score = 0

        for token in tokens:
            if token in hay:
                score += 2
        if entity_id in hay:
            score += 1

        domain = str(entity_id).split(".", 1)[0]
        if domain_hints and domain in domain_hints:
            score += 5

        if area_context and _entity_matches_area(item, area_context):
            score += 3

        # Prefer available entities only after textual/domain relevance is established.
        state = str(item.get("state", "")).lower()
        if score > 0 and state not in UNAVAILABLE_STATES:
            score += 1

        if score > 0:
            ranked.append((score, item))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [it for _, it in ranked[:limit]]


def _detect_intent_rule(text: str) -> str:
    lowered = (text or "").lower().strip()
    if not lowered:
        return "none"

    if any(k in lowered for k in CONTROL_KEYWORDS):
        return "control"
    if any(k in lowered for k in QUERY_KEYWORDS) and _has_home_context_signal(text):
        return "query"
    return "none"


def _extract_intent_slots(text: str) -> dict:
    lowered = (text or "").lower().strip()
    area = _resolve_area_context(text)
    domains = sorted(_infer_domain_hints(text))
    device_classes = sorted(_infer_device_class_hints(text))
    intent = _detect_intent_rule(text)

    name_text = lowered
    remove_terms = set(CONTROL_KEYWORDS) | set(QUERY_KEYWORDS)
    for aliases in _area_aliases().values():
        remove_terms.update(aliases)
    for term in sorted(remove_terms, key=len, reverse=True):
        if term:
            name_text = name_text.replace(term.lower(), " ")
    name_text = " ".join(name_text.split())

    return {
        "intent": intent,
        "slots": {
            "name": name_text,
            "area": area,
            "domain": domains,
            "device_class": device_classes,
        },
    }


def _match_entities_by_slots(states: list[dict], slots: dict, limit: int = 8) -> list[str]:
    if not states:
        return []

    slot_area = str(slots.get("area") or "").strip()
    slot_domains = slots.get("domain") or []
    if isinstance(slot_domains, str):
        slot_domains = [slot_domains]
    slot_domains = [str(d).strip() for d in slot_domains if str(d).strip()]

    slot_device_classes = slots.get("device_class") or []
    if isinstance(slot_device_classes, str):
        slot_device_classes = [slot_device_classes]
    slot_device_classes = [str(d).strip().lower() for d in slot_device_classes if str(d).strip()]

    slot_name = str(slots.get("name") or "").strip().lower()

    scored = []
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not entity_id:
            continue

        domain = str(entity_id).split(".", 1)[0]
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        dev_cls = str(attrs.get("device_class", "")).lower()
        hay = _entity_search_text(item)

        score = 0
        if slot_area and _entity_matches_area(item, slot_area):
            score += 6
        if slot_domains and domain in slot_domains:
            score += 5
        if slot_device_classes and dev_cls in slot_device_classes:
            score += 4
        if slot_name and slot_name in hay:
            score += 3

        # Keep broad compatibility: if no slot constraints, fall back to generic ranking later.
        if score > 0:
            state = str(item.get("state", "")).lower()
            if state not in UNAVAILABLE_STATES:
                score += 1
            scored.append((score, entity_id))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = []
    for _, entity_id in scored:
        if entity_id not in selected:
            selected.append(entity_id)
        if len(selected) >= limit:
            break
    return selected


def _is_ha_related_rule(text: str) -> bool:
    lowered = (text or "").lower()
    if any(k in lowered for k in CONTROL_KEYWORDS):
        return True
    if _has_home_context_signal(text):
        return True
    return False


def _has_home_context_signal(text: str) -> bool:
    lowered = (text or "").lower()
    if any(k in lowered for k in DOMAIN_HINTS.keys()):
        return True
    if any(k in lowered for k in HOME_CONTEXT_KEYWORDS):
        return True
    return False


def _filter_query_entities(text: str, entities: list[dict]) -> list[dict]:
    if not entities:
        return entities

    hints = _infer_domain_hints(text)

    if not hints:
        return entities

    filtered = []
    for item in entities:
        entity_id = str(item.get("entity_id", ""))
        if not entity_id:
            continue
        domain = entity_id.split(".", 1)[0]

        if domain not in hints:
            continue
        filtered.append(item)

    return filtered or entities


def _pick_query_candidates(text: str, states: list[dict], limit: int = 8) -> list[str]:
    ranked = _rank_entities(text, states, limit=60)
    ranked = _filter_query_entities(text, ranked)
    selected = []
    for item in ranked:
        entity_id = item.get("entity_id")
        if entity_id and entity_id not in selected:
            selected.append(entity_id)
        if len(selected) >= limit:
            break
    return selected


def _entity_ids_preview(states: list[dict], max_items: int = 30) -> list[str]:
    ids = []
    for item in states or []:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if entity_id:
            ids.append(entity_id)
    return ids[:max_items]


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


def _candidate_ha_base_urls() -> list[str]:
    primary = (HA_URL or "").rstrip("/")
    candidates = []
    if primary:
        candidates.append(primary)

    try:
        parsed = urlparse(primary)
    except Exception:
        parsed = None

    fallback_bases = [
        "http://localhost:8123",
        "http://127.0.0.1:8123",
        "http://homeassistant:8123",
    ]

    if parsed and parsed.hostname == "homeassistant.local":
        candidates.extend(fallback_bases)

    deduped = []
    seen = set()
    for url in candidates:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _ha_request(method: str, api_path: str, json_payload: dict | None = None, timeout: int = 10):
    last_error = None
    method_lower = (method or "get").lower()

    for base in _candidate_ha_base_urls():
        url = f"{base.rstrip('/')}/{api_path.lstrip('/')}"
        try:
            if method_lower == "post":
                response = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
            else:
                response = requests.get(url, headers=headers, timeout=timeout)

            if base.rstrip("/") != HA_URL.rstrip("/"):
                logger.warning("HA request fallback succeeded via %s", base)
            return response
        except requests.exceptions.RequestException as exc:
            last_error = exc
            logger.warning("HA request failed via %s: %s", base, exc)

    raise last_error if last_error else RuntimeError("Home Assistant request failed")

def is_ha_command(text):
    """Backward-compatible keyword check. Not used in new main flow."""
    ha_keywords = [
        "turn on", "turn off", "light", "switch", "lamp", "fan", "ac", "heater", "thermostat",
        "打开", "关闭", "开灯", "关灯", "灯", "开关", "风扇", "空调", "暖气", "温控", "天气", "温度",
    ]
    text_lower = (text or "").lower()
    return any(keyword in text_lower for keyword in ha_keywords)


def handle_user_text(text: str) -> str:
    """HA-first flow: resolve intent/entities programmatically, then use LLM in constrained mode."""
    if not text or not text.strip():
        return "I didn't catch that."

    # HA-like flow: intent recognition first, then fallback on no_intent_match.
    route = _route_with_llm(text)
    recognition = _recognize_ha_intent_like_ha(text, route)
    intent = recognition.get("intent", "none")
    logger.info(
        "Intent recognition: matched=%s, reason=%s, intent=%s",
        recognition.get("matched"),
        recognition.get("reason"),
        intent,
    )

    if not recognition.get("matched"):
        answer = (route.get("answer") or "").strip()
        return answer or llm.generate_response(text, temperature=0.5, max_tokens=220, retry_on_empty=True)

    logger.info("HA-related request detected, querying Home Assistant entities")
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

    intent_slots = _extract_intent_slots(text)
    logger.info("Intent slots (control): %s", json.dumps(intent_slots, ensure_ascii=False))

    plan = {"actions": []}
    slot_targets = _match_entities_by_slots(states, intent_slots.get("slots") or {}, limit=8)
    if slot_targets:
        action_type = _detect_control_action_type(text)
        if action_type:
            grouped = {}
            for entity_id in slot_targets:
                domain = entity_id.split(".", 1)[0]
                service = _pick_service_for_domain(domain, action_type, service_map)
                if not service:
                    continue
                grouped.setdefault(service, []).append(entity_id)

            actions = []
            for service, entity_list in grouped.items():
                actions.append(
                    {
                        "service": service,
                        "target": {"entity_id": entity_list, "area_id": [], "device_id": []},
                        "service_data": {},
                    }
                )
            plan = {"actions": actions}

    if not plan.get("actions"):
        plan = _plan_ha_actions_with_rules(text, states, services)
    if not plan.get("actions"):
        plan = {"actions": route.get("actions") or []}
    if not plan["actions"]:
        plan = _plan_ha_actions_with_llm(text, states, services)

    planned_actions = plan.get("actions", [])
    logger.info("LLM control plan actions: %s", json.dumps(planned_actions, ensure_ascii=False))
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
    intent_slots = _extract_intent_slots(text)
    logger.info("Intent slots (query): %s", json.dumps(intent_slots, ensure_ascii=False))

    query_entities = route.get("query_entities") or []
    if not isinstance(query_entities, list):
        query_entities = []

    slot_matched = _match_entities_by_slots(states, intent_slots.get("slots") or {}, limit=8)
    if slot_matched:
        query_entities = slot_matched

    # Prefer deterministic match from full entity set.
    ranked_ids = _pick_query_candidates(text, states, limit=8)
    if ranked_ids:
        if not query_entities:
            query_entities = ranked_ids

    if not query_entities:
        query_plan = _plan_ha_queries_with_llm(text, states)
        query_entities = query_plan.get("query_entities") or []

    # Validate and de-duplicate query entities against current HA states.
    state_map = {item.get("entity_id"): item for item in states if isinstance(item, dict) and item.get("entity_id")}
    valid_query_entities = []
    for entity_id in query_entities:
        state_obj = state_map.get(entity_id)
        if not state_obj:
            continue
        if state_obj in _filter_query_entities(text, [state_obj]):
            if entity_id not in valid_query_entities:
                valid_query_entities.append(entity_id)
    query_entities = valid_query_entities

    logger.info("Selected query entities: %s", query_entities)

    if not query_entities:
        # Fallback: still try to answer from full HA entities before admitting unknown.
        return _answer_with_entities_grounded(text, states)

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
                "attributes": _extract_compact_attributes(state_obj.get("attributes") or {}),
            }
        )

    if not query_results:
        return UNKNOWN_FROM_HA_REPLY

    facts = _build_query_facts(query_results)
    summary = _naturalize_summary_with_llm(text, facts, query_results)
    if missing:
        return f"{summary} (Missing entities: {', '.join(missing)})"
    return summary


def _extract_compact_attributes(attrs: dict, max_items: int = 4) -> dict:
    if not isinstance(attrs, dict):
        return {}

    out = {}
    for key, value in attrs.items():
        key_str = str(key)
        if key_str in IGNORED_ATTRIBUTE_KEYS:
            continue
        if key_str.endswith("_id"):
            continue
        if isinstance(value, (dict, list, tuple)):
            continue
        if isinstance(value, str) and len(value) > 40:
            continue
        out[key_str] = value
        if len(out) >= max_items:
            break
    return out


def _build_query_facts(query_results: list[dict]) -> str:
    if not query_results:
        return ""

    compact = []
    for item in query_results:
        compact.append(
            {
                "entity_id": item.get("entity_id"),
                "friendly_name": item.get("friendly_name") or item.get("entity_id"),
                "state": item.get("state"),
                "unit": item.get("unit"),
                "attributes": item.get("attributes") or {},
            }
        )
    return json.dumps(compact, ensure_ascii=False)


def _naturalize_summary_with_llm(user_text: str, facts_json: str, query_results: list[dict]) -> str:
    if not facts_json:
        return UNKNOWN_FROM_HA_REPLY

    lang = _detect_response_language(user_text)
    if _is_chinese_output(lang):
        language_hint = "请使用自然、口语化、简洁中文回答。"
    else:
        language_hint = "Respond in natural, concise spoken English."

    target_language = "Chinese" if _is_chinese_output(lang) else "English"
    fallback_text = UNKNOWN_FROM_HA_REPLY if _is_chinese_output(lang) else "I don't know based on Home Assistant data."

    prompt = (
        "You are a voice assistant response generator for Home Assistant tool results.\n"
        "STRICT RULES:\n"
        f"1) Respond in natural spoken {target_language}.\n"
        "2) Do NOT add any new facts, values, entities, or predictions.\n"
        "3) Include all key facts from tool results that are relevant to the user question.\n"
        "4) For each selected entity, prioritize: state + all scalar numeric/boolean attributes available in the JSON.\n"
        "5) Convert raw keys into user-friendly wording, but do not change values.\n"
        "6) Keep the answer concise (1-2 sentences when possible).\n"
        "7) Output plain text only and never output empty text or ellipsis.\n"
        "8) If facts are insufficient, return exactly the fallback text.\n"
        f"Fallback text: {fallback_text}\n"
        f"{language_hint}\n"
        f"User question: {user_text}\n"
        f"Tool results JSON (source of truth): {facts_json}\n"
        f"Raw query results JSON: {json.dumps(query_results, ensure_ascii=False)}"
    )

    logger.info("Naturalization input facts: %s", facts_json)
    rewritten = (llm.generate_response(prompt, temperature=0.15, max_tokens=260, retry_on_empty=True) or "").strip()
    logger.info("Naturalization output summary: %s", rewritten)
    if rewritten and not _looks_like_unknown_answer(rewritten):
        return rewritten

    # Retry with a shorter, simpler instruction in case the model returned empty output.
    retry_prompt = (
        f"Answer in {target_language}. "
        "Use ONLY the JSON facts below. "
        "Keep it natural and concise. Include state and all relevant scalar numeric/boolean attributes. "
        "Do not add new facts.\n"
        f"Question: {user_text}\n"
        f"Facts JSON: {facts_json}"
    )
    retry = (llm.generate_response(retry_prompt, temperature=0.0, max_tokens=220, retry_on_empty=True) or "").strip()
    logger.info("Naturalization retry summary: %s", retry)
    if retry and not _looks_like_unknown_answer(retry):
        return retry

    # Final safeguard: if we already have HA facts, never answer unknown.
    if query_results:
        return _fallback_natural_from_facts(query_results, lang)

    return rewritten


def _looks_like_unknown_answer(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    if lowered in {"...", "…", "-", "--"}:
        return True
    if re.fullmatch(r"[.。!！?？…\-\s]+", lowered):
        return True
    patterns = [
        "我不知道",
        "不知道",
        "无法确定",
        "没有足够",
        "i don't know",
        "cannot determine",
        "not enough information",
    ]
    return any(p in lowered for p in patterns)


def _fallback_natural_from_facts(query_results: list[dict], lang: str) -> str:
    if not query_results:
        return UNKNOWN_FROM_HA_REPLY if _is_chinese_output(lang) else "I don't know based on Home Assistant data."

    def _humanize_entity_name(item: dict) -> str:
        friendly = str(item.get("friendly_name") or "").strip()
        entity_id = str(item.get("entity_id") or "").strip()
        if friendly:
            if _is_chinese_output(lang) and re.fullmatch(r"[A-Za-z0-9 _.-]+", friendly):
                domain = entity_id.split(".", 1)[0] if entity_id else ""
                return DOMAIN_LOCALIZATION_ZH.get(domain, friendly)
            return friendly
        if entity_id and _is_chinese_output(lang):
            domain = entity_id.split(".", 1)[0]
            return DOMAIN_LOCALIZATION_ZH.get(domain, entity_id)
        return entity_id or ("未知实体" if _is_chinese_output(lang) else "unknown entity")

    def _humanize_state(val):
        val_str = str(val)
        if _is_chinese_output(lang):
            return STATE_LOCALIZATION.get(val_str.lower(), val_str)
        return val_str

    def _format_attr(key: str, val) -> str | None:
        if val is None:
            return None
        key_str = str(key)
        val_str = str(val)
        if _is_chinese_output(lang):
            key_local = ATTRIBUTE_LOCALIZATION.get(key_str, key_str)
            if key_str in {"temperature", "dew_point", "apparent_temperature"} and not val_str.endswith(("℃", "°C")):
                val_str = f"{val_str}℃"
            if key_str == "humidity" and not val_str.endswith("%"):
                val_str = f"{val_str}%"
            return f"{key_local}{val_str}"
        return f"{key_str} {val_str}"

    first = query_results[0]
    first_name = _humanize_entity_name(first)
    first_state = _humanize_state(first.get("state"))
    attrs = first.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}

    ordered_keys = ["temperature", "humidity", "dew_point", "battery", "brightness", "volume_level"]
    attr_parts = []
    for key in ordered_keys:
        if key in attrs:
            part = _format_attr(key, attrs.get(key))
            if part:
                attr_parts.append(part)
    for key, val in attrs.items():
        if key in ordered_keys:
            continue
        part = _format_attr(key, val)
        if part:
            attr_parts.append(part)
        if len(attr_parts) >= 4:
            break

    if _is_chinese_output(lang):
        sentence = f"{first_name}现在是{first_state}"
        if attr_parts:
            sentence += "，" + "，".join(attr_parts)
        if len(query_results) > 1:
            sentence += f"。另外还查询到{len(query_results)-1}个相关实体"
        return sentence + "。"

    sentence = f"{first_name} is currently {first_state}"
    if attr_parts:
        sentence += ", " + ", ".join(attr_parts)
    if len(query_results) > 1:
        sentence += f". I also found {len(query_results)-1} additional related entities"
    return sentence + "."


def _detect_response_language(user_text: str) -> str:
    text = (user_text or "").strip()
    if not text:
        return OUTPUT_LANGUAGE

    # Chinese characters present -> Chinese response.
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-CN"

    # Mostly Latin letters -> English response.
    letters = re.findall(r"[A-Za-z]", text)
    if letters:
        return "en"

    return OUTPUT_LANGUAGE


def _is_chinese_output(lang: str | None = None) -> bool:
    lang = ((lang or OUTPUT_LANGUAGE) or "").lower()
    return lang.startswith("zh")


def _plan_ha_actions_with_rules(text: str, entities: list[dict], services: list[dict]) -> dict:
    action_type = _detect_control_action_type(text)

    if not action_type:
        return {"actions": []}

    ranked = _rank_entities(text, entities, limit=30)
    area_context = _resolve_area_context(text)
    target_ids = []

    area_first = [item for item in ranked if _entity_matches_area(item, area_context)]
    others = [item for item in ranked if not _entity_matches_area(item, area_context)]
    ordered = area_first + others
    for item in ordered:
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        state = str(item.get("state", "")).lower()
        if state in UNAVAILABLE_STATES:
            continue
        target_ids.append(entity_id)

    if not target_ids:
        return {"actions": []}

    service_map = _build_service_map(services)
    grouped: dict[str, list[str]] = {}
    for entity_id in target_ids[:8]:
        domain = entity_id.split(".", 1)[0]
        service = _pick_service_for_domain(domain, action_type, service_map)
        if not service:
            continue
        grouped.setdefault(service, []).append(entity_id)

    actions = []
    for service, entity_list in grouped.items():
        actions.append(
            {
                "service": service,
                "target": {"entity_id": entity_list, "area_id": [], "device_id": []},
                "service_data": {},
            }
        )

    logger.info("Rule-based control plan actions: %s", json.dumps(actions, ensure_ascii=False))
    return {"actions": actions}


def _pick_service_for_domain(domain: str, action_type: str, service_map: dict) -> str | None:
    candidates = []
    if action_type == "turn_on":
        candidates = [f"{domain}.turn_on", "homeassistant.turn_on"]
    elif action_type == "turn_off":
        candidates = [f"{domain}.turn_off", "homeassistant.turn_off"]
    elif action_type == "toggle":
        candidates = [f"{domain}.toggle", "homeassistant.toggle"]

    for service in candidates:
        if service in service_map:
            return service
    return None


def _detect_control_action_type(text: str) -> str | None:
    lowered = (text or "").lower()
    if any(k in lowered for k in ["打开", "开启", "turn on", "open", "start"]):
        return "turn_on"
    if any(k in lowered for k in ["关闭", "关掉", "turn off", "close", "stop"]):
        return "turn_off"
    if "toggle" in lowered or "切换" in lowered:
        return "toggle"
    return None


def _plan_ha_actions_with_llm(text: str, entities: list[dict], services: list[dict]) -> dict:
    candidates = _rank_entities(text, entities, limit=80)
    entity_summaries = []
    for item in candidates:
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
        f"{_area_context_instruction(text)}\n"
        f"User command: {text}\n"
        f"Entities JSON: {json.dumps(entity_summaries, ensure_ascii=False)}\n"
        f"Services JSON: {json.dumps(service_summaries, ensure_ascii=False)}"
    )

    raw = llm.generate_response(prompt, temperature=0.1, max_tokens=260)
    parsed = _extract_json_object(raw)
    logger.info("LLM raw control JSON parsed: %s", json.dumps(parsed, ensure_ascii=False))
    return parsed


def _plan_ha_queries_with_llm(text: str, entities: list[dict]) -> dict:
    candidates = _rank_entities(text, entities, limit=100)
    candidates = _filter_query_entities(text, candidates)
    entity_summaries = []
    for item in candidates:
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

    prompt = (
        "You map user query to Home Assistant entities.\n"
        "Return ONLY JSON with schema: {\"query_entities\": [\"domain.name\"]}.\n"
        "Rules:\n"
        "1) Select only from provided entity list.\n"
        "2) If no confident mapping, return {\"query_entities\": []}.\n"
        f"{_area_context_instruction(text)}\n"
        f"User command: {text}\n"
        f"Entities JSON: {json.dumps(entity_summaries, ensure_ascii=False)}"
    )
    raw = llm.generate_response(prompt, temperature=0.1, max_tokens=220)
    parsed = _extract_json_object(raw)
    logger.info("LLM raw query JSON parsed: %s", json.dumps(parsed, ensure_ascii=False))
    return parsed


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
        "3) If user asks HA state from local entities, set ha_related=true, intent=query.\n"
        "4) If unknown but likely non-HA, set ha_related=false.\n"
        f"{_area_context_instruction(text)}\n"
        f"User text: {text}"
    )
    raw = llm.generate_response(prompt, temperature=0.1, max_tokens=180)
    result = _extract_json_object(raw)
    logger.info("LLM route JSON parsed: %s", json.dumps(result, ensure_ascii=False))
    if not isinstance(result, dict):
        return {}
    return result


def _summarize_query_results_with_llm(user_text: str, query_results: list[dict]) -> str:
    prompt = (
        "你是一个严格基于 Home Assistant 数据回答的助手。\n"
        "只能根据 Query results JSON 回答，不能补充或猜测任何 JSON 之外的信息。\n"
        f"如果 JSON 不足以回答用户问题，必须原样返回：{UNKNOWN_FROM_HA_REPLY}\n"
        f"{_area_context_instruction(user_text)}\n"
        "请用简洁中文回答，若有单位请带上。\n"
        f"User question: {user_text}\n"
        f"Query results JSON: {json.dumps(query_results, ensure_ascii=False)}"
    )
    summary = llm.generate_response(prompt)
    cleaned = (summary or "").strip()
    return cleaned or UNKNOWN_FROM_HA_REPLY


def _answer_with_entities_grounded(user_text: str, states: list[dict]) -> str:
    if not states:
        return UNKNOWN_FROM_HA_REPLY

    logger.info(
        "Grounded answer with HA entities: total=%s, preview=%s",
        len(states),
        _entity_ids_preview(states),
    )

    candidates = _rank_entities(user_text, states, limit=120)
    if not candidates:
        candidates = states

    entity_summaries = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        entity_summaries.append(
            {
                "entity_id": entity_id,
                "state": item.get("state"),
                "friendly_name": attrs.get("friendly_name", ""),
                "unit": attrs.get("unit_of_measurement", ""),
                "device_class": attrs.get("device_class", ""),
            }
        )

    if not entity_summaries:
        return UNKNOWN_FROM_HA_REPLY

    prompt = (
        "你是一个严格基于 Home Assistant 实体数据回答的助手。\n"
        "只允许使用给定 Entities JSON 中的信息回答。\n"
        "禁止猜测、禁止补充常识、禁止编造。\n"
        f"如果实体信息不足，必须原样返回：{UNKNOWN_FROM_HA_REPLY}\n"
        f"{_area_context_instruction(user_text)}\n"
        "回答使用简洁中文。\n"
        f"User question: {user_text}\n"
        f"Entities JSON: {json.dumps(entity_summaries, ensure_ascii=False)}"
    )
    answer = (llm.generate_response(prompt) or "").strip()
    return answer or UNKNOWN_FROM_HA_REPLY


def _looks_like_information_request(text: str) -> bool:
    text_lower = (text or "").lower()
    if not text_lower:
        return False
    return any(k in text_lower for k in QUERY_KEYWORDS)


def _recognize_ha_intent_like_ha(text: str, route: dict) -> dict:
    """Two-stage recognition similar to HA's intent-first behavior."""
    strict_intent = _detect_intent_rule(text)
    if strict_intent in {"control", "query"}:
        return {"matched": True, "intent": strict_intent, "reason": "strict"}

    routed_intent = str((route or {}).get("intent", "none")).strip().lower()
    routed_ha_related = bool((route or {}).get("ha_related", False))
    if routed_intent in {"control", "query"} and (
        routed_ha_related or _has_home_context_signal(text)
    ):
        return {"matched": True, "intent": routed_intent, "reason": "router"}

    return {"matched": False, "intent": "none", "reason": "no_intent_match"}


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
        service = (service or "").strip()
        if not service:
            raise ValueError("service is empty")

        if "/" in service:
            domain, action = service.split("/", 1)
        elif "." in service:
            domain, action = service.split(".", 1)
        else:
            raise ValueError(f"invalid service format: {service}")

        api_path = f"api/services/{domain.strip()}/{action.strip()}"
    except ValueError as exc:
        logger.error("Invalid HA service format: %s", exc)
        return False, f"Home Assistant Error: {exc}"

    try:
        response = _ha_request("post", api_path, json_payload=data, timeout=10)
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
    try:
        response = _ha_request("get", "api/states", timeout=10)
        
        if response.status_code == 200:
            entities = response.json()
            logger.info(f"Discovered {len(entities)} entities")
            logger.info("HA entity list preview: %s", _entity_ids_preview(entities))
            logger.debug("HA entity full payload: %s", json.dumps(entities, ensure_ascii=False))
            return entities
        else:
            logger.error(f"Entity discovery error: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Entity discovery error: {e}")
        return []


def discover_services():
    """Discover available Home Assistant services."""
    try:
        response = _ha_request("get", "api/services", timeout=10)
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