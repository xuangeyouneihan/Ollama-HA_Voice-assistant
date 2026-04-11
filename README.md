# HumbleVoice

Home Assistant automation and script manager service based on Home Assistant APIs.

## Recommended Deployment

- This repository: runs backend APIs for Home Assistant automation and script management.
- Home Assistant: remains the execution and state platform.

## Current Architecture

- Management mode: `server/main.py`
  - Server converts natural language requests to automation/script operations.
  - Server creates/updates/deletes automations and scripts through Home Assistant APIs.

## Integration APIs

- Automation APIs:
  - `POST /ha-tasks/from-text`
  - `POST /ha-tasks/manage-from-text`
  - `POST /ha-tasks/confirm`
- Script APIs:
  - `POST /ha-scripts/from-text`
  - `POST /ha-scripts/manage-from-text`
  - `POST /ha-scripts/confirm`

## What Was Removed

- LLM module (`server/modules/llm.py`)
- Preset/task module (`server/modules/presets.py`)
- Task/preset related APIs
- Local desktop assistant entrypoint (`server/local_assistant.py`)

## Requirements

- Python 3.10+
- Home Assistant instance
- Valid long-lived access token in `server/config.yaml` (or private override)

## Quick Start

1. Install dependencies:

```bash
pip install -r server/requirements.txt
```

2. Configure Home Assistant URL/token in `server/config.yaml` (or `config.private.yaml`).

3. Run management service:

```bash
python server/main.py
```

## Notes

- Service includes startup self-check for external Ollama planning capability.
- Main service health endpoint: `GET /health`.
