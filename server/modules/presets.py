"""Preset management for device-agnostic automation development.

This module provides:
- JSON file persistence for user-defined presets
- Lightweight schema validation
- Compilation to Home Assistant script/automation dictionaries
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from config_loader import get_config


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class PresetValidationError(ValueError):
    """Raised when preset payload is invalid."""


class PresetStore:
    """JSON-backed preset store with minimal validation."""

    def __init__(self, storage_file: str):
        self.storage_file = storage_file
        self._lock = threading.Lock()
        self._ensure_storage_file()

    def _ensure_storage_file(self) -> None:
        os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
        if not os.path.exists(self.storage_file):
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump({"presets": []}, f, ensure_ascii=False, indent=2)

    def _read_all(self) -> dict:
        with open(self.storage_file, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if "presets" not in data or not isinstance(data["presets"], list):
            data = {"presets": []}
        return data

    def _write_all(self, data: dict) -> None:
        with open(self.storage_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def list_presets(self) -> list[dict]:
        with self._lock:
            data = self._read_all()
            return data["presets"]

    def get_preset(self, preset_id: str) -> dict | None:
        with self._lock:
            data = self._read_all()
            for item in data["presets"]:
                if item.get("id") == preset_id:
                    return item
            return None

    def create_preset(self, payload: dict) -> dict:
        with self._lock:
            data = self._read_all()
            preset = normalize_and_validate_preset(payload, is_update=False)
            preset["id"] = str(uuid.uuid4())
            preset["created_at"] = _utc_now_iso()
            preset["updated_at"] = preset["created_at"]
            data["presets"].append(preset)
            self._write_all(data)
            return preset

    def update_preset(self, preset_id: str, payload: dict) -> dict | None:
        with self._lock:
            data = self._read_all()
            for idx, item in enumerate(data["presets"]):
                if item.get("id") != preset_id:
                    continue

                merged = deepcopy(item)
                merged.update(payload or {})
                merged = normalize_and_validate_preset(merged, is_update=True)
                merged["id"] = preset_id
                merged["created_at"] = item.get("created_at") or _utc_now_iso()
                merged["updated_at"] = _utc_now_iso()
                data["presets"][idx] = merged
                self._write_all(data)
                return merged
            return None

    def delete_preset(self, preset_id: str) -> bool:
        with self._lock:
            data = self._read_all()
            before = len(data["presets"])
            data["presets"] = [p for p in data["presets"] if p.get("id") != preset_id]
            if len(data["presets"]) == before:
                return False
            self._write_all(data)
            return True


def normalize_and_validate_preset(payload: dict, is_update: bool) -> dict:
    if not isinstance(payload, dict):
        raise PresetValidationError("preset payload must be an object")

    preset = deepcopy(payload)

    name = str(preset.get("name", "")).strip()
    if not name:
        raise PresetValidationError("name is required")
    preset["name"] = name

    preset["enabled"] = bool(preset.get("enabled", True))
    preset["priority"] = int(preset.get("priority", 100))
    preset["cooldown_s"] = int(preset.get("cooldown_s", 0))
    preset["tags"] = [str(x).strip() for x in _ensure_list(preset.get("tags")) if str(x).strip()]

    trigger = preset.get("trigger") or {"type": "manual"}
    if not isinstance(trigger, dict):
        raise PresetValidationError("trigger must be an object")
    trigger_type = str(trigger.get("type", "manual")).strip().lower()
    if trigger_type not in {"manual", "time", "state", "event"}:
        raise PresetValidationError("trigger.type must be one of manual|time|state|event")
    trigger["type"] = trigger_type
    preset["trigger"] = trigger

    conditions = preset.get("conditions") or []
    if not isinstance(conditions, list):
        raise PresetValidationError("conditions must be a list")
    normalized_conditions = []
    for cond in conditions:
        if not isinstance(cond, dict):
            raise PresetValidationError("each condition must be an object")
        cond_type = str(cond.get("type", "state")).strip().lower()
        if cond_type not in {"state", "numeric_state", "time"}:
            raise PresetValidationError("condition.type must be state|numeric_state|time")
        cond["type"] = cond_type
        normalized_conditions.append(cond)
    preset["conditions"] = normalized_conditions

    actions = preset.get("actions") or []
    if not isinstance(actions, list) or not actions:
        raise PresetValidationError("actions must be a non-empty list")

    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict):
            raise PresetValidationError("each action must be an object")

        service = str(action.get("service", "")).strip()
        if not service or "." not in service:
            raise PresetValidationError("action.service must be in format domain.service")

        target = action.get("target") or {}
        if not isinstance(target, dict):
            raise PresetValidationError("action.target must be an object")

        target_entity = _ensure_list(target.get("entity_id"))
        target_area = _ensure_list(target.get("area_id"))
        target_device = _ensure_list(target.get("device_id"))

        clean_target = {
            "entity_id": [str(x).strip() for x in target_entity if str(x).strip()],
            "area_id": [str(x).strip() for x in target_area if str(x).strip()],
            "device_id": [str(x).strip() for x in target_device if str(x).strip()],
        }

        service_data = action.get("service_data") or {}
        if not isinstance(service_data, dict):
            raise PresetValidationError("action.service_data must be an object")

        delay_s = float(action.get("delay_s", 0))
        if delay_s < 0:
            raise PresetValidationError("action.delay_s must be >= 0")

        normalized_actions.append(
            {
                "service": service,
                "target": clean_target,
                "service_data": service_data,
                "delay_s": delay_s,
            }
        )

    preset["actions"] = normalized_actions

    if is_update:
        preset.pop("id", None)

    return preset


def compile_preset_to_ha(preset: dict) -> dict:
    """Compile a preset into HA script + automation dictionaries.

    This does not push into Home Assistant. It returns dictionaries that can be
    written into HA YAML or sent through a future bridge layer.
    """
    preset_id = preset.get("id", "preview")
    script_key = f"humblevoice_preset_{str(preset_id).replace('-', '_')}"

    sequence = []
    for action in preset.get("actions", []):
        delay_s = float(action.get("delay_s", 0) or 0)
        if delay_s > 0:
            sequence.append({"delay": f"00:00:{delay_s:04.1f}"})

        step = {
            "service": action["service"],
            "target": {
                "entity_id": action.get("target", {}).get("entity_id", []),
                "area_id": action.get("target", {}).get("area_id", []),
                "device_id": action.get("target", {}).get("device_id", []),
            },
        }
        if action.get("service_data"):
            step["data"] = action["service_data"]
        sequence.append(step)

    script_yaml = {
        script_key: {
            "alias": preset.get("name", script_key),
            "mode": "single",
            "sequence": sequence,
        }
    }

    trigger = preset.get("trigger", {})
    trigger_type = trigger.get("type", "manual")

    automation_yaml = None
    if trigger_type != "manual":
        compiled_trigger = _compile_trigger(trigger)
        compiled_conditions = [_compile_condition(c) for c in preset.get("conditions", [])]
        compiled_conditions = [c for c in compiled_conditions if c is not None]

        automation_yaml = {
            f"automation_{script_key}": {
                "alias": f"Auto {preset.get('name', script_key)}",
                "trigger": compiled_trigger,
                "condition": compiled_conditions,
                "action": [
                    {
                        "service": "script.turn_on",
                        "target": {"entity_id": f"script.{script_key}"},
                    }
                ],
                "mode": "single",
            }
        }

    return {
        "script_key": script_key,
        "script": script_yaml,
        "automation": automation_yaml,
    }


def _compile_trigger(trigger: dict):
    t = trigger.get("type")
    if t == "time":
        at = trigger.get("at") or "07:00:00"
        return [{"platform": "time", "at": at}]

    if t == "state":
        return [
            {
                "platform": "state",
                "entity_id": trigger.get("entity_id"),
                "from": trigger.get("from"),
                "to": trigger.get("to"),
            }
        ]

    if t == "event":
        return [
            {
                "platform": "event",
                "event_type": trigger.get("event_type"),
                "event_data": trigger.get("event_data", {}),
            }
        ]

    return []


def _compile_condition(cond: dict):
    cond_type = cond.get("type")
    if cond_type == "state":
        return {
            "condition": "state",
            "entity_id": cond.get("entity_id"),
            "state": cond.get("state"),
        }

    if cond_type == "numeric_state":
        out = {
            "condition": "numeric_state",
            "entity_id": cond.get("entity_id"),
        }
        if "above" in cond:
            out["above"] = cond.get("above")
        if "below" in cond:
            out["below"] = cond.get("below")
        return out

    if cond_type == "time":
        out: dict[str, object] = {"condition": "time"}
        after = cond.get("after")
        before = cond.get("before")
        if after is not None:
            out["after"] = str(after)
        if before is not None:
            out["before"] = str(before)
        return out

    return None


def build_store_from_config() -> PresetStore:
    cfg = get_config() or {}
    preset_cfg = cfg.get("presets") or {}
    configured_file = preset_cfg.get("storage_file")

    if configured_file:
        storage_file = str(configured_file)
    else:
        base_dir = os.path.dirname(os.path.dirname(__file__))
        storage_file = os.path.join(base_dir, "data", "presets.json")

    return PresetStore(storage_file=storage_file)
