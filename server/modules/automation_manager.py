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
import random
import re
import shutil
import threading
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

    def __init__(self, message: str, debug: dict[str, Any] | None = None):
        super().__init__(message)
        self.debug = debug or {}


@dataclass
class MatchResult:
    score: float
    automation: dict[str, Any]
    summary: str


class PendingActionStore:
    def __init__(self, ttl_seconds: int = 300):
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, dict[str, Any]] = {}
        self._rng = random.SystemRandom()

    def put(self, payload: dict[str, Any]) -> str:
        token = ""
        for _ in range(50):
            candidate = f"{self._rng.randint(0, 999999):06d}"
            if candidate not in self._items:
                token = candidate
                break
        if not token:
            raise AutomationError("failed to allocate confirmation_id")
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

    def pop_latest(
        self,
        operation: str | None = None,
        session_id: str | None = None,
        resource_type: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        self._cleanup()
        candidates: list[tuple[str, float, dict[str, Any]]] = []
        for key, item in self._items.items():
            payload = dict(item.get("payload") or {})
            if operation and str(payload.get("operation") or "") != operation:
                continue
            if session_id and str(payload.get("session_id") or "") != session_id:
                continue
            if resource_type and str(payload.get("resource_type") or "automation") != resource_type:
                continue
            candidates.append((key, float(item.get("created_at") or 0), payload))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        token = candidates[0][0]
        payload = self._items.pop(token, {}).get("payload") or {}
        return token, dict(payload)

    def count(
        self,
        operation: str | None = None,
        session_id: str | None = None,
        resource_type: str | None = None,
    ) -> int:
        self._cleanup()
        total = 0
        for item in self._items.values():
            payload = dict(item.get("payload") or {})
            if operation and str(payload.get("operation") or "") != operation:
                continue
            if session_id and str(payload.get("session_id") or "") != session_id:
                continue
            if resource_type and str(payload.get("resource_type") or "automation") != resource_type:
                continue
            total += 1
        return total

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
        self.scripts_file = str(ha_cfg.get("scripts_file", "scripts.yaml")).strip() or "scripts.yaml"
        self.scripts_path = os.path.join(self.config_dir, self.scripts_file)
        default_backup_dir = os.path.join(self.config_dir, "backups", "automations")
        self.automation_backup_dir = os.path.abspath(
            str(ha_cfg.get("automation_backup_dir", default_backup_dir)).strip()
        )
        default_script_backup_dir = os.path.join(self.config_dir, "backups", "scripts")
        self.script_backup_dir = os.path.abspath(
            str(ha_cfg.get("script_backup_dir", default_script_backup_dir)).strip()
        )
        self.automation_backup_keep = int(ha_cfg.get("automation_backup_keep", 20))
        self.script_backup_keep = int(ha_cfg.get("script_backup_keep", 20))
        self.automation_plan_retry_max = max(0, int(ha_cfg.get("automation_plan_retry_max", 2)))
        self.automation_auto_expose_default = bool(ha_cfg.get("automation_auto_expose_default", True))
        self.manage_only_exposed_automations = bool(ha_cfg.get("manage_only_exposed_automations", True))
        self.script_plan_retry_max = max(0, int(ha_cfg.get("script_plan_retry_max", 2)))
        self.script_auto_expose_default = bool(ha_cfg.get("script_auto_expose_default", True))
        self.manage_only_exposed_scripts = bool(ha_cfg.get("manage_only_exposed_scripts", True))

        llm_cfg = cfg.get("llm", {}) if cfg else {}
        self.ollama_host = str(llm_cfg.get("host", "http://localhost:11434")).rstrip("/")
        self.ollama_model = str(llm_cfg.get("model", "")).strip()
        self.ollama_timeout = float(llm_cfg.get("timeout", 120))
        self.ollama_system_prompt = str(llm_cfg.get("system_prompt", "")).strip()
        thinking_raw = llm_cfg.get("thinking", False)
        if isinstance(thinking_raw, str):
            self.llm_thinking_enabled = thinking_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            self.llm_thinking_enabled = bool(thinking_raw)
        self.qwen35_reasoning_budget_enabled = bool(llm_cfg.get("qwen35_reasoning_budget_enabled", False))
        self.exposed_entities_prompt_limit = max(0, int(llm_cfg.get("exposed_entities_prompt_limit", 80)))
        logger.info(
            "Automation planner external Ollama configured: host=%s, model=%s, thinking=%s, qwen35_budget=%s, system_prompt_chars=%s",
            self.ollama_host,
            self.ollama_model,
            self.llm_thinking_enabled,
            self.qwen35_reasoning_budget_enabled,
            len(self.ollama_system_prompt),
        )

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

    def reload_scripts(self):
        self.call_service("script", "reload", {})

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

    def list_exposed_entity_summaries(self, limit: int = 80) -> list[dict[str, str]]:
        exposed = self.list_exposed_entities()
        exposed_ids = [
            entity_id
            for entity_id, flags in exposed.items()
            if bool(flags.get("conversation", False))
        ]
        if not exposed_ids:
            return []

        states = self._request_json("GET", "/api/states")
        if not isinstance(states, list):
            return []

        state_index: dict[str, dict[str, Any]] = {}
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            if entity_id:
                state_index[entity_id] = item

        rows: list[dict[str, str]] = []
        for entity_id in sorted(exposed_ids):
            item = state_index.get(entity_id)
            if not isinstance(item, dict):
                continue
            attrs_raw = item.get("attributes")
            attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
            rows.append(
                {
                    "entity_id": entity_id,
                    "name": str(attrs.get("friendly_name") or "").strip(),
                    "domain": str(entity_id.split(".", 1)[0] if "." in entity_id else "").strip(),
                    "state": str(item.get("state") or "").strip(),
                }
            )
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def list_state_entity_ids(self) -> set[str]:
        states = self._request_json("GET", "/api/states")
        if not isinstance(states, list):
            return set()

        result: set[str] = set()
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            if entity_id:
                result.add(entity_id)
        return result

    def list_domain_entities(self, domain: str) -> list[dict[str, str]]:
        dom = str(domain or "").strip().lower()
        if not dom:
            return []

        states = self._request_json("GET", "/api/states")
        if not isinstance(states, list):
            return []

        rows: list[dict[str, str]] = []
        prefix = f"{dom}."
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            if not entity_id.startswith(prefix):
                continue
            attrs_raw = item.get("attributes")
            attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
            friendly = str(attrs.get("friendly_name") or "").strip()
            rows.append({"entity_id": entity_id, "friendly_name": friendly})
        return rows

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

    def _load_file_scripts(self) -> dict[str, dict[str, Any]]:
        try:
            if not os.path.exists(self.scripts_path):
                return {}

            with open(self.scripts_path, "r", encoding="utf-8") as f:
                payload = yaml.safe_load(f)

            if payload is None:
                return {}
            if isinstance(payload, dict):
                result: dict[str, dict[str, Any]] = {}
                for key, value in payload.items():
                    if not isinstance(key, str) or not isinstance(value, dict):
                        continue
                    result[key] = dict(value)
                return result

            raise AutomationError("scripts.yaml format is invalid; expected a YAML object")
        except Exception as exc:
            raise AutomationError(f"failed to load scripts file {self.scripts_path}: {exc}") from exc

    def _save_file_automations(self, automations: list[dict[str, Any]]):
        try:
            os.makedirs(os.path.dirname(self.automations_path), exist_ok=True)
            self._backup_automations_file_if_needed()
            with open(self.automations_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(automations, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            raise AutomationError(f"failed to save automations file {self.automations_path}: {exc}") from exc

    def _save_file_scripts(self, scripts: dict[str, dict[str, Any]]):
        try:
            os.makedirs(os.path.dirname(self.scripts_path), exist_ok=True)
            self._backup_scripts_file_if_needed()
            with open(self.scripts_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(scripts, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            raise AutomationError(f"failed to save scripts file {self.scripts_path}: {exc}") from exc

    def _backup_automations_file_if_needed(self):
        if not os.path.exists(self.automations_path):
            return

        os.makedirs(self.automation_backup_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_name = f"automations.{timestamp}.{uuid.uuid4().hex[:8]}.yaml.bak"
        backup_path = os.path.join(self.automation_backup_dir, backup_name)
        shutil.copy2(self.automations_path, backup_path)
        self._prune_old_backups()

    def _backup_scripts_file_if_needed(self):
        if not os.path.exists(self.scripts_path):
            return

        os.makedirs(self.script_backup_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_name = f"scripts.{timestamp}.{uuid.uuid4().hex[:8]}.yaml.bak"
        backup_path = os.path.join(self.script_backup_dir, backup_name)
        shutil.copy2(self.scripts_path, backup_path)
        self._prune_old_script_backups()

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

    def _prune_old_script_backups(self):
        keep = max(0, int(self.script_backup_keep))
        if keep <= 0:
            return

        try:
            files = [
                os.path.join(self.script_backup_dir, name)
                for name in os.listdir(self.script_backup_dir)
                if name.startswith("scripts.") and name.endswith(".yaml.bak")
            ]
        except FileNotFoundError:
            return

        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old_file in files[keep:]:
            try:
                os.remove(old_file)
            except FileNotFoundError:
                continue

    @staticmethod
    def _latest_backup_file(backup_dir: str, prefix: str) -> str | None:
        try:
            files = [
                os.path.join(backup_dir, name)
                for name in os.listdir(backup_dir)
                if name.startswith(prefix) and name.endswith(".yaml.bak")
            ]
        except FileNotFoundError:
            return None
        if not files:
            return None
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files[0]

    @staticmethod
    def _restore_file_from_backup(target_path: str, backup_path: str) -> None:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        shutil.copy2(backup_path, target_path)

    def _restore_latest_automation_backup(self) -> str | None:
        latest = self._latest_backup_file(self.automation_backup_dir, "automations.")
        if not latest:
            return None
        self._restore_file_from_backup(self.automations_path, latest)
        return latest

    def _restore_latest_script_backup(self) -> str | None:
        latest = self._latest_backup_file(self.script_backup_dir, "scripts.")
        if not latest:
            return None
        self._restore_file_from_backup(self.scripts_path, latest)
        return latest

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

    def list_scripts(self) -> list[dict[str, Any]]:
        scripts = self._load_file_scripts()
        rows: list[dict[str, Any]] = []
        for script_id, payload in scripts.items():
            row = dict(payload)
            row["script_id"] = script_id
            rows.append(row)
        return rows

    def create_script(self, script_data: dict[str, Any]) -> dict[str, Any]:
        scripts = self._load_file_scripts()
        alias = str(script_data.get("alias") or "").strip()
        requested_id = str(script_data.get("script_id") or "").strip()
        base_id = self._slugify_name(requested_id or alias)
        if not base_id:
            base_id = f"hv_script_{int(time.time())}"

        script_id = base_id
        suffix = 2
        while script_id in scripts:
            script_id = f"{base_id}_{suffix}"
            suffix += 1

        sequence = script_data.get("sequence")
        if not isinstance(sequence, list):
            sequence = []

        new_item = {
            "alias": alias or script_id,
            "description": str(script_data.get("description") or "").strip(),
            "sequence": sequence,
            "mode": str(script_data.get("mode") or "single").strip() or "single",
        }
        scripts[script_id] = new_item
        self._save_file_scripts(scripts)

        result = dict(new_item)
        result["script_id"] = script_id
        return result

    def update_script(self, script_id: str, script_data: dict[str, Any]) -> dict[str, Any]:
        scripts = self._load_file_scripts()
        if script_id not in scripts:
            raise AutomationError(f"failed to update script {script_id}: not found in {self.scripts_path}")

        current = dict(scripts.get(script_id) or {})
        updated = dict(current)
        updated["alias"] = str(script_data.get("alias") or updated.get("alias") or script_id).strip()
        updated["description"] = str(script_data.get("description") or updated.get("description") or "").strip()

        sequence = script_data.get("sequence")
        if isinstance(sequence, list):
            updated["sequence"] = sequence

        updated["mode"] = str(script_data.get("mode") or updated.get("mode") or "single").strip() or "single"
        scripts[script_id] = updated
        self._save_file_scripts(scripts)

        result = dict(updated)
        result["script_id"] = script_id
        return result

    def delete_script(self, script_id: str):
        scripts = self._load_file_scripts()
        if script_id not in scripts:
            raise AutomationError(f"failed to delete script {script_id}: not found in {self.scripts_path}")
        scripts.pop(script_id, None)
        self._save_file_scripts(scripts)

    def resolve_script_entity_id(self, script_id: str, alias: str) -> str | None:
        states = self._request_json("GET", "/api/states")
        if not isinstance(states, list):
            return None

        candidates = [s for s in states if isinstance(s, dict) and str(s.get("entity_id") or "").startswith("script.")]
        if not candidates:
            return None

        script_id_clean = str(script_id or "").strip()
        if script_id_clean:
            direct = f"script.{script_id_clean}"
            for item in candidates:
                entity_id = str(item.get("entity_id") or "").strip()
                if entity_id == direct:
                    return entity_id

        target_alias = str(alias or "").strip()
        if target_alias:
            for item in candidates:
                attrs_raw = item.get("attributes")
                attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
                if str(attrs.get("friendly_name") or "").strip() == target_alias:
                    return str(item.get("entity_id") or "").strip() or None

        return None

    def _post_ollama_chat(
        self,
        messages: list[dict[str, str]],
        think: str | bool | None,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.ollama_model:
            raise AutomationError("llm.model is empty; cannot call external Ollama for planning")

        payload: dict[str, Any] = {
            "model": self.ollama_model,
            "stream": False,
            "messages": messages,
            "options": options,
        }
        if think is not None:
            payload["think"] = think

        try:
            resp = requests.post(
                f"{self.ollama_host}/api/chat",
                json=payload,
                timeout=self.ollama_timeout,
            )
        except requests.RequestException as exc:
            raise AutomationError(f"request to external Ollama failed: {exc}") from exc

        if resp.status_code >= 400:
            raise AutomationError(f"external Ollama API error {resp.status_code}: {resp.text}")

        try:
            result = resp.json()
        except ValueError as exc:
            raise AutomationError(f"external Ollama returned non-JSON response: {resp.text}") from exc

        if not isinstance(result, dict):
            raise AutomationError("external Ollama returned invalid response object")
        return result

    @staticmethod
    def _extract_ollama_message_content(payload: dict[str, Any]) -> str:
        message = payload.get("message")
        if not isinstance(message, dict):
            return ""
        return str(message.get("content") or "").strip()

    @staticmethod
    def _extract_ollama_thinking(payload: dict[str, Any]) -> str:
        message = payload.get("message")
        if not isinstance(message, dict):
            return ""
        return str(message.get("thinking") or "").strip()

    def ask_conversation(
        self,
        prompt: str,
        language: str | None = None,
        include_exposed_entities: bool = False,
    ) -> str:
        lang = language or self.conversation_language
        prompt_text = str(prompt or "").strip()

        if include_exposed_entities:
            try:
                exposed_rows = self.list_exposed_entity_summaries(limit=self.exposed_entities_prompt_limit)
            except Exception as exc:
                logger.warning("Failed to load exposed entities for planner context: %s", exc)
                exposed_rows = []
            if exposed_rows:
                prompt_text += (
                    "\n\nOnly these entities are exposed to Assist. Prefer these entities in plans. "
                    f"Use exact entity_id values when possible: {json.dumps(exposed_rows, ensure_ascii=False)}"
                )

        default_planner_system_prompt = (
            "You are an automation-planning agent for Home Assistant. "
            "Return exactly one valid JSON object. No markdown. No explanation. "
            "No tool calls. Never execute actions or intents."
        )
        effective_system_prompt = self.ollama_system_prompt or default_planner_system_prompt

        messages: list[dict[str, str]] = []
        messages.append({"role": "system", "content": effective_system_prompt})
        messages.append({"role": "user", "content": prompt_text})

        model_lower = self.ollama_model.lower()
        use_qwen35_budget = (
            self.llm_thinking_enabled
            and self.qwen35_reasoning_budget_enabled
            and "qwen3.5" in model_lower
        )

        if use_qwen35_budget:
            reason_payload = self._post_ollama_chat(
                messages=messages,
                think="medium",
                options={
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "top_k": 20,
                    "presence_penalty": 1.5,
                    "num_predict": 512,
                },
            )
            done_reason = str(reason_payload.get("done_reason") or "").strip().lower()
            if done_reason == "stop":
                return self._extract_ollama_message_content(reason_payload)

            thinking = self._extract_ollama_thinking(reason_payload)
            final_prompt = (
                "Review the reasoning above and provide the best final answer now. "
                "Follow all previous instructions exactly, and return only the answer content "
                "without any prefix or explanation."
            )
            direct_messages = list(messages)
            if thinking:
                direct_messages.append({"role": "assistant", "content": f"<think>\n{thinking}\n</think>"})
            direct_messages.append({"role": "user", "content": final_prompt})
            direct_payload = self._post_ollama_chat(
                messages=direct_messages,
                think=False,
                options={
                    "temperature": 0.0,
                    "top_p": 0.8,
                    "top_k": 20,
                    "presence_penalty": 1.1,
                },
            )
            direct_content = self._extract_ollama_message_content(direct_payload)

            # Guard against common wording artifacts from some qwen3.5 responses.
            artifact_prefixes = [
                "the first conclusion reached was",
                "first conclusion reached was",
                "the conclusion reached was",
            ]
            lowered = direct_content.lower()
            for prefix in artifact_prefixes:
                if lowered.startswith(prefix):
                    cleaned = direct_content[len(prefix):].lstrip(" :,-\t\n\r\"'“”")
                    if cleaned:
                        return cleaned
                    break

            return direct_content

        if self.llm_thinking_enabled:
            payload = self._post_ollama_chat(
                messages=messages,
                think="medium",
                options={
                    "temperature": 0.0,
                    "top_p": 0.9,
                    "top_k": 40,
                    "num_predict": 512,
                },
            )
            return self._extract_ollama_message_content(payload)

        payload = self._post_ollama_chat(
            messages=messages,
            think=False,
            options={
                "temperature": 0.0,
                "top_p": 0.9,
                "top_k": 40,
                "num_predict": 512,
            },
        )
        return self._extract_ollama_message_content(payload)


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
        self.pending = PendingActionStore(ttl_seconds=24 * 60 * 60)
        self._confirm_queue = threading.Condition()
        self._next_confirm_ticket = 0
        self._serving_confirm_ticket = 0

    def _run_confirm_serially(self, task: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Serialize confirm execution to preserve FIFO write/reload order."""
        with self._confirm_queue:
            ticket = self._next_confirm_ticket
            self._next_confirm_ticket += 1
            while ticket != self._serving_confirm_ticket:
                self._confirm_queue.wait()

        try:
            return task()
        finally:
            with self._confirm_queue:
                self._serving_confirm_ticket += 1
                self._confirm_queue.notify_all()

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
    def _preview_text(text: str, limit: int = 1200) -> str:
        raw = str(text or "")
        if len(raw) <= limit:
            return raw
        return f"{raw[:limit]}...(truncated, total={len(raw)})"

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
    def _entry_sequence(entry: dict[str, Any]) -> list[dict[str, Any]]:
        value = entry.get("sequence")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []

    @staticmethod
    def _json_like_equal(left: Any, right: Any) -> bool:
        try:
            return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(
                right,
                ensure_ascii=False,
                sort_keys=True,
            )
        except TypeError:
            return left == right

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

    @staticmethod
    def _collect_entity_ids_from_value(value: Any) -> set[str]:
        refs: set[str] = set()
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return refs
            # Skip dynamic templates that cannot be validated statically.
            if "{{" in raw or "{%" in raw:
                return refs
            if re.match(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$", raw):
                refs.add(raw)
            return refs

        if isinstance(value, list):
            for item in value:
                refs.update(AutomationManager._collect_entity_ids_from_value(item))
            return refs

        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) == "entity_id":
                    refs.update(AutomationManager._collect_entity_ids_from_value(item))
                elif isinstance(item, (dict, list)):
                    refs.update(AutomationManager._collect_entity_ids_from_value(item))
            return refs

        return refs

    def _validate_entities_exposed(
        self,
        triggers: list[dict[str, Any]],
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> str | None:
        exposed_map = self.client.list_exposed_entities()
        exposed_entities = {
            entity_id
            for entity_id, flags in exposed_map.items()
            if isinstance(flags, dict) and bool(flags.get("conversation", False))
        }
        known_entities = self.client.list_state_entity_ids()

        referenced: set[str] = set()
        for block in (triggers, conditions, actions):
            referenced.update(self._collect_entity_ids_from_value(block))

        for act in actions:
            if not isinstance(act, dict):
                continue
            service = str(act.get("action") or "").strip()
            # Only treat script.<name> as script entity shorthand when it is not
            # one of script domain built-in services.
            if re.match(r"^script\.[a-zA-Z0-9_]+$", service):
                script_name = service.split(".", 1)[1]
                if script_name in {"turn_on", "turn_off", "toggle", "reload"}:
                    continue
                referenced.add(service)

        # Ignore special shorthand values that are not concrete entities.
        referenced = {entity for entity in referenced if entity not in {"all", "none"}}
        if not referenced:
            return None

        # Generic anti-misclassification: only enforce exposure policy on IDs
        # that are actual HA entities in current states.
        enforce_refs = sorted(entity for entity in referenced if entity in known_entities)
        if not enforce_refs:
            return None

        not_exposed = sorted(entity for entity in enforce_refs if entity not in exposed_entities)
        if not_exposed:
            return (
                "referenced entities are not exposed to Assist: "
                + ", ".join(not_exposed)
            )
        return None

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
            if trigger_type in {"state", "numeric_state"}:
                refs = self._collect_entity_ids_from_value(trig)
                if not refs:
                    return f"trigger[{idx}] requires entity_id for {trigger_type}"

        for idx, cond in enumerate(conditions):
            if not isinstance(cond, dict):
                return f"condition[{idx}] is not an object"
            condition_type = str(cond.get("condition") or "").strip().lower()
            if not condition_type:
                return f"condition[{idx}] missing required key: condition"
            if condition_type in {"state", "numeric_state"}:
                refs = self._collect_entity_ids_from_value(cond)
                if not refs:
                    return f"condition[{idx}] requires entity_id for {condition_type}"

        for idx, act in enumerate(actions):
            if not isinstance(act, dict):
                return f"action[{idx}] is not an object"
            action_service = str(act.get("action") or "").strip()
            if not action_service:
                return f"action[{idx}] missing required key: action"
            if "." not in action_service:
                return f"action[{idx}] should be domain.service format"
            if action_service == "scene.turn_on":
                target_entity: Any = None
                target_raw = act.get("target")
                if isinstance(target_raw, dict):
                    target_entity = target_raw.get("entity_id")
                if target_entity is None:
                    data_raw = act.get("data")
                    if isinstance(data_raw, dict):
                        target_entity = data_raw.get("entity_id")

                cleaned_target = self._sanitize_entity_id_field(target_entity)
                has_scene_target = False
                if isinstance(cleaned_target, str):
                    has_scene_target = cleaned_target.startswith("scene.")
                elif isinstance(cleaned_target, list):
                    has_scene_target = any(str(v).startswith("scene.") for v in cleaned_target)

                if not has_scene_target:
                    return f"action[{idx}] scene.turn_on requires target.entity_id (scene.*)"

        known_entities = self.client.list_state_entity_ids()
        referenced = self._collect_entity_ids_from_value([triggers, conditions, actions])
        referenced = {entity for entity in referenced if entity not in {"all", "none"}}
        unknown = sorted(entity for entity in referenced if entity not in known_entities)
        if unknown:
            return "referenced entities not found in Home Assistant states: " + ", ".join(unknown)

        exposure_error = self._validate_entities_exposed(triggers, conditions, actions)
        if exposure_error:
            return exposure_error

        ha_validation_error = self.client.validate_automation_config(triggers, conditions, actions)
        if ha_validation_error:
            return f"ha validate_config failed: {ha_validation_error}"

        return None

    def _dry_run_validate_script_payload(
        self,
        sequence: list[dict[str, Any]],
        mode: str,
    ) -> str | None:
        if not sequence:
            return "missing sequence"

        allowed_modes = {"single", "restart", "queued", "parallel"}
        if mode not in allowed_modes:
            return f"invalid mode: {mode}"

        for idx, step in enumerate(sequence):
            if not isinstance(step, dict):
                return f"sequence[{idx}] is not an object"
            action_service = str(step.get("action") or "").strip()
            if not action_service:
                return f"sequence[{idx}] missing required key: action"
            if "." not in action_service:
                return f"sequence[{idx}] should be domain.service format"

        known_entities = self.client.list_state_entity_ids()
        referenced = self._collect_entity_ids_from_value([sequence])
        referenced = {entity for entity in referenced if entity not in {"all", "none"}}
        unknown = sorted(entity for entity in referenced if entity not in known_entities)
        if unknown:
            return "referenced entities not found in Home Assistant states: " + ", ".join(unknown)

        exposure_error = self._validate_entities_exposed([], [], sequence)
        if exposure_error:
            return exposure_error

        return None

    def _normalize_trigger_list(self, trigger_list: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in trigger_list:
            if not isinstance(item, dict):
                continue

            raw_trigger = item.get("trigger")
            raw_platform = item.get("platform")
            raw_event = item.get("event")

            trigger_type = ""
            if isinstance(raw_trigger, str):
                trigger_type = raw_trigger.strip().lower()
            elif isinstance(raw_platform, str):
                trigger_type = raw_platform.strip().lower()
            elif isinstance(raw_event, str):
                trigger_type = raw_event.strip().lower()

            # Accept shorthand like trigger="time:09:00" from LLM outputs.
            if trigger_type.startswith("time:"):
                at_short = self._normalize_time_string(trigger_type.split(":", 1)[1].strip())
                if at_short:
                    normalized.append({"trigger": "time", "at": at_short})
                    continue

            if trigger_type == "time":
                at = self._normalize_time_string(str(item.get("at") or item.get("from") or "").strip())
                if at:
                    normalized.append({"trigger": "time", "at": at})
                continue

            # Keep valid non-time trigger entries, but normalize key style and
            # drop legacy/invalid platform field to avoid schema/type errors.
            if trigger_type:
                normalized_item = dict(item)
                normalized_item["trigger"] = trigger_type
                normalized_item.pop("platform", None)
                normalized.append(normalized_item)

        if normalized:
            return normalized
        return self._extract_time_trigger_from_text(text)

    def _infer_scene_entity_from_text(self, text: str) -> str | None:
        raw_text = str(text or "").strip()
        if not raw_text:
            return None

        scenes = self.client.list_domain_entities("scene")
        if not scenes:
            return None

        query = self._normalize_text(raw_text)
        if not query:
            return None

        best_entity = ""
        best_score = 0.0
        for row in scenes:
            entity_id = str(row.get("entity_id") or "").strip()
            friendly = str(row.get("friendly_name") or "").strip()
            if not entity_id:
                continue

            entity_tail = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
            candidate = self._normalize_text(" ".join([entity_tail, friendly, entity_id]))
            if not candidate:
                continue

            score = max(
                self._sequence_score(query, candidate),
                self._token_overlap_score(query, candidate),
            )
            if score > best_score:
                best_score = score
                best_entity = entity_id

        if best_entity and best_score >= 0.22:
            return best_entity
        return None

    @staticmethod
    def _is_entity_id_like(value: str) -> bool:
        return bool(re.match(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$", value))

    @staticmethod
    def _is_service_name_like(value: str) -> bool:
        parts = value.split(".", 1)
        if len(parts) != 2:
            return False
        service_name = parts[1]
        common_services = {
            "turn_on",
            "turn_off",
            "toggle",
            "reload",
            "set_value",
            "set_temperature",
            "open_cover",
            "close_cover",
            "stop_cover",
            "activate",
            "play",
            "pause",
            "stop",
        }
        return service_name in common_services

    def _sanitize_entity_id_field(self, value: Any) -> str | list[str] | None:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if "{{" in raw or "{%" in raw:
                return raw
            if self._is_service_name_like(raw):
                return None
            if self._is_entity_id_like(raw):
                return raw
            m = re.search(r"([a-zA-Z0-9_]+\.[a-zA-Z0-9_]+)", raw)
            if not m:
                return None
            candidate = m.group(1)
            if self._is_service_name_like(candidate):
                return None
            return candidate

        if isinstance(value, list):
            sanitized: list[str] = []
            for item in value:
                cleaned = self._sanitize_entity_id_field(item)
                if isinstance(cleaned, str) and cleaned:
                    sanitized.append(cleaned)
            return sanitized or None

        return None

    def _sanitize_entity_ids_recursive(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, sub in value.items():
                key_str = str(key)
                if key_str == "entity_id":
                    cleaned = self._sanitize_entity_id_field(sub)
                    if cleaned is not None:
                        sanitized[key_str] = cleaned
                    continue

                cleaned_sub = self._sanitize_entity_ids_recursive(sub)
                if isinstance(cleaned_sub, dict) and not cleaned_sub:
                    continue
                if isinstance(cleaned_sub, list) and not cleaned_sub:
                    continue
                sanitized[key_str] = cleaned_sub
            return sanitized

        if isinstance(value, list):
            sanitized_list: list[Any] = []
            for item in value:
                cleaned_item = self._sanitize_entity_ids_recursive(item)
                if isinstance(cleaned_item, dict) and not cleaned_item:
                    continue
                if isinstance(cleaned_item, list) and not cleaned_item:
                    continue
                sanitized_list.append(cleaned_item)
            return sanitized_list

        return value

    def _normalize_condition_list(self, condition_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in condition_list:
            if not isinstance(item, dict):
                continue

            condition_type = str(item.get("condition") or item.get("type") or "").strip().lower()
            if not condition_type:
                continue

            normalized_item = dict(item)
            normalized_item["condition"] = condition_type
            normalized_item.pop("type", None)
            normalized_item = self._sanitize_entity_ids_recursive(normalized_item)
            if isinstance(normalized_item, dict) and normalized_item:
                normalized.append(normalized_item)

        return normalized

    def _normalize_action_list(self, action_list: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        inferred_scene_entity_id: str | None = None

        def ensure_inferred_scene_id() -> str | None:
            nonlocal inferred_scene_entity_id
            if inferred_scene_entity_id is not None:
                return inferred_scene_entity_id or None
            inferred_scene_entity_id = self._infer_scene_entity_from_text(text) or ""
            return inferred_scene_entity_id or None

        for item in action_list:
            if not isinstance(item, dict):
                continue
            service = str(item.get("action") or item.get("service") or "").strip()
            if not service and str(item.get("type") or "").strip().lower() == "call_script":
                service = str(item.get("script") or item.get("entity_id") or "").strip()
            if not service and item.get("script"):
                service = str(item.get("script") or "").strip()

            # Keep valid domain.service actions as-is; only coerce script shorthand.
            if service.startswith("scripts."):
                service = "script." + service.split(".", 1)[1]
            elif "." not in service:
                service = self._coerce_script_service(service) or service

            # Repair malformed script shorthand (e.g. "script", "script.", "script.script").
            if service in {"script", "script.", "script.script"}:
                target_entity = ""
                target_raw = item.get("target")
                if isinstance(target_raw, dict):
                    target_entity_raw = target_raw.get("entity_id")
                    if isinstance(target_entity_raw, str):
                        target_entity = target_entity_raw.strip()
                    elif isinstance(target_entity_raw, list):
                        first = next((x for x in target_entity_raw if isinstance(x, str) and x.strip()), "")
                        target_entity = str(first).strip()
                if not target_entity and isinstance(item.get("entity_id"), str):
                    target_entity = str(item.get("entity_id") or "").strip()

                if target_entity.startswith("script.") and len(target_entity) > len("script."):
                    service = target_entity
                else:
                    fallback = self._extract_script_action_from_text(text)
                    if fallback:
                        service = str(fallback[0].get("action") or service).strip()

            if service:
                script_entity_target = ""
                if re.match(r"^script\.[a-zA-Z0-9_]+$", service):
                    script_name = service.split(".", 1)[1]
                    if script_name not in {"turn_on", "turn_off", "toggle", "reload"}:
                        # Canonicalize script entity shorthand to script.turn_on + target.entity_id.
                        script_entity_target = service
                        service = "script.turn_on"

                normalized_item = dict(item)
                normalized_item.pop("service", None)
                normalized_item["action"] = service

                # Convert flattened dotted key style to nested target object.
                if "target.entity_id" in normalized_item:
                    flat_entity = normalized_item.pop("target.entity_id")
                    target_obj = normalized_item.get("target")
                    if not isinstance(target_obj, dict):
                        target_obj = {}
                    target_obj["entity_id"] = flat_entity
                    normalized_item["target"] = target_obj

                # Accept common alternate payload keys from LLM output.
                if "service_data" in normalized_item and "data" not in normalized_item:
                    normalized_item["data"] = normalized_item.get("service_data")
                normalized_item.pop("service_data", None)

                # Convert entity_id shorthand into target when target is missing.
                if "target" not in normalized_item and "entity_id" in normalized_item:
                    normalized_item["target"] = {"entity_id": normalized_item.get("entity_id")}
                    normalized_item.pop("entity_id", None)

                # Accept LLM shorthand target_id and map it to target.entity_id.
                # HA validate_config rejects unknown keys like target_id in action objects.
                if "target_id" in normalized_item:
                    target_id_value = normalized_item.pop("target_id")
                    target_obj = normalized_item.get("target")
                    if not isinstance(target_obj, dict):
                        target_obj = {}
                    if "entity_id" not in target_obj:
                        target_obj["entity_id"] = target_id_value
                    normalized_item["target"] = target_obj

                target_raw = normalized_item.get("target")
                if isinstance(target_raw, dict):
                    target = dict(target_raw)
                    if "entity_id" in target:
                        cleaned_entity = self._sanitize_entity_id_field(target.get("entity_id"))
                        if cleaned_entity is None:
                            target.pop("entity_id", None)
                        else:
                            target["entity_id"] = cleaned_entity

                    if service == "scene.turn_on" and "entity_id" not in target:
                        data_raw = normalized_item.get("data")
                        data_entity: Any = None
                        if isinstance(data_raw, dict):
                            data_entity = data_raw.get("entity_id")
                        cleaned_data_entity = self._sanitize_entity_id_field(data_entity)
                        if isinstance(cleaned_data_entity, str) and cleaned_data_entity.startswith("scene."):
                            target["entity_id"] = cleaned_data_entity
                        elif isinstance(cleaned_data_entity, list):
                            first_scene = next(
                                (v for v in cleaned_data_entity if isinstance(v, str) and v.startswith("scene.")),
                                "",
                            )
                            if first_scene:
                                target["entity_id"] = first_scene
                        else:
                            guessed_scene = ensure_inferred_scene_id()
                            if guessed_scene:
                                target["entity_id"] = guessed_scene

                    if target:
                        normalized_item["target"] = target
                    else:
                        normalized_item.pop("target", None)

                if service == "scene.turn_on" and "target" not in normalized_item:
                    data_raw = normalized_item.get("data")
                    data_entity: Any = None
                    if isinstance(data_raw, dict):
                        data_entity = data_raw.get("entity_id")
                    cleaned_data_entity = self._sanitize_entity_id_field(data_entity)
                    if isinstance(cleaned_data_entity, str) and cleaned_data_entity.startswith("scene."):
                        normalized_item["target"] = {"entity_id": cleaned_data_entity}
                    elif isinstance(cleaned_data_entity, list):
                        first_scene = next(
                            (v for v in cleaned_data_entity if isinstance(v, str) and v.startswith("scene.")),
                            "",
                        )
                        if first_scene:
                            normalized_item["target"] = {"entity_id": first_scene}
                    else:
                        guessed_scene = ensure_inferred_scene_id()
                        if guessed_scene:
                            normalized_item["target"] = {"entity_id": guessed_scene}

                if script_entity_target:
                    target = normalized_item.get("target")
                    if not isinstance(target, dict):
                        target = {}
                    target_entity = target.get("entity_id")
                    cleaned_target_entity = self._sanitize_entity_id_field(target_entity)
                    if cleaned_target_entity is None:
                        target["entity_id"] = script_entity_target
                    else:
                        target["entity_id"] = cleaned_target_entity
                    normalized_item["target"] = target

                normalized_item = self._sanitize_entity_ids_recursive(normalized_item)
                if not isinstance(normalized_item, dict) or not normalized_item:
                    continue

                normalized.append(normalized_item)

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

    def _script_content_text(self, script: dict[str, Any]) -> str:
        script_id = str(script.get("script_id") or "")
        alias = str(script.get("alias") or script_id)
        description = str(script.get("description") or "")
        clean_desc, meta = self._strip_meta(description)
        summary = str(meta.get("summary") or "")
        sequence = json.dumps(self._entry_sequence(script), ensure_ascii=False)
        mode = str(script.get("mode") or "")
        return " ".join([script_id, alias, clean_desc, summary, sequence, mode]).strip()

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

    def _managed_scripts(self) -> list[dict[str, Any]]:
        scripts = self.client.list_scripts()
        if not scripts:
            return []

        if not self.client.manage_only_exposed_scripts:
            return scripts

        exposed_map = self.client.list_exposed_entities()
        if not exposed_map:
            return []

        managed: list[dict[str, Any]] = []
        for item in scripts:
            script_id = str(item.get("script_id") or "").strip()
            alias = str(item.get("alias") or script_id).strip()
            entity_id = self.client.resolve_script_entity_id(script_id=script_id, alias=alias)
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

    def _summarize_script(self, script: dict[str, Any]) -> str:
        description = str(script.get("description") or "")
        clean_desc, meta = self._strip_meta(description)
        summary = str(meta.get("summary") or "").strip()
        if summary:
            return summary

        sequence = self._entry_sequence(script)
        sequence_brief = json.dumps(sequence[:1], ensure_ascii=False)
        generated = f"步骤: {sequence_brief}"
        if clean_desc:
            return f"{clean_desc} | {generated}"
        return generated

    def _match_script(self, query_name: str, query_content: str) -> MatchResult:
        scripts = self._managed_scripts()
        if not scripts:
            if self.client.manage_only_exposed_scripts:
                raise AutomationError("no script exposed to Assist is available for management")
            raise AutomationError("no script found in Home Assistant")

        qn = self._normalize_text(query_name)
        qc = self._normalize_text(query_content)

        best: MatchResult | None = None
        for item in scripts:
            script_id = str(item.get("script_id") or "")
            alias = str(item.get("alias") or script_id)
            name_norm = self._normalize_text(" ".join([alias, script_id]))
            content_norm = self._normalize_text(self._script_content_text(item))

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

            summary = self._summarize_script(item)
            current = MatchResult(score=combined, automation=item, summary=summary)
            if best is None or current.score > best.score:
                best = current

        if best is None:
            raise AutomationError("failed to match script")
        return best

    def _native_match_script(
        self,
        user_text: str,
        query_name: str,
        query_content: str,
        language: str | None = None,
    ) -> MatchResult | None:
        candidates = self._managed_scripts()
        if not candidates:
            return None

        candidate_rows: list[dict[str, str]] = []
        for item in candidates[:120]:
            candidate_rows.append(
                {
                    "id": str(item.get("script_id") or "").strip(),
                    "name": str(item.get("alias") or item.get("script_id") or "").strip(),
                    "summary": self._summarize_script(item),
                }
            )

        request_payload = {
            "task": "native_match_script",
            "user_text": user_text,
            "query_name": query_name,
            "query_content": query_content,
            "candidates": candidate_rows,
        }

        try:
            raw = self.client.ask_conversation(json.dumps(request_payload, ensure_ascii=False), language=language)
        except Exception:
            return None

        parsed = self._extract_json_block(raw)
        if not isinstance(parsed, dict):
            return None

        target_id = str(parsed.get("target_id") or "").strip()
        confidence_raw = parsed.get("confidence")
        try:
            confidence = float(str(confidence_raw))
        except (TypeError, ValueError):
            confidence = 0.0

        if not target_id or confidence < 0.45:
            return None

        chosen = next(
            (
                item
                for item in candidates
                if str(item.get("script_id") or "").strip() == target_id
            ),
            None,
        )
        if not chosen:
            return None

        return MatchResult(
            score=max(0.0, min(1.0, confidence)),
            automation=chosen,
            summary=self._summarize_script(chosen),
        )

    def _native_match_automation(
        self,
        user_text: str,
        query_name: str,
        query_content: str,
        language: str | None = None,
    ) -> MatchResult | None:
        candidates = self._managed_automations()
        if not candidates:
            return None

        candidate_rows: list[dict[str, str]] = []
        for item in candidates[:120]:
            candidate_rows.append(
                {
                    "id": str(item.get("id") or item.get("automation_id") or "").strip(),
                    "name": str(item.get("alias") or item.get("name") or "").strip(),
                    "summary": self._summarize_automation(item),
                }
            )

        request_payload = {
            "task": "native_match_automation",
            "user_text": user_text,
            "query_name": query_name,
            "query_content": query_content,
            "candidates": candidate_rows,
        }

        try:
            raw = self.client.ask_conversation(json.dumps(request_payload, ensure_ascii=False), language=language)
        except Exception:
            return None

        parsed = self._extract_json_block(raw)
        if not isinstance(parsed, dict):
            return None

        target_id = str(parsed.get("target_id") or "").strip()
        confidence_raw = parsed.get("confidence")
        try:
            confidence = float(str(confidence_raw))
        except (TypeError, ValueError):
            confidence = 0.0

        if not target_id or confidence < 0.45:
            return None

        chosen = next(
            (
                item
                for item in candidates
                if str(item.get("id") or item.get("automation_id") or "").strip() == target_id
            ),
            None,
        )
        if not chosen:
            return None

        return MatchResult(
            score=max(0.0, min(1.0, confidence)),
            automation=chosen,
            summary=self._summarize_automation(chosen),
        )

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
                "action 列表每项必须有 action 键并使用 domain.service 格式；"
                "可操作任意已暴露给语音助手的实体，优先在 action 中提供 target.entity_id（或 entity_id）和 data；"
                "不要把 domain.service（例如 scene.turn_on）写入 entity_id 字段。"
            )
        raw = self.client.ask_conversation(
            prompt,
            language=language,
            include_exposed_entities=True,
        )
        logger.info(
            "conversation planning raw response (automation, op=%s): %s",
            operation,
            self._preview_text(raw),
        )
        parsed = self._extract_json_block(raw) or {}
        logger.info(
            "conversation planning parsed JSON (automation, op=%s): %s",
            operation,
            parsed if parsed else "<empty-or-invalid-json>",
        )

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

    def _plan_script_from_text(
        self,
        operation: str,
        text: str,
        language: str | None = None,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        prompt = (
            "你是 Home Assistant 脚本规划器。"
            "请将用户需求转换成 JSON。"
            "仅输出 JSON，不要输出其他文字。"
            "JSON schema: "
            "{"
            '"target_name":"string or empty",'
            '"new_name":"string or empty",'
            '"name_specified":true|false,'
            '"summary":"string",'
            '"expose_to_assist":true|false|empty,'
            '"sequence":[],"mode":"single|restart|queued|parallel|empty"'
            "}."
            f"operation={operation}; user_text={text}"
        )
        if retry_reason:
            prompt += (
                " 上一次输出未通过校验，原因是: "
                f"{retry_reason}. "
                "请返回可直接用于 Home Assistant script YAML 的结果："
                "sequence 列表每项必须有 action 键并使用 domain.service 格式；"
                "优先在步骤中提供 target.entity_id（或 entity_id）和 data；"
                "不要把 domain.service（例如 light.turn_on）写入 entity_id 字段。"
            )
        raw = self.client.ask_conversation(
            prompt,
            language=language,
            include_exposed_entities=True,
        )
        logger.info(
            "conversation planning raw response (script, op=%s): %s",
            operation,
            self._preview_text(raw),
        )
        parsed = self._extract_json_block(raw) or {}
        logger.info(
            "conversation planning parsed JSON (script, op=%s): %s",
            operation,
            parsed if parsed else "<empty-or-invalid-json>",
        )

        if not parsed:
            if self.client.uses_default_conversation_agent:
                raise AutomationError(
                    "default conversation agent cannot produce structured planning JSON for this request. "
                    "Please configure home_assistant.automation_conversation_agent to an LLM-capable agent."
                )
            raise AutomationError(
                "conversation agent did not return structured JSON for script planning. "
                "Please configure home_assistant.automation_conversation_agent to an LLM-capable agent."
            )

        result = {
            "target_name": str(parsed.get("target_name") or "").strip(),
            "new_name": str(parsed.get("new_name") or parsed.get("target_name") or "").strip(),
            "name_specified": bool(parsed.get("name_specified", False)),
            "summary": str(parsed.get("summary") or "").strip(),
            "expose_to_assist": parsed.get("expose_to_assist"),
            "sequence": parsed.get("sequence") if isinstance(parsed.get("sequence"), list) else [],
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

    def _resolve_script_expose_preference(self, text: str, plan: dict[str, Any]) -> bool:
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
        return self.client.script_auto_expose_default

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
            condition_raw: list[dict[str, Any]] = [item for item in condition_any if isinstance(item, dict)]
            action_raw: list[dict[str, Any]] = [item for item in action_raw_any if isinstance(item, dict)]

            trigger = self._normalize_trigger_list(trigger_raw, text)
            condition = self._normalize_condition_list(condition_raw)
            action = self._normalize_action_list(action_raw, text)

            retry_reason = self._dry_run_validate_payload(trigger, condition, action, mode)
            if retry_reason is None:
                break

            if attempt >= max_attempts - 1:
                raise AutomationError(
                    "automation dry-run failed after retries: "
                    f"{retry_reason}. Please provide a clearer command or adjust the conversation agent prompt.",
                    debug={
                        "phase": "create_dry_run",
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "retry_reason": retry_reason,
                        "plan": plan,
                        "normalized": {
                            "trigger": trigger,
                            "condition": condition,
                            "action": action,
                            "mode": mode,
                        },
                    },
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

    def create_script_from_text(self, text: str, language: str | None = None) -> dict[str, Any]:
        max_attempts = self.client.script_plan_retry_max + 1
        retry_reason: str | None = None
        plan: dict[str, Any] = {}

        final_name = ""
        ai_generated_name = False
        summary = ""
        mode = "single"
        sequence: list[dict[str, Any]] = []

        for attempt in range(max_attempts):
            plan = self._plan_script_from_text("create", text, language=language, retry_reason=retry_reason)
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
            sequence_raw_any = plan.get("sequence")
            if not isinstance(sequence_raw_any, list):
                sequence_raw_any = []
            sequence_raw: list[dict[str, Any]] = [item for item in sequence_raw_any if isinstance(item, dict)]
            sequence = self._normalize_action_list(sequence_raw, text)

            retry_reason = self._dry_run_validate_script_payload(sequence, mode)
            if retry_reason is None:
                break

            if attempt >= max_attempts - 1:
                raise AutomationError(
                    "script dry-run failed after retries: "
                    f"{retry_reason}. Please provide a clearer command or adjust the conversation agent prompt.",
                    debug={
                        "phase": "create_script_dry_run",
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "retry_reason": retry_reason,
                        "plan": plan,
                        "normalized": {
                            "sequence": sequence,
                            "mode": mode,
                        },
                    },
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
            "sequence": sequence,
            "mode": mode,
        }

        created = self.client.create_script(payload)
        self.client.reload_scripts()

        script_id = str(created.get("script_id") or "").strip()
        should_expose = self._resolve_script_expose_preference(text=text, plan=plan)
        exposed_to_assist: bool | None = None
        exposure_warning = ""
        exposed_entity_id = ""

        if should_expose:
            try:
                exposed_entity_id = str(
                    self.client.resolve_script_entity_id(script_id=script_id, alias=final_name) or ""
                ).strip()
                if not exposed_entity_id:
                    raise AutomationError("script entity_id was not found after reload")
                self.client.set_entity_exposed_to_conversation(exposed_entity_id, should_expose=True)
                exposed_to_assist = True
            except Exception as exc:
                exposed_to_assist = False
                exposure_warning = f"created but failed to expose to Assist: {exc}"
                logger.warning(exposure_warning)
        else:
            exposed_to_assist = False

        if self.client.manage_only_exposed_scripts and not exposed_to_assist:
            raise AutomationError(
                "script created but not exposed to Assist; policy blocks managing non-exposed scripts"
            )

        return {
            "ok": True,
            "operation": "create_script",
            "script_id": script_id,
            "name": final_name,
            "ai_generated_name": ai_generated_name,
            "summary": summary,
            "assist_exposed": exposed_to_assist,
            "assist_entity_id": exposed_entity_id,
            "assist_exposure_warning": exposure_warning,
            "message": f"已创建脚本: {final_name}",
        }

    def prepare_manage(
        self,
        text: str,
        expected_operation: str,
        session_id: str = "",
        language: str | None = None,
    ) -> dict[str, Any]:
        operation = "update" if expected_operation == "task_update" else "delete"
        plan = self._plan_from_text(operation, text, language=language)

        target_name = str(plan.get("target_name") or "").strip()
        summary = str(plan.get("summary") or "").strip() or str(text).strip()

        match = self._native_match_automation(
            user_text=text,
            query_name=target_name,
            query_content=summary,
            language=language,
        )
        if match is None:
            match = self._match_automation(query_name=target_name, query_content=summary)
        target = match.automation

        target_id = str(target.get("id") or target.get("automation_id") or "").strip()
        target_alias = str(target.get("alias") or target.get("name") or "").strip()
        if not target_id:
            raise AutomationError("matched automation has no id")

        payload = {
            "resource_type": "automation",
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

    def manage_without_confirmation(
        self,
        text: str,
        expected_operation: str,
        session_id: str = "",
        language: str | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_manage(
            text=text,
            expected_operation=expected_operation,
            session_id=session_id,
            language=language,
        )
        confirmation_id = str(prepared.get("confirmation_id") or "").strip()
        if not confirmation_id:
            raise AutomationError("failed to create confirmation for direct manage execution")
        return self.confirm_manage(confirmation_id)

    def confirm_manage(
        self,
        confirmation_id: str,
        expected_operation: str | None = None,
    ) -> dict[str, Any]:
        payload = self.pending.pop(confirmation_id)
        if not payload:
            raise AutomationError("confirmation_id invalid or expired")

        payload_type = str(payload.get("resource_type") or "automation").strip()
        if payload_type != "automation":
            raise AutomationError("confirmation_id is not for automation operation")

        operation = str(payload.get("operation") or "").strip()
        if expected_operation:
            op_raw = str(expected_operation or "").strip()
            normalized_expected = "update" if op_raw == "task_update" else "delete" if op_raw == "task_delete" else ""
            if not normalized_expected:
                raise AutomationError("expected_operation must be task_update or task_delete")
            if normalized_expected != operation:
                raise AutomationError("confirmation_id does not match expected_operation")

        def _task() -> dict[str, Any]:
            target_id = str(payload.get("target_id") or "")
            target_alias = str(payload.get("target_alias") or "")
            plan_raw = payload.get("plan")
            plan: dict[str, Any] = plan_raw if isinstance(plan_raw, dict) else {}
            input_text = str(payload.get("input_text") or "")
            language = str(payload.get("language") or self.client.conversation_language)

            if operation == "delete":
                try:
                    self.client.delete_automation(target_id)
                    self.client.reload_automations()
                except Exception as exc:
                    backup_used = self.client._restore_latest_automation_backup()
                    if backup_used:
                        try:
                            self.client.reload_automations()
                        except Exception as reload_exc:
                            raise AutomationError(
                                f"delete failed and rollback reload also failed: {reload_exc}",
                                debug={"backup": backup_used, "cause": str(exc)},
                            ) from reload_exc
                        raise AutomationError(
                            f"delete failed and rolled back from backup: {exc}",
                            debug={"backup": backup_used},
                        ) from exc
                    raise AutomationError(f"delete failed: {exc}") from exc
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
            condition = self._normalize_condition_list(condition_plan if condition_plan else current_condition)
            action = self._normalize_action_list(action_plan if action_plan else current_action, input_text)
            mode = str(plan.get("mode") or "").strip() or str(current.get("mode") or "single")

            behavior_unchanged = (
                final_name == current_alias
                and self._json_like_equal(trigger, current_trigger)
                and self._json_like_equal(condition, current_condition)
                and self._json_like_equal(action, current_action)
                and mode == str(current.get("mode") or "single")
            )
            if behavior_unchanged:
                raise AutomationError(
                    "no effective update was detected from this instruction; automation was not changed",
                    debug={
                        "phase": "update_noop",
                        "target_id": target_id,
                        "target_alias": target_alias,
                        "plan": plan,
                        "current": {
                            "name": current_alias,
                            "trigger": current_trigger,
                            "condition": current_condition,
                            "action": current_action,
                            "mode": str(current.get("mode") or "single"),
                        },
                        "normalized": {
                            "name": final_name,
                            "trigger": trigger,
                            "condition": condition,
                            "action": action,
                            "mode": mode,
                        },
                    },
                )

            update_dry_run_error = self._dry_run_validate_payload(trigger, condition, action, mode)
            if update_dry_run_error is not None:
                raise AutomationError(
                    "automation update dry-run failed: "
                    f"{update_dry_run_error}. Please provide clearer update instructions.",
                    debug={
                        "phase": "update_dry_run",
                        "target_id": target_id,
                        "target_alias": target_alias,
                        "plan": plan,
                        "normalized": {
                            "trigger": trigger,
                            "condition": condition,
                            "action": action,
                            "mode": mode,
                        },
                    },
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

            try:
                self.client.update_automation(target_id, update_payload)
                self.client.reload_automations()
            except Exception as exc:
                backup_used = self.client._restore_latest_automation_backup()
                if backup_used:
                    try:
                        self.client.reload_automations()
                    except Exception as reload_exc:
                        raise AutomationError(
                            f"update failed and rollback reload also failed: {reload_exc}",
                            debug={"backup": backup_used, "cause": str(exc)},
                        ) from reload_exc
                    raise AutomationError(
                        f"update failed and rolled back from backup: {exc}",
                        debug={"backup": backup_used},
                    ) from exc
                raise AutomationError(f"update failed: {exc}") from exc

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

        return self._run_confirm_serially(_task)

    def prepare_manage_script(
        self,
        text: str,
        expected_operation: str,
        session_id: str = "",
        language: str | None = None,
    ) -> dict[str, Any]:
        operation = "update" if expected_operation == "task_update" else "delete"
        plan = self._plan_script_from_text(operation, text, language=language)

        target_name = str(plan.get("target_name") or "").strip()
        summary = str(plan.get("summary") or "").strip() or str(text).strip()

        match = self._native_match_script(
            user_text=text,
            query_name=target_name,
            query_content=summary,
            language=language,
        )
        if match is None:
            match = self._match_script(query_name=target_name, query_content=summary)
        target = match.automation

        target_id = str(target.get("script_id") or "").strip()
        target_alias = str(target.get("alias") or target_id).strip()
        if not target_id:
            raise AutomationError("matched script has no script_id")

        payload = {
            "resource_type": "script",
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
            "resource_type": "script",
            "match_score": round(match.score, 4),
            "target": {
                "id": target_id,
                "name": target_alias,
                "summary": match.summary,
            },
            "message": (
                f"请确认{operation}脚本: 目标是【{target_alias}】, 内容概述: {match.summary}. "
                f"若确认，请再次调用并携带 confirmation_id={confirmation_id}。"
            ),
        }

    def manage_script_without_confirmation(
        self,
        text: str,
        expected_operation: str,
        session_id: str = "",
        language: str | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_manage_script(
            text=text,
            expected_operation=expected_operation,
            session_id=session_id,
            language=language,
        )
        confirmation_id = str(prepared.get("confirmation_id") or "").strip()
        if not confirmation_id:
            raise AutomationError("failed to create confirmation for direct script manage execution")
        return self.confirm_manage_script(confirmation_id)

    def confirm_manage_script(
        self,
        confirmation_id: str,
        expected_operation: str | None = None,
    ) -> dict[str, Any]:
        payload = self.pending.pop(confirmation_id)
        if not payload:
            raise AutomationError("confirmation_id invalid or expired")

        payload_type = str(payload.get("resource_type") or "").strip()
        if payload_type != "script":
            raise AutomationError("confirmation_id is not for script operation")

        operation = str(payload.get("operation") or "").strip()
        if expected_operation:
            op_raw = str(expected_operation or "").strip()
            normalized_expected = "update" if op_raw == "task_update" else "delete" if op_raw == "task_delete" else ""
            if not normalized_expected:
                raise AutomationError("expected_operation must be task_update or task_delete")
            if normalized_expected != operation:
                raise AutomationError("confirmation_id does not match expected_operation")

        def _task() -> dict[str, Any]:
            target_id = str(payload.get("target_id") or "")
            target_alias = str(payload.get("target_alias") or "")
            plan_raw = payload.get("plan")
            plan: dict[str, Any] = plan_raw if isinstance(plan_raw, dict) else {}
            input_text = str(payload.get("input_text") or "")
            language = str(payload.get("language") or self.client.conversation_language)

            if operation == "delete":
                try:
                    self.client.delete_script(target_id)
                    self.client.reload_scripts()
                except Exception as exc:
                    backup_used = self.client._restore_latest_script_backup()
                    if backup_used:
                        try:
                            self.client.reload_scripts()
                        except Exception as reload_exc:
                            raise AutomationError(
                                f"delete script failed and rollback reload also failed: {reload_exc}",
                                debug={"backup": backup_used, "cause": str(exc)},
                            ) from reload_exc
                        raise AutomationError(
                            f"delete script failed and rolled back from backup: {exc}",
                            debug={"backup": backup_used},
                        ) from exc
                    raise AutomationError(f"delete script failed: {exc}") from exc
                return {
                    "ok": True,
                    "operation": "delete_script",
                    "script_id": target_id,
                    "name": target_alias,
                    "message": f"已删除脚本: {target_alias}",
                }

            if operation != "update":
                raise AutomationError(f"unsupported pending operation: {operation}")

            all_items = self._managed_scripts()
            current = next(
                (
                    item
                    for item in all_items
                    if str(item.get("script_id") or "") == target_id
                ),
                None,
            )
            if not current:
                raise AutomationError("target script not found before update")

            current_alias = str(current.get("alias") or target_id).strip()
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

            sequence_plan = plan.get("sequence") if isinstance(plan.get("sequence"), list) and plan.get("sequence") else []
            current_sequence = self._entry_sequence(current)
            sequence = self._normalize_action_list(sequence_plan if sequence_plan else current_sequence, input_text)
            mode = str(plan.get("mode") or "").strip() or str(current.get("mode") or "single")

            behavior_unchanged = (
                final_name == current_alias
                and self._json_like_equal(sequence, current_sequence)
                and mode == str(current.get("mode") or "single")
            )
            if behavior_unchanged:
                raise AutomationError(
                    "no effective update was detected from this instruction; script was not changed",
                    debug={
                        "phase": "update_script_noop",
                        "target_id": target_id,
                        "target_alias": target_alias,
                        "plan": plan,
                        "current": {
                            "name": current_alias,
                            "sequence": current_sequence,
                            "mode": str(current.get("mode") or "single"),
                        },
                        "normalized": {
                            "name": final_name,
                            "sequence": sequence,
                            "mode": mode,
                        },
                    },
                )

            update_dry_run_error = self._dry_run_validate_script_payload(sequence, mode)
            if update_dry_run_error is not None:
                raise AutomationError(
                    "script update dry-run failed: "
                    f"{update_dry_run_error}. Please provide clearer update instructions.",
                    debug={
                        "phase": "update_script_dry_run",
                        "target_id": target_id,
                        "target_alias": target_alias,
                        "plan": plan,
                        "normalized": {
                            "sequence": sequence,
                            "mode": mode,
                        },
                    },
                )

            updated_meta = {
                "ai_generated_name": ai_generated_after,
                "summary": summary,
                "updated_at": int(time.time()),
            }
            description = self._build_description(clean_desc, updated_meta)

            update_payload = {
                "alias": final_name,
                "description": description,
                "sequence": sequence,
                "mode": mode,
            }

            try:
                self.client.update_script(target_id, update_payload)
                self.client.reload_scripts()
            except Exception as exc:
                backup_used = self.client._restore_latest_script_backup()
                if backup_used:
                    try:
                        self.client.reload_scripts()
                    except Exception as reload_exc:
                        raise AutomationError(
                            f"update script failed and rollback reload also failed: {reload_exc}",
                            debug={"backup": backup_used, "cause": str(exc)},
                        ) from reload_exc
                    raise AutomationError(
                        f"update script failed and rolled back from backup: {exc}",
                        debug={"backup": backup_used},
                    ) from exc
                raise AutomationError(f"update script failed: {exc}") from exc

            return {
                "ok": True,
                "operation": "update_script",
                "script_id": target_id,
                "old_name": current_alias,
                "name": final_name,
                "ai_generated_name": ai_generated_after,
                "summary": summary,
                "message": f"已更新脚本: {current_alias} -> {final_name}",
            }

        return self._run_confirm_serially(_task)

    def confirm_latest_manage(self, expected_operation: str | None = None) -> dict[str, Any]:
        operation: str | None = None
        if expected_operation:
            op_raw = str(expected_operation or "").strip()
            if op_raw == "task_update":
                operation = "update"
            elif op_raw == "task_delete":
                operation = "delete"
            else:
                raise AutomationError("expected_operation must be task_update or task_delete")

        if operation is None:
            pending_count = self.pending.count(resource_type="automation")
            if pending_count > 1:
                raise AutomationError(
                    "multiple pending automation confirmations exist; "
                    "please confirm with confirmation_id or expected_operation"
                )

        latest = self.pending.pop_latest(
            operation=operation,
            resource_type="automation",
        )
        if not latest:
            if operation:
                raise AutomationError("no pending automation confirmation found for operation")
            raise AutomationError("no pending automation confirmation found")

        token, _ = latest
        return self.confirm_manage(token)

    def confirm_latest_manage_script(self, expected_operation: str | None = None) -> dict[str, Any]:
        operation: str | None = None
        if expected_operation:
            op_raw = str(expected_operation or "").strip()
            if op_raw == "task_update":
                operation = "update"
            elif op_raw == "task_delete":
                operation = "delete"
            else:
                raise AutomationError("expected_operation must be task_update or task_delete")

        if operation is None:
            pending_count = self.pending.count(resource_type="script")
            if pending_count > 1:
                raise AutomationError(
                    "multiple pending script confirmations exist; "
                    "please confirm with confirmation_id or expected_operation"
                )

        latest = self.pending.pop_latest(
            operation=operation,
            resource_type="script",
        )
        if not latest:
            if operation:
                raise AutomationError("no pending script confirmation found for operation")
            raise AutomationError("no pending script confirmation found")

        token, _ = latest
        return self.confirm_manage_script(token)
