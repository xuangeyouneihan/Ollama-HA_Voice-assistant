# HumbleVoice

Home Assistant Assist pipeline based voice assistant. Current codebase focuses on audio flow only.

## Current Architecture

- Local mode: `server/local_assistant.py`
  - Microphone capture on local machine
  - Send raw audio to Home Assistant Assist pipeline
  - Play returned TTS audio on local speaker
- ESP32 mode: `server/main.py` + `ESP32_firmware/Arduino IDE/Alexa.ino`
  - ESP32 streams microphone audio to server `/audio`
  - Server forwards to Home Assistant Assist pipeline
  - Server sends TTS audio bytes back to ESP32

## What Was Removed

- LLM module (`server/modules/llm.py`)
- Preset/task module (`server/modules/presets.py`)
- Task/preset related APIs

## Requirements

- Python 3.10+
- Home Assistant instance with Assist pipeline enabled
- Valid long-lived access token in `server/config.yaml` (or private override)
- For local mode: working microphone and speaker on host machine

## Quick Start

1. Install dependencies:

```bash
pip install -r server/requirements.txt
```

2. Configure Home Assistant URL/token in `server/config.yaml` (or `config.private.yaml`).

3. Choose one run mode:

- Local mode:

```bash
python server/local_assistant.py
```

- ESP32 bridge mode:

```bash
python server/main.py
```

## Notes

- ESP32 firmware is configured to connect WebSocket path `/audio` on port `8000`.
- ESP32 playback implementation is still TODO in firmware (`handleBinaryMessage`).
