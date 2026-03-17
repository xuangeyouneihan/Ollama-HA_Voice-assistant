"""
Home Assistant integration module for HumbleVoice
Handles HA commands and entity control
"""
import requests
import json
import logging
import re
import os
import io
import wave
import asyncio
import socket
import numpy as np
import yaml
from urllib.parse import urlparse, urlunparse
import websockets
from config_loader import get_config
from modules import llm, presets

import av

logger = logging.getLogger(__name__)

cfg = get_config()
audio_cfg = cfg.get("audio", {}) if cfg else {}
ha_cfg = cfg.get("home_assistant", {}) if cfg else {}
HA_URL = ha_cfg.get("url", "http://homeassistant.local:8123")
HA_TOKEN = ha_cfg.get("token", "YOUR_HA_TOKEN")
DEFAULT_AREA = str(ha_cfg.get("default_area", "客厅")).strip()
AREA_ALIASES_CFG = ha_cfg.get("area_aliases") or {}
OUTPUT_LANGUAGE = str(ha_cfg.get("response_language", "zh-CN")).strip()
ASSIST_AUDIO_MODE = bool(ha_cfg.get("assist_audio_mode", False))
ASSIST_PIPELINE_ID = str(ha_cfg.get("assist_pipeline_id", "")).strip()
ASSIST_INPUT_SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))
ASSIST_AUDIO_TIMEOUT_S = float(ha_cfg.get("assist_audio_timeout_s", 45))

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

DOMAIN_LOCALIZATION_EN = {
    "weather": "weather",
    "climate": "climate",
    "light": "lights",
    "switch": "switches",
    "fan": "fans",
    "cover": "covers",
    "media_player": "media",
    "sensor": "sensors",
    "binary_sensor": "sensors",
    "lock": "locks",
    "vacuum": "vacuum",
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

TASK_CREATION_KEYWORDS = {
    "计划任务", "自动化任务", "创建自动化", "创建计划", "定时", "每天", "每周", "每月",
    "到点", "如果", "当", "schedule", "scheduled", "automation", "create automation",
}

TASK_UPDATE_KEYWORDS = {
    "修改任务", "更新任务", "编辑任务", "调整任务", "改成", "改为", "change task", "update task", "edit task",
}

TASK_DELETE_KEYWORDS = {
    "删除任务", "取消任务", "移除任务", "删掉任务", "delete task", "remove task", "cancel automation",
}

_preset_store = presets.build_store_from_config()


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


def use_assist_audio_mode() -> bool:
    return ASSIST_AUDIO_MODE


def _is_name_resolution_error(exc: Exception) -> bool:
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == -2:
        return True
    message = str(exc).lower()
    return (
        "name or service not known" in message
        or "failed to resolve" in message
        or "name resolution" in message
    )


def _build_ws_url(base_http_url: str) -> str:
    parsed = urlparse((base_http_url or "").rstrip("/"))
    if not parsed.netloc:
        raise ValueError(f"invalid Home Assistant URL: {base_http_url}")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/api/websocket", "", "", ""))


def _build_candidate_tts_urls(tts_url: str) -> list[str]:
    raw = str(tts_url or "").strip()
    if not raw:
        return []

    parsed = urlparse(raw)
    candidates = []

    if parsed.scheme and parsed.netloc:
        candidates.append(raw)
        if parsed.hostname == "homeassistant.local":
            for base in _candidate_ha_base_urls():
                parsed_base = urlparse(base)
                if not parsed_base.scheme or not parsed_base.netloc:
                    continue
                alt = urlunparse(
                    (
                        parsed_base.scheme,
                        parsed_base.netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
                candidates.append(alt)
    else:
        for base in _candidate_ha_base_urls():
            candidates.append(f"{base.rstrip('/')}/{raw.lstrip('/')}")

    deduped = []
    seen = set()
    for url in candidates:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _extract_speech_text(intent_output: dict | None) -> str:
    intent_output = intent_output or {}
    response = intent_output.get("response") or {}
    speech = response.get("speech") or {}
    if not isinstance(speech, dict):
        return ""
    plain_raw = speech.get("plain")
    ssml_raw = speech.get("ssml")
    plain = plain_raw if isinstance(plain_raw, dict) else {}
    ssml = ssml_raw if isinstance(ssml_raw, dict) else {}
    return str(plain.get("speech") or ssml.get("speech") or "").strip()


def _decode_wav_if_possible(audio_bytes: bytes) -> tuple[bytes, int, int, int] | None:
    if not audio_bytes or len(audio_bytes) < 44:
        return None
    if not audio_bytes.startswith(b"RIFF"):
        return None

    with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        pcm = wf.readframes(wf.getnframes())
    return pcm, sample_rate, sample_width, channels


def _looks_like_mp3(audio_bytes: bytes) -> bool:
    if not audio_bytes:
        return False
    if audio_bytes.startswith(b"ID3"):
        return True
    if len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and (audio_bytes[1] & 0xE0) == 0xE0:
        return True
    return False


def _looks_like_ogg(audio_bytes: bytes) -> bool:
    return bool(audio_bytes and audio_bytes.startswith(b"OggS"))


def _layout_channel_count(layout_obj) -> int:
    if layout_obj is None:
        return 0
    channels_obj = getattr(layout_obj, "channels", None)
    if isinstance(channels_obj, int):
        return channels_obj
    if isinstance(channels_obj, (list, tuple)):
        return len(channels_obj)
    try:
        return int(channels_obj or 0)
    except Exception:
        return 0


def _decode_compressed_audio_if_possible(audio_bytes: bytes, mime_type: str = "") -> tuple[bytes, int, int, int] | None:
    if not audio_bytes:
        return None

    lower_mime = str(mime_type or "").lower()
    should_try = (
        "audio/mpeg" in lower_mime
        or "audio/mp3" in lower_mime
        or "audio/ogg" in lower_mime
        or "vorbis" in lower_mime
        or _looks_like_mp3(audio_bytes)
        or _looks_like_ogg(audio_bytes)
    )
    if not should_try:
        return None

    try:
        with av.open(io.BytesIO(audio_bytes), mode="r") as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                return None

            pcm_chunks = []
            sample_rate = int(getattr(stream, "rate", 0) or 0)
            channels = 0
            resampler = None

            for frame in container.decode(stream):
                frame_rate = int(getattr(frame, "sample_rate", 0) or sample_rate or 44100)
                if frame_rate <= 0:
                    frame_rate = 44100

                frame_channels = _layout_channel_count(getattr(frame, "layout", None))
                if frame_channels <= 0:
                    frame_channels = _layout_channel_count(getattr(stream, "layout", None)) or 2

                target_layout = "mono" if frame_channels == 1 else "stereo"
                resampler = av.audio.resampler.AudioResampler(
                    format="s16",
                    layout=target_layout,
                    rate=frame_rate,
                )

                resampled = resampler.resample(frame)
                if resampled is None:
                    continue
                if not isinstance(resampled, list):
                    resampled = [resampled]

                for out in resampled:
                    arr = out.to_ndarray()
                    if arr is None:
                        continue

                    out_channels = _layout_channel_count(getattr(out, "layout", None)) or frame_channels
                    if out_channels <= 0:
                        out_channels = 2

                    if arr.dtype != np.int16:
                        if np.issubdtype(arr.dtype, np.floating):
                            arr = np.clip(arr, -1.0, 1.0)
                            arr = (arr * 32767.0).astype(np.int16)
                        else:
                            arr = arr.astype(np.int16)

                    if arr.ndim == 1:
                        channels = out_channels
                        pcm_chunks.append(arr.reshape(-1).astype(np.int16, copy=False).tobytes())
                        sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)
                        continue

                    if arr.ndim == 2 and arr.shape[0] == 1 and out_channels > 1:
                        channels = out_channels
                        pcm_chunks.append(arr.reshape(-1).astype(np.int16, copy=False).tobytes())
                        sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)
                        continue

                    if arr.ndim == 2:
                        channels = out_channels if out_channels > 0 else int(arr.shape[0])
                        # Convert shape (channels, samples) -> interleaved int16 bytes.
                        pcm_chunks.append(arr.T.astype(np.int16, copy=False).tobytes())
                        sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)
                        continue

                    channels = out_channels
                    pcm_chunks.append(arr.reshape(-1).astype(np.int16, copy=False).tobytes())
                    sample_rate = int(getattr(out, "sample_rate", 0) or frame_rate)

            if not pcm_chunks:
                return None

            pcm = b"".join(pcm_chunks)
            if sample_rate <= 0:
                sample_rate = 44100
            if channels <= 0:
                channels = 2

        sample_width = 2
        return pcm, sample_rate, sample_width, channels
    except Exception as exc:
        logger.warning("Failed to decode compressed TTS audio with PyAV: %s", exc)
        return None


def _decode_tts_audio_if_possible(audio_bytes: bytes, mime_type: str = "") -> tuple[bytes, int, int, int] | None:
    decoded = _decode_wav_if_possible(audio_bytes)
    if decoded is not None:
        return decoded
    return _decode_compressed_audio_if_possible(audio_bytes, mime_type=mime_type)


def _download_tts_audio(url: str, timeout: int = 20) -> tuple[bytes, str]:
    last_error = None
    auth_headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    candidates = _build_candidate_tts_urls(url)
    for idx, candidate in enumerate(candidates):
        try:
            resp = requests.get(candidate, headers=auth_headers, timeout=timeout)
            resp.raise_for_status()
            if idx > 0:
                logger.warning("HA TTS download fallback succeeded via %s", candidate)
            return resp.content, str(resp.headers.get("Content-Type", "")).strip()
        except requests.RequestException as exc:
            last_error = exc
            if idx == 0 and _is_name_resolution_error(exc) and len(candidates) > 1:
                logger.warning(
                    "HA TTS primary host resolve failed via %s, trying fallbacks: %s",
                    candidate,
                    ", ".join(candidates[1:]),
                )
            logger.warning("Failed to download HA TTS audio via %s: %s", candidate, exc)
    raise last_error if last_error else RuntimeError("failed to download HA TTS audio")


async def process_audio_with_assist_pipeline(audio_data: bytes, sample_rate: int | None = None) -> dict:
    if not audio_data:
        return {
            "ok": False,
            "message": "empty audio input",
            "transcript": "",
            "response_text": "",
            "tts_audio": b"",
            "tts_mime_type": "",
        }

    run_payload = {
        "type": "assist_pipeline/run",
        "start_stage": "stt",
        "end_stage": "tts",
        "input": {
            "sample_rate": int(sample_rate if sample_rate is not None else ASSIST_INPUT_SAMPLE_RATE),
        },
    }
    if ASSIST_PIPELINE_ID:
        run_payload["pipeline"] = ASSIST_PIPELINE_ID

    run_id = 1
    transcript = ""
    response_text = ""
    tts_url = ""
    tts_mime_type = ""
    run_success = False
    last_error = None

    candidate_bases = _candidate_ha_base_urls()
    candidate_ws_urls = []
    for _base in candidate_bases:
        try:
            candidate_ws_urls.append(_build_ws_url(_base))
        except Exception:
            continue

    for idx, base in enumerate(candidate_bases):
        ws_url = _build_ws_url(base)
        try:
            if idx > 0:
                logger.warning("Assist pipeline fallback attempt via %s", ws_url)

            attempt_transcript = ""
            attempt_response_text = ""
            attempt_tts_url = ""
            attempt_tts_mime_type = ""
            stt_handler_id = None
            stt_started = False
            audio_sent = False
            saw_run_end = False

            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                auth_required = await asyncio.wait_for(ws.recv(), timeout=10)
                auth_required_msg = json.loads(auth_required)
                if auth_required_msg.get("type") != "auth_required":
                    raise RuntimeError("unexpected websocket auth challenge")

                await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                auth_result = await asyncio.wait_for(ws.recv(), timeout=10)
                auth_result_msg = json.loads(auth_result)
                if auth_result_msg.get("type") != "auth_ok":
                    raise RuntimeError(f"websocket auth failed: {auth_result_msg}")

                run_payload_with_id = dict(run_payload)
                run_payload_with_id["id"] = run_id
                await ws.send(json.dumps(run_payload_with_id))

                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=ASSIST_AUDIO_TIMEOUT_S)
                    if isinstance(raw, bytes):
                        continue

                    msg = json.loads(raw)
                    msg_type = msg.get("type")

                    if msg_type == "result" and msg.get("id") == run_id and not msg.get("success", False):
                        raise RuntimeError(f"assist pipeline run failed: {msg}")

                    if msg_type != "event" or msg.get("id") != run_id:
                        continue

                    event = msg.get("event") or {}
                    event_type = event.get("type")
                    data = event.get("data") or {}

                    if event_type == "run-start":
                        runner_data = data.get("runner_data") or {}
                        if stt_handler_id is None:
                            stt_handler_id = runner_data.get("stt_binary_handler_id")
                        tts_output = data.get("tts_output") or {}
                        if not attempt_tts_url:
                            attempt_tts_url = str(tts_output.get("url") or "").strip()
                        if not attempt_tts_mime_type:
                            attempt_tts_mime_type = str(tts_output.get("mime_type") or "").strip()

                    elif event_type == "stt-start":
                        stt_started = True

                    elif event_type == "stt-end":
                        stt_output = data.get("stt_output") or {}
                        attempt_transcript = str(stt_output.get("text") or "").strip()

                    elif event_type == "intent-end":
                        attempt_response_text = _extract_speech_text(data.get("intent_output"))

                    elif event_type == "tts-end":
                        attempt_tts_url = str(data.get("url") or attempt_tts_url or "").strip()
                        attempt_tts_mime_type = str(data.get("mime_type") or attempt_tts_mime_type or "").strip()

                    elif event_type == "error":
                        code = str(data.get("code") or "unknown")
                        message = str(data.get("message") or "")
                        raise RuntimeError(f"assist pipeline error [{code}]: {message}")

                    elif event_type == "run-end":
                        saw_run_end = True
                        break

                    if stt_started and stt_handler_id is not None and not audio_sent:
                        handler_byte = bytes([int(stt_handler_id)])
                        chunk_size = 2048
                        for i in range(0, len(audio_data), chunk_size):
                            await ws.send(handler_byte + audio_data[i : i + chunk_size])
                        await ws.send(handler_byte)
                        audio_sent = True

                if not saw_run_end:
                    raise RuntimeError("assist pipeline did not complete with run-end")

                transcript = attempt_transcript
                response_text = attempt_response_text
                tts_url = attempt_tts_url
                tts_mime_type = attempt_tts_mime_type
                if idx > 0:
                    logger.warning("Assist pipeline fallback succeeded via %s", ws_url)
                run_success = True
                break
        except Exception as exc:
            last_error = exc
            if idx == 0 and _is_name_resolution_error(exc) and len(candidate_ws_urls) > 1:
                logger.warning(
                    "Assist pipeline primary host resolve failed via %s, trying fallbacks: %s",
                    ws_url,
                    ", ".join(candidate_ws_urls[1:]),
                )
            logger.warning("Assist pipeline attempt failed via %s: %s", ws_url, exc)

    if not run_success:
        return {
            "ok": False,
            "message": f"assist pipeline request failed: {last_error or 'unknown error'}",
            "transcript": "",
            "response_text": "",
            "tts_audio": b"",
            "tts_mime_type": "",
        }

    tts_audio = b""
    tts_sample_rate = 0
    tts_sample_width = 0
    tts_channels = 0
    if tts_url:
        try:
            downloaded, downloaded_mime = _download_tts_audio(tts_url, timeout=20)
            if downloaded_mime and not tts_mime_type:
                tts_mime_type = downloaded_mime

            decoded = _decode_tts_audio_if_possible(downloaded, mime_type=tts_mime_type)
            if decoded is not None:
                tts_audio, tts_sample_rate, tts_sample_width, tts_channels = decoded
                tts_mime_type = "audio/pcm"
            else:
                tts_audio = downloaded
        except Exception as exc:
            logger.warning("Failed to fetch/decode HA TTS audio: %s", exc)

    return {
        "ok": True,
        "message": "ok",
        "transcript": transcript,
        "response_text": response_text,
        "tts_audio": tts_audio,
        "tts_mime_type": tts_mime_type,
        "tts_sample_rate": tts_sample_rate,
        "tts_sample_width": tts_sample_width,
        "tts_channels": tts_channels,
    }


def process_audio_with_assist_pipeline_sync(audio_data: bytes, sample_rate: int | None = None) -> dict:
    return asyncio.run(process_audio_with_assist_pipeline(audio_data, sample_rate=sample_rate))

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

    if intent == "task_create":
        task_result = create_ha_task_from_text(text)
        return _summarize_task_creation_with_llm(text, task_result)

    if intent in {"task_update", "task_delete"}:
        task_result = manage_ha_task_from_text(text, expected_operation=intent)
        return _summarize_task_management_with_llm(text, task_result)

    logger.info("HA-related request detected, querying Home Assistant entities")
    states = discover_entities()
    if not states:
        return _summarize_failure_with_llm(
            user_text=text,
            failure_code="ha_states_unavailable",
            technical_message="Home Assistant Error: Unable to fetch entity states",
        )

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


def _is_task_creation_request(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    return any(k in lowered for k in TASK_CREATION_KEYWORDS)


def _is_task_update_request(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    return any(k in lowered for k in TASK_UPDATE_KEYWORDS)


def _is_task_delete_request(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    return any(k in lowered for k in TASK_DELETE_KEYWORDS)


def _summarize_entities_for_task_prompt(entities: list[dict], limit: int = 80) -> list[dict]:
    out = []
    for item in entities[:limit]:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        out.append(
            {
                "entity_id": entity_id,
                "friendly_name": attrs.get("friendly_name", ""),
                "state": item.get("state", ""),
                "unit": attrs.get("unit_of_measurement", ""),
                "device_class": attrs.get("device_class", ""),
            }
        )
    return out


def _build_task_preset_with_llm(
    text: str,
    entities: list[dict],
    services: list[dict],
    retry_context: str | None = None,
) -> dict:
    entity_summaries = _summarize_entities_for_task_prompt(entities)
    service_summaries = _summarize_services_for_prompt(services)

    prompt = (
        "You create Home Assistant task preset JSON from natural language.\n"
        "Return ONLY JSON object with schema:\n"
        "{\n"
        "  \"name\": string,\n"
        "  \"enabled\": true,\n"
        "  \"trigger\": {\"type\": \"manual|time|state|event\", ...},\n"
        "  \"conditions\": [{\"type\": \"state|numeric_state|time\", ...}],\n"
        "  \"actions\": [{\"service\": \"domain.service\", \"target\": {\"entity_id\": [], \"area_id\": [], \"device_id\": []}, \"service_data\": {}, \"delay_s\": 0}]\n"
        "}\n"
        "Rules:\n"
        "1) Prefer trigger.type=time or state for scheduled tasks when user says time/condition.\n"
        "2) Choose action service from Services JSON only.\n"
        "3) entity_id must be selected from Entities JSON only.\n"
        "4) If not sure, keep target lists empty but ensure valid JSON schema.\n"
        "5) time format must be HH:MM:SS.\n"
        "6) Output only JSON, no markdown.\n"
        f"User text: {text}\n"
        f"Entities JSON: {json.dumps(entity_summaries, ensure_ascii=False)}\n"
        f"Services JSON: {json.dumps(service_summaries, ensure_ascii=False)}"
    )

    if retry_context:
        prompt += f"\nRetry context: {retry_context}\n"

    raw = llm.generate_response(prompt, temperature=0.1, max_tokens=420, retry_on_empty=True)
    candidate = _extract_json_object(raw)
    if not candidate:
        raise ValueError("LLM did not return valid preset JSON")

    return presets.normalize_and_validate_preset(candidate, is_update=False)


def _validate_task_references_or_raise(preset_payload: dict, states: list[dict], services: list[dict]) -> None:
    """Hard validation for task actions: service callable + entity exists."""
    if not isinstance(preset_payload, dict):
        raise ValueError("invalid preset payload")

    state_ids = set()
    for item in states or []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        if entity_id:
            state_ids.add(entity_id)

    service_ids = set()
    for item in services or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip()
        service = str(item.get("service") or "").strip()
        if domain and service:
            service_ids.add(f"{domain}.{service}")

    actions = preset_payload.get("actions") or []
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")

    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError(f"action[{idx}] must be an object")

        service = str(action.get("service") or "").strip()
        if not service:
            raise ValueError(f"action[{idx}].service is required")
        if service_ids and service not in service_ids:
            raise ValueError(f"action[{idx}].service not callable in HA: {service}")

        target = action.get("target") or {}
        if not isinstance(target, dict):
            continue

        entity_ids = target.get("entity_id") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not isinstance(entity_ids, list):
            entity_ids = []

        for entity_id in entity_ids:
            eid = str(entity_id).strip()
            if not eid:
                continue
            if eid not in state_ids:
                raise ValueError(f"action[{idx}].target.entity_id not found in HA: {eid}")


def _judge_hard_validation_failure_with_llm(
    user_text: str,
    operation: str,
    candidate_payload: dict,
    error_message: str,
) -> dict:
    prompt = (
        "You analyze Home Assistant task-validation failure causes.\n"
        "Return ONLY JSON with schema:\n"
        "{\"classification\":\"llm_generation_error|user_intent_invalid|ambiguous\",\"should_retry\":bool,\"message\":string}\n"
        "Rules:\n"
        "1) llm_generation_error: user intent is reasonable but generated references are invalid. should_retry=true.\n"
        "2) user_intent_invalid: user asked for unavailable/nonexistent things. should_retry=false.\n"
        "3) ambiguous: not enough clarity. should_retry=false and ask user to clarify.\n"
        f"Operation: {operation}\n"
        f"User text: {user_text}\n"
        f"Generated payload JSON: {json.dumps(candidate_payload, ensure_ascii=False)}\n"
        f"Validation error: {error_message}"
    )
    raw = llm.generate_response(prompt, temperature=0.1, max_tokens=180, retry_on_empty=True)
    parsed = _extract_json_object(raw)
    if not isinstance(parsed, dict):
        return {
            "classification": "ambiguous",
            "should_retry": False,
            "message": "任务校验失败，请补充更明确的设备名或服务名。",
        }

    classification = str(parsed.get("classification") or "ambiguous").strip().lower()
    should_retry = bool(parsed.get("should_retry", False))
    message = str(parsed.get("message") or "").strip()

    if classification not in {"llm_generation_error", "user_intent_invalid", "ambiguous"}:
        classification = "ambiguous"
    return {
        "classification": classification,
        "should_retry": should_retry,
        "message": message,
    }


def _export_compiled_ha_yaml(compiled: dict, preset_id: str) -> dict:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    output_dir = os.path.join(base_dir, "data", "ha_exports")
    os.makedirs(output_dir, exist_ok=True)

    script_path = os.path.join(output_dir, f"script_{preset_id}.yaml")
    with open(script_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(compiled.get("script") or {}, f, allow_unicode=True, sort_keys=False)

    automation_path = None
    if compiled.get("automation"):
        automation_path = os.path.join(output_dir, f"automation_{preset_id}.yaml")
        with open(automation_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(compiled.get("automation") or {}, f, allow_unicode=True, sort_keys=False)

    return {
        "script_yaml_path": script_path,
        "automation_yaml_path": automation_path,
    }


def _auto_apply_ha_task_changes() -> dict:
    """Reload HA script/automation so task changes take effect automatically."""
    steps = []
    for service in ["script.reload", "automation.reload"]:
        ok, message = _call_ha_service(service, {})
        steps.append({"service": service, "ok": bool(ok), "message": str(message or "")})

    ok_all = all(step.get("ok") for step in steps)
    return {
        "ok": ok_all,
        "steps": steps,
    }


def create_ha_task_from_text(text: str) -> dict:
    user_text = str(text or "").strip()
    if not user_text:
        return {"ok": False, "message": "text is required"}

    states = discover_entities()
    if not states:
        return {
            "ok": False,
            "message": _summarize_failure_with_llm(
                user_text=user_text,
                failure_code="task_create_states_unavailable",
                technical_message="Home Assistant Error: Unable to fetch entity states",
            ),
        }

    services = discover_services()
    if not services:
        return {
            "ok": False,
            "message": _summarize_failure_with_llm(
                user_text=user_text,
                failure_code="task_create_services_unavailable",
                technical_message="Home Assistant Error: Unable to fetch service list",
            ),
        }

    retry_context = None
    planned_candidate: dict = {}
    last_error = None
    for attempt in range(2):
        try:
            planned = _build_task_preset_with_llm(user_text, states, services, retry_context=retry_context)
            planned_candidate = planned if isinstance(planned, dict) else {}
            _validate_task_references_or_raise(planned_candidate, states, services)
            created = _preset_store.create_preset(planned_candidate)
            compiled = presets.compile_preset_to_ha(created)
            exported = _export_compiled_ha_yaml(compiled, created.get("id") or "unknown")
            applied = _auto_apply_ha_task_changes()
            return {
                "ok": True,
                "preset": created,
                "compiled": compiled,
                "exported": exported,
                "applied": applied,
            }
        except Exception as exc:
            last_error = str(exc)
            logger.error("Create HA task from text failed (attempt %s): %s", attempt + 1, exc)

            judge = _judge_hard_validation_failure_with_llm(
                user_text=user_text,
                operation="create",
                candidate_payload=planned_candidate,
                error_message=last_error,
            )

            if attempt == 0 and judge.get("classification") == "llm_generation_error" and judge.get("should_retry"):
                retry_context = (
                    "Previous generated task failed hard validation. "
                    f"Error: {last_error}. "
                    "Regenerate with strict valid service names and existing entity_ids only."
                )
                continue

            judge_msg = str(judge.get("message") or "").strip()
            if not judge_msg:
                judge_msg = _summarize_failure_with_llm(
                    user_text=user_text,
                    failure_code="task_create_validation_failed",
                    technical_message=f"创建自动化任务失败: {last_error}",
                )
            return {"ok": False, "message": judge_msg}

    return {
        "ok": False,
        "message": _summarize_failure_with_llm(
            user_text=user_text,
            failure_code="task_create_unknown",
            technical_message=f"创建自动化任务失败: {last_error or 'unknown error'}",
        ),
    }


def _list_presets_for_prompt(limit: int = 80) -> list[dict]:
    items = _preset_store.list_presets()
    summaries = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        summaries.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "enabled": item.get("enabled", True),
                "trigger": item.get("trigger") or {},
                "conditions": item.get("conditions") or [],
                "actions": item.get("actions") or [],
            }
        )
    return summaries


def _plan_task_management_with_llm(text: str, preset_summaries: list[dict], retry_context: str | None = None) -> dict:
    prompt = (
        "You manage Home Assistant automation presets by natural language.\n"
        "Return ONLY JSON with schema:\n"
        "{\"operation\":\"update|delete|unknown\",\"preset_id\":\"\",\"name_hint\":\"\",\"changes\":{},\"reason\":\"\"}\n"
        "Rules:\n"
        "1) operation=delete only if user explicitly asks deletion/cancel/removal.\n"
        "2) operation=update for edit/modify/change requests.\n"
        "3) preset_id must be from Presets JSON ids if identifiable.\n"
        "4) If id not identifiable, provide name_hint from user text.\n"
        "5) changes must contain only fields that need update, using preset schema fields.\n"
        "6) Output JSON only.\n"
        f"User text: {text}\n"
        f"Presets JSON: {json.dumps(preset_summaries, ensure_ascii=False)}"
    )
    if retry_context:
        prompt += f"\nRetry context: {retry_context}\n"
    raw = llm.generate_response(prompt, temperature=0.1, max_tokens=320, retry_on_empty=True)
    return _extract_json_object(raw)


def _resolve_preset_by_plan(plan: dict, presets_list: list[dict]) -> dict | None:
    if not presets_list:
        return None

    preset_id = str(plan.get("preset_id") or "").strip()
    if preset_id:
        for item in presets_list:
            if str(item.get("id")) == preset_id:
                return item

    name_hint = str(plan.get("name_hint") or "").strip().lower()
    if name_hint:
        for item in presets_list:
            name = str(item.get("name") or "").strip().lower()
            if name and (name_hint in name or name in name_hint):
                return item

    return None


def manage_ha_task_from_text(text: str, expected_operation: str | None = None) -> dict:
    user_text = str(text or "").strip()
    if not user_text:
        return {
            "ok": False,
            "message": _summarize_failure_with_llm(
                user_text=text,
                failure_code="task_manage_text_required",
                technical_message="text is required",
            ),
            "operation": "unknown",
        }

    presets_list = _preset_store.list_presets()
    if not presets_list:
        return {
            "ok": False,
            "message": _summarize_failure_with_llm(
                user_text=user_text,
                failure_code="task_manage_no_presets",
                technical_message="当前没有可修改或删除的任务",
            ),
            "operation": "unknown",
        }

    retry_context = None
    for attempt in range(2):
        plan = _plan_task_management_with_llm(user_text, _list_presets_for_prompt(), retry_context=retry_context)
        operation = str(plan.get("operation") or "unknown").strip().lower()
        if expected_operation in {"task_update", "task_delete"}:
            forced = "update" if expected_operation == "task_update" else "delete"
            if operation not in {"update", "delete"}:
                operation = forced

        target = _resolve_preset_by_plan(plan, presets_list)
        if not target:
            return {
                "ok": False,
                "message": _summarize_failure_with_llm(
                    user_text=user_text,
                    failure_code="task_manage_target_not_found",
                    technical_message="无法定位要修改或删除的任务，请说出任务名称",
                ),
                "operation": operation,
                "plan": plan,
            }

        target_id = str(target.get("id") or "").strip()
        if not target_id:
            return {
                "ok": False,
                "message": _summarize_failure_with_llm(
                    user_text=user_text,
                    failure_code="task_manage_target_missing_id",
                    technical_message="目标任务缺少ID",
                ),
                "operation": operation,
                "plan": plan,
            }

        if operation == "delete":
            deleted = _preset_store.delete_preset(target_id)
            if not deleted:
                return {
                    "ok": False,
                    "message": _summarize_failure_with_llm(
                        user_text=user_text,
                        failure_code="task_delete_failed",
                        technical_message="删除任务失败",
                    ),
                    "operation": operation,
                    "target": target,
                }
            applied = _auto_apply_ha_task_changes()
            return {
                "ok": True,
                "operation": "delete",
                "target": {"id": target_id, "name": target.get("name")},
                "applied": applied,
            }

        if operation != "update":
            return {
                "ok": False,
                "message": _summarize_failure_with_llm(
                    user_text=user_text,
                    failure_code="task_manage_intent_not_found",
                    technical_message="没有识别到更新或删除意图",
                ),
                "operation": operation,
                "plan": plan,
            }

        changes = plan.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return {
                "ok": False,
                "message": _summarize_failure_with_llm(
                    user_text=user_text,
                    failure_code="task_update_no_changes",
                    technical_message="没有识别到可更新的字段",
                ),
                "operation": "update",
                "target": target,
            }

        try:
            states = discover_entities()
            if not states:
                return {
                    "ok": False,
                    "message": _summarize_failure_with_llm(
                        user_text=user_text,
                        failure_code="task_update_states_unavailable",
                        technical_message="Home Assistant Error: Unable to fetch entity states",
                    ),
                    "operation": "update",
                }

            services = discover_services()
            if not services:
                return {
                    "ok": False,
                    "message": _summarize_failure_with_llm(
                        user_text=user_text,
                        failure_code="task_update_services_unavailable",
                        technical_message="Home Assistant Error: Unable to fetch service list",
                    ),
                    "operation": "update",
                }

            merged_candidate = dict(target)
            merged_candidate.update(changes)
            merged_candidate = presets.normalize_and_validate_preset(merged_candidate, is_update=True)
            _validate_task_references_or_raise(merged_candidate, states, services)

            updated = _preset_store.update_preset(target_id, changes)
            if not updated:
                return {
                    "ok": False,
                    "message": _summarize_failure_with_llm(
                        user_text=user_text,
                        failure_code="task_update_failed",
                        technical_message="更新任务失败",
                    ),
                    "operation": "update",
                    "target": target,
                }

            compiled = presets.compile_preset_to_ha(updated)
            exported = _export_compiled_ha_yaml(compiled, updated.get("id") or target_id)
            applied = _auto_apply_ha_task_changes()
            return {
                "ok": True,
                "operation": "update",
                "preset": updated,
                "compiled": compiled,
                "exported": exported,
                "applied": applied,
                "changes": changes,
            }
        except Exception as exc:
            logger.error("Update HA task from text failed (attempt %s): %s", attempt + 1, exc)

            judge = _judge_hard_validation_failure_with_llm(
                user_text=user_text,
                operation="update",
                candidate_payload={"target": target, "changes": changes},
                error_message=str(exc),
            )

            if attempt == 0 and judge.get("classification") == "llm_generation_error" and judge.get("should_retry"):
                retry_context = (
                    "Previous update plan failed hard validation. "
                    f"Error: {str(exc)}. "
                    "Regenerate changes using only callable services and existing entity_ids."
                )
                continue

            judge_msg = str(judge.get("message") or "").strip()
            if not judge_msg:
                judge_msg = _summarize_failure_with_llm(
                    user_text=user_text,
                    failure_code="task_update_validation_failed",
                    technical_message=f"更新任务失败: {str(exc)}",
                )
            return {
                "ok": False,
                "message": judge_msg,
                "operation": "update",
                "target": target,
            }

    return {
        "ok": False,
        "message": _summarize_failure_with_llm(
            user_text=user_text,
            failure_code="task_update_retry_exceeded",
            technical_message="更新任务失败: exceeded retry",
        ),
        "operation": "update",
    }


def _summarize_task_management_with_llm(user_text: str, result: dict) -> str:
    lang = _detect_response_language(user_text)
    fallback_fail = "任务操作失败" if _is_chinese_output(lang) else "Task operation failed."

    if not isinstance(result, dict):
        return fallback_fail

    if not result.get("ok"):
        msg = str(result.get("message") or "").strip() or fallback_fail
        return _rewrite_result_with_llm(
            user_text=user_text,
            result_payload={"ok": False, "operation": result.get("operation"), "message": msg},
            task_name="task_manage_failure_naturalization",
            instruction="Explain task update/delete failure clearly and briefly.",
            fallback_text=msg,
            temperature=0.1,
            max_tokens=120,
        )

    op = str(result.get("operation") or "").lower()
    if op == "delete":
        target = result.get("target") or {}
        applied = result.get("applied") or {}
        auto_applied = bool(applied.get("ok", False))
        fallback_ok = (
            f"已删除任务{target.get('name') or ''}，任务ID为{target.get('id') or 'unknown'}，"
            f"{'并已自动在HA中生效' if auto_applied else '但HA自动生效失败，请在HA手动重载'}。"
            if _is_chinese_output(lang)
            else (
                f"Deleted task {target.get('name') or ''} with ID {target.get('id') or 'unknown'}, "
                f"and {'auto-applied in Home Assistant' if auto_applied else 'auto-apply failed; please reload in Home Assistant manually'}."
            )
        )
        return _rewrite_result_with_llm(
            user_text=user_text,
            result_payload=result,
            task_name="task_delete_result_naturalization",
            instruction="Confirm deletion with task name and ID.",
            fallback_text=fallback_ok,
            temperature=0.1,
            max_tokens=120,
        )

    preset = result.get("preset") or {}
    applied = result.get("applied") or {}
    auto_applied = bool(applied.get("ok", False))
    fallback_ok = (
        f"已更新任务{preset.get('name') or ''}，任务ID为{preset.get('id') or 'unknown'}，并重新导出了YAML，"
        f"{'且已自动在HA中生效' if auto_applied else '但HA自动生效失败，请在HA手动重载'}。"
        if _is_chinese_output(lang)
        else (
            f"Updated task {preset.get('name') or ''} with ID {preset.get('id') or 'unknown'}, re-exported YAML, "
            f"and {'auto-applied in Home Assistant' if auto_applied else 'auto-apply failed; please reload in Home Assistant manually'}."
        )
    )
    return _rewrite_result_with_llm(
        user_text=user_text,
        result_payload=result,
        task_name="task_update_result_naturalization",
        instruction="Confirm update with task name and ID, and mention YAML re-export.",
        fallback_text=fallback_ok,
        temperature=0.1,
        max_tokens=160,
    )


def _summarize_task_creation_with_llm(user_text: str, task_result: dict) -> str:
    lang = _detect_response_language(user_text)
    fallback_fail = "创建自动化任务失败" if _is_chinese_output(lang) else "Failed to create Home Assistant automation task."

    if not isinstance(task_result, dict):
        return fallback_fail

    if not task_result.get("ok"):
        msg = str(task_result.get("message") or "").strip()
        if not msg:
            return fallback_fail
        rewritten_fail = _rewrite_result_with_llm(
            user_text=user_text,
            result_payload={"ok": False, "message": msg},
            task_name="task_creation_failure_naturalization",
            instruction="Rewrite the failure reason clearly for end users.",
            fallback_text=msg or fallback_fail,
            temperature=0.1,
            max_tokens=120,
        )
        return rewritten_fail or msg or fallback_fail

    created = task_result.get("preset") or {}
    compiled = task_result.get("compiled") or {}
    exported = task_result.get("exported") or {}
    applied = task_result.get("applied") or {}
    has_automation = bool(compiled.get("automation"))
    auto_applied = bool(applied.get("ok", False))

    summary_payload = {
        "preset_name": created.get("name") or "",
        "preset_id": created.get("id") or "",
        "trigger_type": (created.get("trigger") or {}).get("type"),
        "has_automation": has_automation,
        "script_yaml_path": exported.get("script_yaml_path"),
        "automation_yaml_path": exported.get("automation_yaml_path"),
        "auto_applied": auto_applied,
        "apply_result": applied,
    }

    fallback_ok = (
        f"已创建计划任务{summary_payload.get('preset_name') or '未命名任务'}，"
        f"任务ID为{summary_payload.get('preset_id') or 'unknown'}，"
        f"{'并已自动在HA中生效' if auto_applied else '但HA自动生效失败，请将导出的YAML合并后在HA手动重载automation与script'}。"
        if _is_chinese_output(lang)
        else (
            f"Created automation task {summary_payload.get('preset_name') or 'unnamed task'} "
            f"with ID {summary_payload.get('preset_id') or 'unknown'}, "
            f"and {'auto-applied in Home Assistant' if auto_applied else 'auto-apply failed; merge exported YAML and reload automation/script manually'}."
        )
    )

    rewritten_ok = _rewrite_result_with_llm(
        user_text=user_text,
        result_payload=summary_payload,
        task_name="task_creation_result_naturalization",
        instruction=(
            "Mention task name and task ID. "
            "If has_automation is true, say it is scheduled/automated and mention YAML export files. "
            "If has_automation is false, say it is manual trigger and suggest adding time/condition. "
            "Also mention whether auto-apply succeeded based on auto_applied/apply_result."
        ),
        fallback_text=fallback_ok,
        temperature=0.1,
        max_tokens=180,
    )
    return rewritten_ok or fallback_ok


def _normalize_match_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _input_select_supports_option(entity: dict, option: str) -> bool:
    attrs = entity.get("attributes") or {}
    if not isinstance(attrs, dict):
        return False
    options = attrs.get("options")
    if not isinstance(options, list):
        return False
    normalized = {_normalize_match_text(str(v)) for v in options if str(v).strip()}
    return _normalize_match_text(str(option or "")) in normalized


def _plan_mode_or_scene_action(
    text: str,
    states: list[dict],
    service_map: dict,
) -> dict:
    lowered = _normalize_match_text(text)
    if not lowered:
        return {"actions": []}

    has_mode_scene_signal = any(k in lowered for k in ["mode", "scene", "模式", "场景"])
    has_switch_signal = any(k in lowered for k in ["切换", "switch", "set", "设置", "设为", "改为"])
    if not (has_mode_scene_signal or has_switch_signal):
        return {"actions": []}

    scored_actions = []

    # 1) Dynamic input_select option matching from live HA options.
    if not service_map or "input_select.select_option" in service_map:
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            if not entity_id.startswith("input_select."):
                continue

            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            options = attrs.get("options")
            if not isinstance(options, list):
                continue

            friendly = _normalize_match_text(str(attrs.get("friendly_name") or ""))
            object_id = _normalize_match_text(entity_id.split(".", 1)[1] if "." in entity_id else entity_id)

            for option in options:
                option_raw = str(option).strip()
                option_norm = _normalize_match_text(option_raw)
                if not option_norm:
                    continue
                if option_norm not in lowered:
                    continue

                score = 10
                if "mode" in object_id or "模式" in object_id or "scene" in object_id or "场景" in object_id:
                    score += 3
                if friendly and ("mode" in friendly or "模式" in friendly or "scene" in friendly or "场景" in friendly):
                    score += 3

                scored_actions.append(
                    (
                        score,
                        {
                            "service": "input_select.select_option",
                            "target": {"entity_id": [entity_id], "area_id": [], "device_id": []},
                            "service_data": {"option": option_raw},
                        },
                    )
                )

    # 2) Dynamic scene matching from scene entities.
    if not service_map or "scene.turn_on" in service_map:
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            if not entity_id.startswith("scene."):
                continue

            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            friendly = _normalize_match_text(str(attrs.get("friendly_name") or ""))
            object_id = _normalize_match_text(entity_id.split(".", 1)[1] if "." in entity_id else entity_id)

            matched = False
            if object_id and object_id in lowered:
                matched = True
            if friendly and friendly in lowered:
                matched = True
            if not matched:
                continue

            score = 8
            if has_mode_scene_signal:
                score += 2
            scored_actions.append(
                (
                    score,
                    {
                        "service": "scene.turn_on",
                        "target": {"entity_id": [entity_id], "area_id": [], "device_id": []},
                        "service_data": {},
                    },
                )
            )

    if not scored_actions:
        return {"actions": []}

    scored_actions.sort(key=lambda x: x[0], reverse=True)
    return {"actions": [scored_actions[0][1]]}


def _read_entity_state(entity_id: str) -> str | None:
    entity = str(entity_id or "").strip()
    if not entity:
        return None
    try:
        response = _ha_request("get", f"api/states/{entity}", timeout=8)
        if response.status_code != 200:
            return None
        payload = response.json() or {}
        if not isinstance(payload, dict):
            return None
        state = payload.get("state")
        return str(state) if state is not None else None
    except Exception:
        return None


def _handle_control_intent(text: str, route: dict, states: list[dict]) -> str:
    services = discover_services()
    service_map = _build_service_map(services)

    intent_slots = _extract_intent_slots(text)
    logger.info("Intent slots (control): %s", json.dumps(intent_slots, ensure_ascii=False))

    plan = {"actions": []}

    # Special-case mode/scene switching using dynamic HA entities/options.
    plan = _plan_mode_or_scene_action(text, states, service_map)

    slot_targets = _match_entities_by_slots(states, intent_slots.get("slots") or {}, limit=8)
    if slot_targets and not plan.get("actions"):
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
        return _summarize_failure_with_llm(
            user_text=text,
            failure_code="control_no_valid_action",
            technical_message="I couldn't find a valid Home Assistant control action",
        )

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

            # Post-check for deterministic set operations (e.g., input_select.select_option).
            if ok and service == "input_select.select_option":
                expected_option = str(service_data.get("option") or "").strip()
                if expected_option:
                    mismatch = []
                    for entity_id in valid_entities:
                        current_state = _read_entity_state(entity_id)
                        if current_state is None or str(current_state).lower() != expected_option.lower():
                            mismatch.append(entity_id)
                    if mismatch:
                        ok = False
                        response_msg = (
                            f"Post-check failed: expected option '{expected_option}' not reached for {', '.join(mismatch)}"
                        )

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
        return _summarize_failure_with_llm(
            user_text=text,
            failure_code="control_no_available_target",
            technical_message="No available target entities found in Home Assistant",
        )

    return _summarize_control_result_with_llm(text, execution_results)


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
        return _summarize_failure_with_llm(
            user_text=text,
            failure_code="query_no_results",
            technical_message=UNKNOWN_FROM_HA_REPLY,
            fallback_text=UNKNOWN_FROM_HA_REPLY,
        )

    lang = _detect_response_language(text)
    facts = _build_query_facts(query_results, lang)
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


def _display_name_for_naturalization(entity_id: object | None, friendly_name: object | None, lang: str) -> str:
    friendly = str(friendly_name or "").strip()
    entity = str(entity_id or "").strip()
    domain = entity.split(".", 1)[0] if entity else ""
    if not friendly:
        if _is_chinese_output(lang):
            return DOMAIN_LOCALIZATION_ZH.get(domain, "")
        return DOMAIN_LOCALIZATION_EN.get(domain, "")

    object_id = entity.split(".", 1)[1] if "." in entity else entity
    object_tokens = set(re.findall(r"[a-z0-9]+", object_id.lower()))
    friendly_tokens = set(re.findall(r"[a-z0-9]+", friendly.lower()))

    looks_ascii = bool(re.fullmatch(r"[A-Za-z0-9 _.-]+", friendly))
    token_overlap = bool(object_tokens and friendly_tokens and object_tokens.issubset(friendly_tokens))
    generic_tokens = {"home", "default", "forecast", "local", "assistant"}
    generic_hit = bool(friendly_tokens & generic_tokens)
    looks_internal = looks_ascii and (token_overlap or generic_hit)

    # Hide likely internal source names like "Forecast Home" for both zh/en output.
    if looks_internal:
        if _is_chinese_output(lang):
            return DOMAIN_LOCALIZATION_ZH.get(domain, "")
        return DOMAIN_LOCALIZATION_EN.get(domain, "")

    return friendly


def _build_query_facts(query_results: list[dict], lang: str) -> str:
    if not query_results:
        return ""

    compact = []
    for item in query_results:
        entity_id = item.get("entity_id")
        display_name = _display_name_for_naturalization(
            entity_id=entity_id,
            friendly_name=item.get("friendly_name"),
            lang=lang,
        )
        compact.append(
            {
                "friendly_name": display_name,
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
    fallback_text = UNKNOWN_FROM_HA_REPLY if _is_chinese_output(lang) else "I don't know based on Home Assistant data."

    logger.info("Naturalization input facts: %s", facts_json)
    rewritten = _rewrite_result_with_llm(
        user_text=user_text,
        result_payload={
            "facts": json.loads(facts_json),
            "raw_query_results": query_results,
        },
        task_name="query_result_naturalization",
        instruction=(
            "Include all key facts relevant to the user question. "
            "For each selected entity, prioritize state and scalar numeric/boolean attributes. "
            "Convert raw keys into user-friendly wording without changing values. "
            "Do not mention internal source names (entity_id/friendly_name) unless the user explicitly asks for which device/source. "
            "If facts are insufficient, return fallback text exactly."
        ),
        fallback_text=fallback_text,
        temperature=0.15,
        max_tokens=260,
    )
    logger.info("Naturalization output summary: %s", rewritten)
    if rewritten and not _looks_like_unknown_answer(rewritten):
        return rewritten

    # Retry with a shorter, simpler instruction in case the model returned empty output.
    target_language = "Chinese" if _is_chinese_output(lang) else "English"
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
        domain = entity_id.split(".", 1)[0] if entity_id else ""
        if friendly:
            object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
            object_tokens = set(re.findall(r"[a-z0-9]+", object_id.lower()))
            friendly_tokens = set(re.findall(r"[a-z0-9]+", friendly.lower()))
            looks_ascii = bool(re.fullmatch(r"[A-Za-z0-9 _.-]+", friendly))
            token_overlap = bool(object_tokens and friendly_tokens and object_tokens.issubset(friendly_tokens))
            generic_hit = bool(friendly_tokens & {"home", "default", "forecast", "local", "assistant"})
            looks_internal = looks_ascii and (token_overlap or generic_hit)

            if looks_internal:
                if _is_chinese_output(lang):
                    return DOMAIN_LOCALIZATION_ZH.get(domain, friendly)
                return DOMAIN_LOCALIZATION_EN.get(domain, friendly)
            return friendly
        if entity_id and _is_chinese_output(lang):
            return DOMAIN_LOCALIZATION_ZH.get(domain, entity_id)
        if entity_id:
            return DOMAIN_LOCALIZATION_EN.get(domain, entity_id)
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


def _rewrite_result_with_llm(
    user_text: str,
    result_payload,
    task_name: str,
    instruction: str,
    fallback_text: str,
    temperature: float = 0.1,
    max_tokens: int = 200,
) -> str:
    """Unified multilingual post-processor for query/control/task results."""
    lang = _detect_response_language(user_text)
    target_language = "Chinese" if _is_chinese_output(lang) else "English"

    try:
        payload_json = json.dumps(result_payload, ensure_ascii=False)
    except Exception:
        payload_json = json.dumps({"value": str(result_payload)}, ensure_ascii=False)

    prompt = (
        "You are a concise voice assistant response writer.\n"
        f"Task: {task_name}\n"
        f"Respond in natural spoken {target_language}.\n"
        "Use ONLY the result JSON facts. Do not add new facts.\n"
        "Output plain text only (1-2 short sentences).\n"
        f"Instruction: {instruction}\n"
        f"Fallback text: {fallback_text}\n"
        f"User request: {user_text}\n"
        f"Result JSON: {payload_json}"
    )
    rewritten = (llm.generate_response(prompt, temperature=temperature, max_tokens=max_tokens, retry_on_empty=True) or "").strip()
    return rewritten or fallback_text


def _summarize_failure_with_llm(
    user_text: str,
    failure_code: str,
    technical_message: str,
    fallback_text: str | None = None,
) -> str:
    fallback = str(fallback_text or technical_message or "操作失败").strip()
    return _rewrite_result_with_llm(
        user_text=user_text,
        result_payload={
            "failure_code": failure_code,
            "technical_message": technical_message,
        },
        task_name="failure_naturalization",
        instruction=(
            "Rewrite the failure in clear, user-friendly language. "
            "Keep meaning consistent with technical_message."
        ),
        fallback_text=fallback,
        temperature=0.1,
        max_tokens=140,
    )


def _summarize_control_result_with_llm(user_text: str, execution_results: list[dict]) -> str:
    success_count = sum(1 for result in execution_results if result.get("ok"))
    skipped_count = sum(len(result.get("skipped", [])) for result in execution_results)

    lang = _detect_response_language(user_text)
    if _is_chinese_output(lang):
        if success_count > 0 and skipped_count == 0:
            fallback = "已完成控制操作。"
        elif success_count > 0:
            fallback = f"已完成可执行的控制操作，跳过了{skipped_count}个不可用或缺失实体。"
        else:
            fallback = "找到了目标实体，但执行控制操作失败。"
    else:
        if success_count > 0 and skipped_count == 0:
            fallback = "Done."
        elif success_count > 0:
            fallback = f"Done for available entities. Skipped {skipped_count} unavailable or missing entities."
        else:
            fallback = "I found target entities, but could not execute actions successfully."

    return _rewrite_result_with_llm(
        user_text=user_text,
        result_payload={
            "success_count": success_count,
            "skipped_count": skipped_count,
            "execution_results": execution_results,
        },
        task_name="control_result_naturalization",
        instruction=(
            "Summarize execution result clearly. Mention whether operation succeeded fully, partially, or failed. "
            "If partial, mention skipped entities count."
        ),
        fallback_text=fallback,
        temperature=0.1,
        max_tokens=150,
    )


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
        "{\"ha_related\": bool, \"intent\": \"none|control|query|task_create|task_update|task_delete\", \"answer\": string, "
        "\"actions\": [{\"service\": \"domain.service\", \"target\": {\"entity_id\": [\"domain.name\"], \"area_id\": [], \"device_id\": []}, \"service_data\": {}}], "
        "\"query_entities\": [\"domain.name\"]}\n"
        "Rules:\n"
        "1) If user asks general knowledge/chitchat, set ha_related=false, intent=none, and provide answer.\n"
        "2) If user wants to control HA entities, set ha_related=true, intent=control.\n"
        "3) If user asks HA state from local entities, set ha_related=true, intent=query.\n"
        "4) If user asks to create scheduled automation/task/plan in Home Assistant, set ha_related=true, intent=task_create.\n"
        "5) If user asks to modify an existing automation task, set ha_related=true, intent=task_update.\n"
        "6) If user asks to delete/cancel/remove an existing automation task, set ha_related=true, intent=task_delete.\n"
        "7) If unknown but likely non-HA, set ha_related=false.\n"
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
    if routed_intent in {"control", "query", "task_create", "task_update", "task_delete"} and (
        routed_ha_related or _has_home_context_signal(text)
    ):
        return {"matched": True, "intent": routed_intent, "reason": "router"}

    # Safety fallback for task-management phrases when router is uncertain.
    if _is_task_creation_request(text):
        return {"matched": True, "intent": "task_create", "reason": "task_keyword_fallback"}
    if _is_task_update_request(text):
        return {"matched": True, "intent": "task_update", "reason": "task_keyword_fallback"}
    if _is_task_delete_request(text):
        return {"matched": True, "intent": "task_delete", "reason": "task_keyword_fallback"}

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