# HumbleVoice

Home Assistant Assist pipeline based voice assistant. Current codebase focuses on audio flow only.

## Recommended Deployment

- Voice satellite: use Linux Voice Assistant (LVA) or other HA Assist-compatible satellite as the capture/playback endpoint.
- This repository: runs backend bridge/API logic for ESP32 audio bridge and automation/script management APIs.
- Home Assistant: remains the central Assist pipeline and conversation orchestration endpoint.

## Current Architecture

- ESP32 mode: `server/main.py` + `ESP32_firmware/Arduino IDE/Alexa.ino`
  - ESP32 streams microphone audio to server `/audio`
  - Server forwards to Home Assistant Assist pipeline
  - Server sends TTS audio bytes back to ESP32

## Integration APIs

- Audio bridge: `GET /health`, `WS /audio`
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
- Home Assistant instance with Assist pipeline enabled
- Valid long-lived access token in `server/config.yaml` (or private override)

## Quick Start

1. Install dependencies:

```bash
pip install -r server/requirements.txt
```

2. Configure Home Assistant URL/token in `server/config.yaml` (or `config.private.yaml`).

3. Run ESP32 bridge mode:

```bash
python server/main.py
```

## Notes

- ESP32 firmware is configured to connect WebSocket path `/audio` on port `8000`.
- ESP32 playback implementation is still TODO in firmware (`handleBinaryMessage`).
- If you use Linux Voice Assistant (satellite), add it in Home Assistant via ESPHome integration (port `6053` on the satellite host).
