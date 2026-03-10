# HumbleVoice - Open Source Smart Speaker

A privacy-focused, offline-capable smart speaker system using local LLMs and Home Assistant integration. Built with ESP32 hardware and runs completely on your local network.

## Features

- **100% Offline**: No cloud dependencies, all processing happens locally
- **ESP32 Client**: Microphone and speaker integration via I2S
- **Local LLM**: Uses Ollama for private AI conversations
- **Home Assistant Integration**: Control smart home devices with voice
- **Open Source**: Fully customizable and transparent
- **Arduino IDE Compatible**: Easy ESP32 firmware setup

## Preset Workflow (No Real Devices Required)

You can build and test automation presets before smart devices are ready.

### What is available now

- Preset CRUD API in server
- Preset simulation API (no real HA call)
- Compile preset to Home Assistant script/automation dictionaries

### API endpoints

- `GET /presets`
- `GET /presets/{preset_id}`
- `POST /presets`
- `PUT /presets/{preset_id}`
- `DELETE /presets/{preset_id}`
- `POST /presets/{preset_id}/simulate`
- `POST /presets/{preset_id}/compile-ha`

### Minimal preset payload example

```json
{
   "name": "Rain Close Cover",
   "enabled": true,
   "trigger": {
      "type": "state",
      "entity_id": "binary_sensor.rain_detected",
      "to": "on"
   },
   "conditions": [
      {
         "type": "time",
         "after": "06:00:00",
         "before": "23:00:00"
      }
   ],
   "actions": [
      {
         "service": "cover.close_cover",
         "target": {
            "entity_id": ["cover.living_room"]
         },
         "service_data": {}
      }
   ]
}
```

## Hardware Requirements

### Server (Raspberry Pi 4/5 or Linux computer)
- Raspberry Pi 4 (4GB+ RAM) or modern Linux computer
- MicroSD card (32GB+) or SSD for storage
- Ethernet or WiFi connection

### Client (ESP32 Device)
- ESP32-S3-Box (recommended) or ESP32-S3 development board
- I2S microphone (built-in on ESP32-S3-Box)
- I2S speaker (optional, for audio playback)
- Micro-USB or USB-C cable for programming

## Quick Start

### Server Setup (Raspberry Pi/Linux)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/humblevoice.git
   cd humblevoice