"""Automation management backend for HumbleVoice.

This module provides create/update/delete flows backed by Home Assistant APIs,
including:
- optional AI-generated names using HA conversation agent
- metadata marker for AI-generated names
- two-step confirmation for update/delete
- relevance matching based on name + content
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from collections.abc import Callable

import requests
import yaml

from config_loader import get_config

logger = logging.getLogger(__name__)

_META_PREFIX = "HV_META:"
_META_PATTERN = re.compile(r"(?:\n|\r\n)?HV_META:(\{.*\})\s*$", re.DOTALL)


class AutomationError(RuntimeError):
    """Raised when automation operation fails."""


@dataclass
class MatchResult:
    score: float
    automation: dict[str, Any]
    summary: str


class PendingActionStore:
    def __init__(self, ttl_seconds: int = 300):
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, dict[str, Any]] = {}

    def put(self, payload: dict[str, Any]) -> str:
        token = uuid.uuid4().hex
        self._items[token] = {
            "created_at": time.time(),
            "payload": payload,
        }
        self._cleanup()
        return token

    def pop(self, token: str) -> dict[str, Any] | None:
        self._cleanup()
        item = self._items.pop(token, None)
        if not item:
            return None
        return dict(item.get("payload") or {})

    def pop_latest(self, operation: str | None = None) -> tuple[str, dict[str, Any]] | None:
        self._cleanup()
        candidates: list[tuple[str, float, dict[str, Any]]] = []
        for key, item in self._items.items():
            payload = dict(item.get("payload") or {})
            if operation and str(payload.get("operation") or "") != operation:
                continue
            candidates.append((key, float(item.get("created_at") or 0), payload))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        token = candidates[0][0]
        payload = self._items.pop(token, {}).get("payload") or {}
        return token, dict(payload)

    def _cleanup(self):
        now = time.time()
        expired = [
            key
            for key, value in self._items.items()
            if now - float(value.get("created_at", 0)) > self._ttl_seconds
        ]
        for key in expired:
            self._items.pop(key, None)


class HomeAssistantAutomationClient:
    def __init__(
        self,
        request_with_fallback: Callable[
            [str, str, str, dict[str, str], float, dict[str, Any] | None],
            requests.Response,
        ]
        | None = None,
    ):
        cfg = get_config()
        ha_cfg = cfg.get("home_assistant", {}) if cfg else {}

        self.base_url = str(ha_cfg.get("url", "http://homeassistant.local:8123")).rstrip("/")
        self.token = str(ha_cfg.get("token", "")).strip()
        self.timeout = float(ha_cfg.get("automation_api_timeout_s", 20))
        self.conversation_language = str(ha_cfg.get("conversation_language", "zh")).strip() or "zh"
        # Prefer the new key `automation_conversation_agent`, keep legacy fallback for compatibility.
        self.conversation_agent_id = str(
            ha_cfg.get("automation_conversation_agent")
            or ha_cfg.get("conversation_agent_id", "")
        ).strip()
        self.config_dir = os.path.abspath(
            str(ha_cfg.get("config_dir", "/var/lib/homeassistant/homeassistant")).strip()
        )
        self.automations_file = str(ha_cfg.get("automations_file", "automations.yaml")).strip() or "automations.yaml"
        self.automations_path = os.path.join(self.config_dir, self.automations_file)
        default_backup_dir = os.path.join(self.config_dir, "backups", "automations")
        self.automation_backup_dir = os.path.abspath(
            str(ha_cfg.get("automation_backup_dir", default_backup_dir)).strip()
        )
        self.automation_backup_keep = int(ha_cfg.get("automation_backup_keep", 20))
        self.automation_plan_retry_max = max(0, int(ha_cfg.get("automation_plan_retry_max", 2)))
        self.automation_auto_expose_default = bool(ha_cfg.get("automation_auto_expose_default", True))
        self.manage_only_exposed_automations = bool(ha_cfg.get("manage_only_exposed_automations", True))

        if not self.token:
            raise AutomationError("home_assistant.token is empty; cannot manage automations")

        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        self._request_with_fallback = request_with_fallback

    @property
    def uses_default_conversation_agent(self) -> bool:
        return not bool(self.conversation_agent_id)

    @property
    def conversation_agent_label(self) -> str:
        return "default(home_assistant)" if self.uses_default_conversation_agent else self.conversation_agent_id

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> requests.Response:
        try:
            if self._request_with_fallback is not None:
                return self._request_with_fallback(
                    method,
                    self.base_url,
                    path,
                    self._headers,
                    self.timeout,
                    json_body,
                )

            url = f"{self.base_url}{path}"
            return requests.request(
                method=method,
                url=url,
                headers=self._headers,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AutomationError(f"request to HA failed: {exc}") from exc

    def _request_json(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        resp = self._request(method, path, json_body=json_body)
        if resp.status_code >= 400:
            raise AutomationError(f"HA API error {resp.status_code} on {path}: {resp.text}")
        if not resp.text.strip():
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise AutomationError(f"HA API returned non-JSON on {path}: {resp.text}") from exc

    def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None):
        self._request_json("POST", f"/api/services/{domain}/{service}", json_body=data or {})

    def reload_automations(self):
        self.call_service("automation", "reload", {})

    def _build_ws_url(self) -> str:
        parsed = urlparse(self.base_url)
        if not parsed.netloc:
            raise AutomationError(f"invalid Home Assistant URL: {self.base_url}")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, "/api/websocket", "", "", ""))

    def _candidate_base_urls(self) -> list[str]:
        primary = str(self.base_url or "").rstrip("/")
        candidates: list[str] = []
        if primary:
            candidates.append(primary)

        parsed = urlparse(primary)
        if parsed.hostname == "homeassistant.local":
            candidates.extend([
                "http://localhost:8123",
                "http://127.0.0.1:8123",
                "http://homeassistant:8123",
            ])

        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item and item not in seen:
                deduped.append(item)
                seen.add(item)
        return deduped

    @staticmethod
    def _is_name_resolution_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "name or service not known" in msg
            or "failed to resolve" in msg
            or "name resolution" in msg
            or "[errno -2]" in msg
        )

    def _ws_send_command(self, command: dict[str, Any]) -> Any:
        try:
            from websockets.sync.client import connect as ws_connect
        except Exception as exc:
            raise AutomationError(f"websocket sync client is unavailable: {exc}") from exc

        last_exc: Exception | None = None
        for idx, base in enumerate(self._candidate_base_urls()):
            ws_url = self._build_ws_url() if idx == 0 else urlunparse(
                (
                    "wss" if urlparse(base).scheme == "https" else "ws",
                    urlparse(base).netloc,
                    "/api/websocket",
                    "",
                    "",
                    "",
                )
            )
            try:
                with ws_connect(ws_url, open_timeout=self.timeout, close_timeout=self.timeout) as ws:
                    first = json.loads(ws.recv())
                    if str(first.get("type") or "") != "auth_required":
                        raise AutomationError(f"unexpected websocket greeting: {first}")

                    ws.send(json.dumps({"type": "auth", "access_token": self.token}, ensure_ascii=False))
                    auth_resp = json.loads(ws.recv())
                    if str(auth_resp.get("type") or "") != "auth_ok":
                        raise AutomationError(f"websocket auth failed: {auth_resp}")

                    msg = {"id": 1}
                    msg.update(command)
                    ws.send(json.dumps(msg, ensure_ascii=False))

                    while True:
                        packet = json.loads(ws.recv())
                        if int(packet.get("id") or 0) != 1:
                            continue
                        if str(packet.get("type") or "") != "result":
                            continue
                        if bool(packet.get("success", False)):
                            if idx > 0:
                                logger.warning("HA websocket fallback succeeded via %s", base)
                            return packet.get("result")
                        err = packet.get("error") or {}
                        raise AutomationError(
                            f"HA websocket command failed: {err.get('code') or 'unknown'} {err.get('message') or ''}"
                        )
            except AutomationError:
                raise
            except Exception as exc:
                last_exc = exc
                if idx == 0 and self._is_name_resolution_error(exc):
                    logger.warning("HA websocket primary host resolve failed via %s", base)
                logger.warning("HA websocket request failed via %s: %s", base, exc)

        raise AutomationError(f"HA websocket request failed: {last_exc}") from last_exc

    @staticmethod
    def _slugify_name(name: str) -> str:
        s = str(name or "").strip().lower()
        s = re.sub(r"[^a-z0-9_\-\s]", "", s)
        s = re.sub(r"[\s\-]+", "_", s)
        s = re.sub(r"_+", "_", s)
        return s.strip("_")

    def resolve_automation_entity_id(self, automation_id: str, alias: str) -> str | None:
        states = self._request_json("GET", "/api/states")
        if not isinstance(states, list):
            return None

        candidates = [s for s in states if isinstance(s, dict) and str(s.get("entity_id") or "").startswith("automation.")]
        if not candidates:
            return None

        target_id = str(automation_id or "").strip()
        if target_id:
            for item in candidates:
                attrs_raw = item.get("attributes")
                attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
                if str(attrs.get("id") or "").strip() == target_id:
                    return str(item.get("entity_id") or "").strip() or None

        target_alias = str(alias or "").strip()
        if target_alias:
            for item in candidates:
                attrs_raw = item.get("attributes")
                attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
                if str(attrs.get("friendly_name") or "").strip() == target_alias:
                    return str(item.get("entity_id") or "").strip() or None

            slug = self._slugify_name(target_alias)
            if slug:
                entity_id = f"automation.{slug}"
                for item in candidates:
                    if str(item.get("entity_id") or "").strip() == entity_id:
                        return entity_id

        return None

    def set_entity_exposed_to_conversation(self, entity_id: str, should_expose: bool) -> None:
        if not entity_id:
            raise AutomationError("entity_id is empty for expose operation")

        self._ws_send_command(
            {
                "type": "homeassistant/expose_entity",
                "assistants": ["conversation"],
                "entity_ids": [entity_id],
                "should_expose": bool(should_expose),
            }
        )

    def list_exposed_entities(self) -> dict[str, dict[str, bool]]:
        result = self._ws_send_command({"type": "homeassistant/expose_entity/list"})
        if not isinstance(result, dict):
            return {}
        exposed_raw = result.get("exposed_entities")
        if not isinstance(exposed_raw, dict):
            return {}

        exposed: dict[str, dict[str, bool]] = {}
        for entity_id, flags in exposed_raw.items():
            if not isinstance(entity_id, str) or not isinstance(flags, dict):
                continue
            mapped: dict[str, bool] = {}
            for assistant, value in flags.items():
                if isinstance(assistant, str):
                    mapped[assistant] = bool(value)
            exposed[entity_id] = mapped
        return exposed

    def validate_automation_config(
        self,
        triggers: list[dict[str, Any]],
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> str | None:
        result = self._ws_send_command(
            {
                "type": "validate_config",
                "triggers": triggers,
                "conditions": conditions,
                "actions": actions,
            }
        )

        if not isinstance(result, dict):
            return "validate_config returned non-object result"

        issues: list[str] = []
        for key in ("triggers", "conditions", "actions"):
            part = result.get(key)
            if not isinstance(part, dict):
                continue
            valid = bool(part.get("valid", False))
            if valid:
                continue
            err = str(part.get("error") or "unknown validation error").strip()
            issues.append(f"{key}: {err}")

        if issues:
            return "; ".join(issues)
        return None

    def _load_file_automations(self) -> list[dict[str, Any]]:
        try:
            if not os.path.exists(self.automations_path):
                return []

            with open(self.automations_path, "r", encoding="utf-8") as f:
                payload = yaml.safe_load(f)

            if payload is None:
                return []
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                # Be permissive in case file content is wrapped.
                candidate = payload.get("automation")
                if isinstance(candidate, list):
                    return [item for item in candidate if isinstance(item, dict)]
            raise AutomationError("automations.yaml format is invalid; expected a YAML list")
        except Exception as exc:
            raise AutomationError(f"failed to load automations file {self.automations_path}: {exc}") from exc

    def _save_file_automations(self, automations: list[dict[str, Any]]):
        try:
            os.makedirs(os.path.dirname(self.automations_path), exist_ok=True)
            self._backup_automations_file_if_needed()
            with open(self.automations_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(automations, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            raise AutomationError(f"failed to save automations file {self.automations_path}: {exc}") from exc

    def _backup_automations_file_if_needed(self):
        if not os.path.exists(self.automations_path):
            return

        os.makedirs(self.automation_backup_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_name = f"automations.{timestamp}.{uuid.uuid4().hex[:8]}.yaml.bak"
        backup_path = os.path.join(self.automation_backup_dir, backup_name)
        shutil.copy2(self.automations_path, backup_path)
        self._prune_old_backups()

    def _prune_old_backups(self):
        keep = max(0, int(self.automation_backup_keep))
        if keep <= 0:
            return

        try:
            files = [
                os.path.join(self.automation_backup_dir, name)
                for name in os.listdir(self.automation_backup_dir)
                if name.startswith("automations.") and name.endswith(".yaml.bak")
            ]
        except FileNotFoundError:
            return

        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old_file in files[keep:]:
            try:
                os.remove(old_file)
            except FileNotFoundError:
                continue

    def _ensure_ids(self, automations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        changed = False
        updated: list[dict[str, Any]] = []
        for item in automations:
            entry = dict(item)
            cur_id = str(entry.get("id") or entry.get("automation_id") or "").strip()
            if not cur_id:
                entry["id"] = uuid.uuid4().hex
                changed = True
            updated.append(entry)

        if changed:
            self._save_file_automations(updated)
        return updated

    def list_automations(self) -> list[dict[str, Any]]:
        automations = self._load_file_automations()
        return self._ensure_ids(automations)

    def create_automation(self, automation_data: dict[str, Any]) -> dict[str, Any]:
        automations = self._ensure_ids(self._load_file_automations())
        trigger_list = automation_data.get("triggers") if isinstance(automation_data.get("triggers"), list) else None
        if trigger_list is None:
            trigger_list = automation_data.get("trigger") if isinstance(automation_data.get("trigger"), list) else []
        condition_list = automation_data.get("conditions") if isinstance(automation_data.get("conditions"), list) else None
        if condition_list is None:
            condition_list = automation_data.get("condition") if isinstance(automation_data.get("condition"), list) else []
        action_list = automation_data.get("actions") if isinstance(automation_data.get("actions"), list) else None
        if action_list is None:
            action_list = automation_data.get("action") if isinstance(automation_data.get("action"), list) else []

        new_item = {
            "id": uuid.uuid4().hex,
            "alias": str(automation_data.get("alias") or "").strip(),
            "description": str(automation_data.get("description") or "").strip(),
            "triggers": trigger_list,
            "conditions": condition_list,
            "actions": action_list,
            "mode": str(automation_data.get("mode") or "single").strip() or "single",
        }
        automations.append(new_item)
        self._save_file_automations(automations)
        return dict(new_item)

    def update_automation(self, automation_id: str, automation_data: dict[str, Any]) -> dict[str, Any]:
        automations = self._ensure_ids(self._load_file_automations())
        for idx, item in enumerate(automations):
            item_id = str(item.get("id") or item.get("automation_id") or "").strip()
            if item_id != automation_id:
                continue

            updated = dict(item)
            updated["alias"] = str(automation_data.get("alias") or updated.get("alias") or "").strip()
            updated["description"] = str(automation_data.get("description") or updated.get("description") or "").strip()
            trigger_list = automation_data.get("triggers") if isinstance(automation_data.get("triggers"), list) else None
            if trigger_list is None and isinstance(automation_data.get("trigger"), list):
                trigger_list = automation_data.get("trigger")
            condition_list = automation_data.get("conditions") if isinstance(automation_data.get("conditions"), list) else None
            if condition_list is None and isinstance(automation_data.get("condition"), list):
                condition_list = automation_data.get("condition")
            action_list = automation_data.get("actions") if isinstance(automation_data.get("actions"), list) else None
            if action_list is None and isinstance(automation_data.get("action"), list):
                action_list = automation_data.get("action")

            if isinstance(trigger_list, list):
                updated["triggers"] = trigger_list
                updated.pop("trigger", None)
            if isinstance(condition_list, list):
                updated["conditions"] = condition_list
                updated.pop("condition", None)
            if isinstance(action_list, list):
                updated["actions"] = action_list
                updated.pop("action", None)
            updated["mode"] = str(automation_data.get("mode") or updated.get("mode") or "single").strip() or "single"

            automations[idx] = updated
            self._save_file_automations(automations)
            return dict(updated)

        raise AutomationError(f"failed to update automation {automation_id}: not found in {self.automations_path}")

    def delete_automation(self, automation_id: str):
        automations = self._ensure_ids(self._load_file_automations())
        before = len(automations)
        filtered = [
            item
            for item in automations
            if str(item.get("id") or item.get("automation_id") or "").strip() != automation_id
        ]
        if len(filtered) == before:
            raise AutomationError(f"failed to delete automation {automation_id}: not found in {self.automations_path}")
        self._save_file_automations(filtered)

    def ask_conversation(self, prompt: str, language: str | None = None) -> str:
        body: dict[str, Any] = {
            "text": prompt,
            "language": language or self.conversation_language,
        }
        if self.conversation_agent_id:
            body["agent_id"] = self.conversation_agent_id

        payload = self._request_json("POST", "/api/conversation/process", json_body=body)
        response = payload.get("response") or {}
        speech = response.get("speech") or {}
        plain_raw = speech.get("plain")
        ssml_raw = speech.get("ssml")
        plain = plain_raw if isinstance(plain_raw, dict) else {}
        ssml = ssml_raw if isinstance(ssml_raw, dict) else {}
        text = str(plain.get("speech") or ssml.get("speech") or "").strip()
        return text


class AutomationManager:
    def __init__(
        self,
        request_with_fallback: Callable[
            [str, str, str, dict[str, str], float, dict[str, Any] | None],
            requests.Response,
        ]
        | None = None,
    ):
        self.client = HomeAssistantAutomationClient(request_with_fallback=request_with_fallback)
        self.pending = PendingActionStore(ttl_seconds=300)

    @staticmethod
    def _strip_meta(description: str) -> tuple[str, dict[str, Any]]:
        desc = str(description or "")
        match = _META_PATTERN.search(desc)
        if not match:
            return desc.strip(), {}
        meta_raw = match.group(1)
        try:
            meta = json.loads(meta_raw)
            if not isinstance(meta, dict):
                meta = {}
        except json.JSONDecodeError:
            meta = {}
        clean = _META_PATTERN.sub("", desc).strip()
        return clean, meta

    @staticmethod
    def _build_description(base_description: str, meta: dict[str, Any]) -> str:
        clean, _ = AutomationManager._strip_meta(base_description)
        if not meta:
            return clean
        meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        if clean:
            return f"{clean}\n\n{_META_PREFIX}{meta_json}"
        return f"{_META_PREFIX}{meta_json}"

    @staticmethod
    def _extract_json_block(text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw, re.IGNORECASE)
        candidate = fenced.group(1) if fenced else raw
        if not fenced:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start >= 0 and end > start:
                candidate = candidate[start : end + 1]

        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _looks_like_agent_failure_text(text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return True
        patterns = [
            "抱歉",
            "无法理解",
            "不明白",
            "换个说法",
            "sorry",
            "i don't understand",
            "can you rephrase",
            "no_intent_match",
            "failed_to_handle",
        ]
        return any(p in raw for p in patterns)

    def run_conversation_self_check(self) -> dict[str, Any]:
        """Best-effort startup check for conversation planning capability."""
        status = {
            "ok": False,
            "agent": self.client.conversation_agent_label,
            "uses_default_agent": self.client.uses_default_conversation_agent,
            "message": "",
        }

        prompt = (
            "只输出JSON，不要输出其它内容。"
            "JSON格式: {\"ping\":\"pong\",\"ok\":true}"
        )
        try:
            raw = self.client.ask_conversation(prompt, language=self.client.conversation_language)
        except Exception as exc:
            status["message"] = f"conversation API unavailable: {exc}"
            return status

        parsed = self._extract_json_block(raw)
        if isinstance(parsed, dict) and str(parsed.get("ping") or "").lower() == "pong":
            status["ok"] = True
            status["message"] = "conversation self-check passed"
            return status

        if self._looks_like_agent_failure_text(raw):
            status["message"] = "conversation agent returned fallback text, not structured JSON"
        else:
            status["message"] = "conversation agent response is not valid planning JSON"
        return status

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = str(value or "").lower()
        text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _sequence_score(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return float(SequenceMatcher(None, a, b).ratio())

    @staticmethod
    def _token_overlap_score(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        sa = set(a.split())
        sb = set(b.split())
        if not sa or not sb:
            return 0.0
        inter = len(sa.intersection(sb))
        denom = max(len(sa), len(sb))
        return float(inter / denom) if denom else 0.0

    @staticmethod
    def _entry_triggers(entry: dict[str, Any]) -> list[dict[str, Any]]:
        value = entry.get("triggers")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        value = entry.get("trigger")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []

    @staticmethod
    def _entry_conditions(entry: dict[str, Any]) -> list[dict[str, Any]]:
        value = entry.get("conditions")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        value = entry.get("condition")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []

    @staticmethod
    def _entry_actions(entry: dict[str, Any]) -> list[dict[str, Any]]:
        value = entry.get("actions")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        value = entry.get("action")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []

    @staticmethod
    def _normalize_time_string(value: str) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", raw)
        if not m:
            return None
        hh = int(m.group(1))
        mm = int(m.group(2))
        ss = int(m.group(3) or 0)
        if hh < 0 or hh > 23 or mm < 0 or mm > 59 or ss < 0 or ss > 59:
            return None
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

    @staticmethod
    def _coerce_script_service(script_raw: str) -> str | None:
        script = str(script_raw or "").strip()
        if not script:
            return None
        if script.startswith("scripts."):
            script = "script." + script.split(".", 1)[1]
        elif not script.startswith("script."):
            script = f"script.{script}"
        return script

    def _extract_time_trigger_from_text(self, text: str) -> list[dict[str, Any]]:
        raw = str(text or "")
        m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", raw)
        if not m:
            return []
        at = self._normalize_time_string(m.group(1))
        if not at:
            return []
        return [{"trigger": "time", "at": at}]

    def _extract_script_action_from_text(self, text: str) -> list[dict[str, Any]]:
        raw = str(text or "")
        m = re.search(r"(?:script|scripts)\.([a-zA-Z0-9_]+)", raw)
        script_name = m.group(1) if m else ""
        if not script_name:
            m2 = re.search(r"脚本(?:模式)?\s*([a-zA-Z0-9_]+)", raw)
            script_name = m2.group(1) if m2 else ""
        service = self._coerce_script_service(script_name)
        if not service:
            return []
        return [{"action": service}]

    def _dry_run_validate_payload(
        self,
        triggers: list[dict[str, Any]],
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        mode: str,
    ) -> str | None:
        if not triggers:
            return "missing triggers"
        if not actions:
            return "missing actions"

        allowed_modes = {"single", "restart", "queued", "parallel"}
        if mode not in allowed_modes:
            return f"invalid mode: {mode}"

        for idx, trig in enumerate(triggers):
            if not isinstance(trig, dict):
                return f"trigger[{idx}] is not an object"
            trigger_type = str(trig.get("trigger") or "").strip().lower()
            if not trigger_type:
                return f"trigger[{idx}] missing required key: trigger"
            if trigger_type == "time":
                at = self._normalize_time_string(str(trig.get("at") or "").strip())
                if not at:
                    return f"trigger[{idx}] invalid time trigger, requires at=HH:MM[:SS]"

        for idx, cond in enumerate(conditions):
            if not isinstance(cond, dict):
                return f"condition[{idx}] is not an object"
            if not str(cond.get("condition") or "").strip():
                return f"condition[{idx}] missing required key: condition"

        for idx, act in enumerate(actions):
            if not isinstance(act, dict):
                return f"action[{idx}] is not an object"
            action_service = str(act.get("action") or "").strip()
            if not action_service:
                return f"action[{idx}] missing required key: action"
            if "." not in action_service:
                return f"action[{idx}] should be domain.service format"

        ha_validation_error = self.client.validate_automation_config(triggers, conditions, actions)
        if ha_validation_error:
            return f"ha validate_config failed: {ha_validation_error}"

        return None

    def _normalize_trigger_list(self, trigger_list: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in trigger_list:
            if not isinstance(item, dict):
                continue
            trigger_type = str(item.get("trigger") or item.get("platform") or item.get("event") or "").strip().lower()
            if trigger_type == "time":
                at = self._normalize_time_string(str(item.get("at") or item.get("from") or "").strip())
                if at:
                    normalized.append({"trigger": "time", "at": at})
                continue
            # Keep already valid-looking trigger entries.
            if item.get("trigger"):
                normalized.append(item)

        if normalized:
            return normalized
        return self._extract_time_trigger_from_text(text)

    def _normalize_action_list(self, action_list: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in action_list:
            if not isinstance(item, dict):
                continue
            service = str(item.get("action") or item.get("service") or "").strip()
            if not service and str(item.get("type") or "").strip().lower() == "call_script":
                service = str(item.get("script") or item.get("entity_id") or "").strip()
            if not service and item.get("script"):
                service = str(item.get("script") or "").strip()

            service = self._coerce_script_service(service) or service
            if service:
                normalized.append({"action": service})

        if normalized:
            return normalized
        return self._extract_script_action_from_text(text)

    def _content_text(self, automation: dict[str, Any]) -> str:
        alias = str(automation.get("alias") or automation.get("name") or "")
        description = str(automation.get("description") or "")
        clean_desc, meta = self._strip_meta(description)
        summary = str(meta.get("summary") or "")
        trigger = json.dumps(self._entry_triggers(automation), ensure_ascii=False)
        condition = json.dumps(self._entry_conditions(automation), ensure_ascii=False)
        action = json.dumps(self._entry_actions(automation), ensure_ascii=False)
        mode = str(automation.get("mode") or "")
        return " ".join([alias, clean_desc, summary, trigger, condition, action, mode]).strip()

    def _managed_automations(self) -> list[dict[str, Any]]:
        automations = self.client.list_automations()
        if not automations:
            return []

        if not self.client.manage_only_exposed_automations:
            return automations

        exposed_map = self.client.list_exposed_entities()
        if not exposed_map:
            return []

        managed: list[dict[str, Any]] = []
        for item in automations:
            automation_id = str(item.get("id") or item.get("automation_id") or "").strip()
            alias = str(item.get("alias") or item.get("name") or "").strip()
            entity_id = self.client.resolve_automation_entity_id(automation_id=automation_id, alias=alias)
            if not entity_id:
                continue
            flags = exposed_map.get(entity_id)
            if isinstance(flags, dict) and bool(flags.get("conversation", False)):
                managed.append(item)
        return managed

    def _match_automation(self, query_name: str, query_content: str) -> MatchResult:
        automations = self._managed_automations()
        if not automations:
            if self.client.manage_only_exposed_automations:
                raise AutomationError("no automation exposed to Assist is available for management")
            raise AutomationError("no automation found in Home Assistant")

        qn = self._normalize_text(query_name)
        qc = self._normalize_text(query_content)

        best: MatchResult | None = None
        for item in automations:
            alias = str(item.get("alias") or item.get("name") or "")
            name_norm = self._normalize_text(alias)
            content_norm = self._normalize_text(self._content_text(item))

            name_score = max(self._sequence_score(qn, name_norm), self._token_overlap_score(qn, name_norm))
            content_score = max(
                self._sequence_score(qc, content_norm),
                self._token_overlap_score(qc, content_norm),
            )
            if not qn:
                name_score = 0.0
            if not qc:
                content_score = 0.0

            combined = 0.55 * name_score + 0.45 * content_score
            if not qn and qc:
                combined = content_score
            if qn and not qc:
                combined = name_score
            if not qn and not qc:
                combined = self._sequence_score(self._normalize_text(query_content), content_norm)

            summary = self._summarize_automation(item)
            current = MatchResult(score=combined, automation=item, summary=summary)
            if best is None or current.score > best.score:
                best = current

        if best is None:
            raise AutomationError("failed to match automation")
        return best

    def _summarize_automation(self, automation: dict[str, Any]) -> str:
        description = str(automation.get("description") or "")
        clean_desc, meta = self._strip_meta(description)
        summary = str(meta.get("summary") or "").strip()
        if summary:
            return summary

        trigger = self._entry_triggers(automation)
        action = self._entry_actions(automation)
        trigger_brief = json.dumps(trigger[:1], ensure_ascii=False)
        action_brief = json.dumps(action[:1], ensure_ascii=False)
        generated = f"触发: {trigger_brief}; 动作: {action_brief}"
        if clean_desc:
            return f"{clean_desc} | {generated}"
        return generated

    def _fallback_name(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if not normalized:
            return f"AI任务_{int(time.time())}"
        clipped = normalized[:24]
        return f"AI任务_{clipped}"

    def _plan_from_text(
        self,
        operation: str,
        text: str,
        language: str | None = None,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        prompt = (
            "你是 Home Assistant 自动化规划器。"
            "请将用户需求转换成 JSON。"
            "仅输出 JSON，不要输出其他文字。"
            "JSON schema: "
            "{"
            '"target_name":"string or empty",'
            '"new_name":"string or empty",'
            '"name_specified":true|false,'
            '"summary":"string",'
            '"expose_to_assist":true|false|empty,'
            '"trigger":[],"condition":[],"action":[],"mode":"single|restart|queued|parallel|empty"'
            "}."
            f"operation={operation}; user_text={text}"
        )
        if retry_reason:
            prompt += (
                " 上一次输出未通过校验，原因是: "
                f"{retry_reason}. "
                "请返回可直接用于 Home Assistant automation YAML 的结果："
                "trigger 列表每项必须有 trigger 键，time trigger 必须使用 at；"
                "action 列表每项必须有 action 键并使用 domain.service 格式。"
            )
        raw = self.client.ask_conversation(prompt, language=language)
        parsed = self._extract_json_block(raw) or {}

        if not parsed:
            if self.client.uses_default_conversation_agent:
                raise AutomationError(
                    "default conversation agent cannot produce structured planning JSON for this request. "
                    "Please configure home_assistant.automation_conversation_agent to an LLM-capable agent."
                )
            raise AutomationError(
                "conversation agent did not return structured JSON for automation planning. "
                "Please configure home_assistant.automation_conversation_agent to an LLM-capable agent."
            )

        result = {
            "target_name": str(parsed.get("target_name") or "").strip(),
            "new_name": str(parsed.get("new_name") or "").strip(),
            "name_specified": bool(parsed.get("name_specified", False)),
            "summary": str(parsed.get("summary") or "").strip(),
            "expose_to_assist": parsed.get("expose_to_assist"),
            "trigger": parsed.get("trigger") if isinstance(parsed.get("trigger"), list) else [],
            "condition": parsed.get("condition") if isinstance(parsed.get("condition"), list) else [],
            "action": parsed.get("action") if isinstance(parsed.get("action"), list) else [],
            "mode": str(parsed.get("mode") or "").strip(),
        }
        return result

    def _wants_no_expose_from_text(self, text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False

        patterns = [
            r"不暴露",
            r"不要暴露",
            r"别暴露",
            r"不公开",
            r"不要公开",
            r"不让语音",
            r"不要让语音",
            r"do\s+not\s+expose",
            r"don't\s+expose",
            r"dont\s+expose",
            r"not\s+expose",
            r"private\s+automation",
            r"keep\s+it\s+private",
        ]
        return any(re.search(p, raw) for p in patterns)

    def _resolve_expose_preference(self, text: str, plan: dict[str, Any]) -> bool:
        expose_raw = plan.get("expose_to_assist")
        if isinstance(expose_raw, bool):
            return expose_raw
        if isinstance(expose_raw, str):
            value = expose_raw.strip().lower()
            if value in {"true", "yes", "1", "on"}:
                return True
            if value in {"false", "no", "0", "off"}:
                return False

        if self._wants_no_expose_from_text(text):
            return False
        return self.client.automation_auto_expose_default

    def _generate_name(self, summary: str, text: str, language: str | None = None) -> str:
        prompt = (
            "你是 Home Assistant 语音助手。"
            "请为一个自动化生成简洁名称，只输出名称，不要解释。"
            "限制在 24 个字符以内。"
            f"summary={summary}; user_text={text}"
        )
        generated = self.client.ask_conversation(prompt, language=language)
        if self._looks_like_agent_failure_text(generated):
            return self._fallback_name(summary or text)

        # Some conversation agents are constrained to always return planning JSON.
        # If that happens, extract a usable name field instead of writing raw JSON into alias.
        generated_json = self._extract_json_block(generated)
        if isinstance(generated_json, dict):
            candidate = str(
                generated_json.get("new_name")
                or generated_json.get("target_name")
                or generated_json.get("summary")
                or ""
            ).strip()
            if not candidate:
                return self._fallback_name(summary or text)
            generated = candidate

        clean = re.sub(r"\s+", " ", generated).strip()
        if not clean:
            return self._fallback_name(summary or text)
        # remove common wrapper quotes/prefixes
        clean = clean.strip('"“”')
        if len(clean) > 32:
            clean = clean[:32].strip()
        return clean or self._fallback_name(summary or text)

    @staticmethod
    def is_confirmation_text(text: str) -> bool:
        normalized = re.sub(r"\s+", "", str(text or "").lower())
        keywords = ["确认", "确定", "yes", "confirm", "ok", "好的"]
        return any(k in normalized for k in keywords)

    def create_from_text(self, text: str, language: str | None = None) -> dict[str, Any]:
        max_attempts = self.client.automation_plan_retry_max + 1
        retry_reason: str | None = None
        plan: dict[str, Any] = {}

        final_name = ""
        ai_generated_name = False
        summary = ""
        mode = "single"
        trigger: list[dict[str, Any]] = []
        condition: list[dict[str, Any]] = []
        action: list[dict[str, Any]] = []

        for attempt in range(max_attempts):
            plan = self._plan_from_text("create", text, language=language, retry_reason=retry_reason)
            name_specified = bool(plan.get("name_specified"))
            requested_name = str(plan.get("new_name") or plan.get("target_name") or "").strip()
            summary = str(plan.get("summary") or "").strip() or str(text).strip()

            ai_generated_name = False
            if name_specified and requested_name:
                final_name = requested_name
            else:
                final_name = self._generate_name(summary=summary, text=text, language=language)
                ai_generated_name = True

            mode = str(plan.get("mode") or "").strip() or "single"
            trigger_raw_any = plan.get("trigger")
            if not isinstance(trigger_raw_any, list):
                trigger_raw_any = []
            condition_raw_any = plan.get("condition")
            condition_any: list[Any] = condition_raw_any if isinstance(condition_raw_any, list) else []
            action_raw_any = plan.get("action")
            if not isinstance(action_raw_any, list):
                action_raw_any = []

            trigger_raw: list[dict[str, Any]] = [item for item in trigger_raw_any if isinstance(item, dict)]
            condition = [item for item in condition_any if isinstance(item, dict)]
            action_raw: list[dict[str, Any]] = [item for item in action_raw_any if isinstance(item, dict)]

            trigger = self._normalize_trigger_list(trigger_raw, text)
            action = self._normalize_action_list(action_raw, text)

            retry_reason = self._dry_run_validate_payload(trigger, condition, action, mode)
            if retry_reason is None:
                break

            if attempt >= max_attempts - 1:
                raise AutomationError(
                    "automation dry-run failed after retries: "
                    f"{retry_reason}. Please provide a clearer command or adjust the conversation agent prompt."
                )

        metadata = {
            "ai_generated_name": ai_generated_name,
            "summary": summary,
            "updated_at": int(time.time()),
        }
        description = self._build_description("", metadata)

        payload = {
            "alias": final_name,
            "description": description,
            "trigger": trigger,
            "condition": condition,
            "action": action,
            "mode": mode,
        }

        created = self.client.create_automation(payload)
        self.client.reload_automations()

        automation_id = str(created.get("id") or created.get("automation_id") or "")
        should_expose = self._resolve_expose_preference(text=text, plan=plan)
        exposed_to_assist: bool | None = None
        exposure_warning = ""
        exposed_entity_id = ""

        if should_expose:
            try:
                exposed_entity_id = str(
                    self.client.resolve_automation_entity_id(automation_id=automation_id, alias=final_name) or ""
                ).strip()
                if not exposed_entity_id:
                    raise AutomationError("automation entity_id was not found after reload")
                self.client.set_entity_exposed_to_conversation(exposed_entity_id, should_expose=True)
                exposed_to_assist = True
            except Exception as exc:
                exposed_to_assist = False
                exposure_warning = f"created but failed to expose to Assist: {exc}"
                logger.warning(exposure_warning)
        else:
            exposed_to_assist = False

        if self.client.manage_only_exposed_automations and not exposed_to_assist:
            raise AutomationError(
                "automation created but not exposed to Assist; policy blocks managing non-exposed automations"
            )

        return {
            "ok": True,
            "operation": "create",
            "automation_id": automation_id,
            "name": final_name,
            "ai_generated_name": ai_generated_name,
            "summary": summary,
            "assist_exposed": exposed_to_assist,
            "assist_entity_id": exposed_entity_id,
            "assist_exposure_warning": exposure_warning,
            "message": f"已创建自动化: {final_name}",
        }

    def prepare_manage(self, text: str, expected_operation: str, language: str | None = None) -> dict[str, Any]:
        operation = "update" if expected_operation == "task_update" else "delete"
        plan = self._plan_from_text(operation, text, language=language)

        target_name = str(plan.get("target_name") or "").strip()
        summary = str(plan.get("summary") or "").strip() or str(text).strip()

        match = self._match_automation(query_name=target_name, query_content=summary)
        target = match.automation

        target_id = str(target.get("id") or target.get("automation_id") or "").strip()
        target_alias = str(target.get("alias") or target.get("name") or "").strip()
        if not target_id:
            raise AutomationError("matched automation has no id")

        payload = {
            "operation": operation,
            "target_id": target_id,
            "target_alias": target_alias,
            "target_summary": match.summary,
            "plan": plan,
            "input_text": text,
            "language": language or self.client.conversation_language,
        }
        confirmation_id = self.pending.put(payload)

        return {
            "ok": True,
            "needs_confirmation": True,
            "confirmation_id": confirmation_id,
            "operation": operation,
            "match_score": round(match.score, 4),
            "target": {
                "id": target_id,
                "name": target_alias,
                "summary": match.summary,
            },
            "message": (
                f"请确认{operation}操作: 目标是【{target_alias}】, 内容概述: {match.summary}. "
                f"若确认，请再次调用并携带 confirmation_id={confirmation_id}。"
            ),
        }

    def confirm_manage(self, confirmation_id: str) -> dict[str, Any]:
        payload = self.pending.pop(confirmation_id)
        if not payload:
            raise AutomationError("confirmation_id invalid or expired")

        operation = str(payload.get("operation") or "")
        target_id = str(payload.get("target_id") or "")
        target_alias = str(payload.get("target_alias") or "")
        plan_raw = payload.get("plan")
        plan: dict[str, Any] = plan_raw if isinstance(plan_raw, dict) else {}
        input_text = str(payload.get("input_text") or "")
        language = str(payload.get("language") or self.client.conversation_language)

        if operation == "delete":
            self.client.delete_automation(target_id)
            self.client.reload_automations()
            return {
                "ok": True,
                "operation": "delete",
                "automation_id": target_id,
                "name": target_alias,
                "message": f"已删除自动化: {target_alias}",
            }

        if operation != "update":
            raise AutomationError(f"unsupported pending operation: {operation}")

        # Load latest target before applying update.
        all_items = self._managed_automations()
        current = next(
            (
                item
                for item in all_items
                if str(item.get("id") or item.get("automation_id") or "") == target_id
            ),
            None,
        )
        if not current:
            raise AutomationError("target automation not found before update")

        current_alias = str(current.get("alias") or current.get("name") or "").strip()
        current_description = str(current.get("description") or "")
        clean_desc, meta = self._strip_meta(current_description)
        ai_generated_before = bool(meta.get("ai_generated_name", False))

        name_specified = bool(plan.get("name_specified", False))
        requested_name = str(plan.get("new_name") or "").strip()
        summary = str(plan.get("summary") or "").strip() or input_text

        ai_generated_after = ai_generated_before
        if name_specified and requested_name:
            final_name = requested_name
            if ai_generated_before:
                ai_generated_after = False
        elif ai_generated_before:
            final_name = self._generate_name(summary=summary, text=input_text, language=language)
            ai_generated_after = True
        else:
            final_name = current_alias

        trigger_plan = plan.get("trigger") if isinstance(plan.get("trigger"), list) and plan.get("trigger") else []
        condition_plan = plan.get("condition") if isinstance(plan.get("condition"), list) and plan.get("condition") else []
        action_plan = plan.get("action") if isinstance(plan.get("action"), list) and plan.get("action") else []

        current_trigger = self._entry_triggers(current)
        current_condition = self._entry_conditions(current)
        current_action = self._entry_actions(current)

        trigger = self._normalize_trigger_list(trigger_plan if trigger_plan else current_trigger, input_text)
        condition = condition_plan if condition_plan else current_condition
        action = self._normalize_action_list(action_plan if action_plan else current_action, input_text)
        mode = str(plan.get("mode") or "").strip() or str(current.get("mode") or "single")

        update_dry_run_error = self._dry_run_validate_payload(trigger, condition, action, mode)
        if update_dry_run_error is not None:
            raise AutomationError(
                "automation update dry-run failed: "
                f"{update_dry_run_error}. Please provide clearer update instructions."
            )

        updated_meta = {
            "ai_generated_name": ai_generated_after,
            "summary": summary,
            "updated_at": int(time.time()),
        }
        description_base = clean_desc
        description = self._build_description(description_base, updated_meta)

        update_payload = {
            "alias": final_name,
            "description": description,
            "trigger": trigger,
            "condition": condition,
            "action": action,
            "mode": mode,
        }

        self.client.update_automation(target_id, update_payload)
        self.client.reload_automations()

        return {
            "ok": True,
            "operation": "update",
            "automation_id": target_id,
            "old_name": current_alias,
            "name": final_name,
            "ai_generated_name": ai_generated_after,
            "summary": summary,
            "message": f"已更新自动化: {current_alias} -> {final_name}",
        }

    def confirm_latest_manage(self, expected_operation: str) -> dict[str, Any]:
        operation = "update" if expected_operation == "task_update" else "delete"
        latest = self.pending.pop_latest(operation=operation)
        if not latest:
            raise AutomationError("no pending confirmation found")
        token, _ = latest
        return self.confirm_manage(token)
