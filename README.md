# HumbleVoice - Open Source Smart Speaker

A privacy-focused smart speaker system built around Home Assistant Assist pipeline. Built with ESP32 hardware and designed for local-network deployment.

## Features

- **HA Assist Pipeline**: Voice requests are forwarded to Home Assistant Assist pipeline
- **ESP32 Client**: Microphone and speaker integration via I2S
- **Automation Task API**: Text-to-automation CRUD endpoints for Home Assistant workflows
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

### Auto apply after task change

- After creating/updating/deleting a task, the service will automatically try to call:
  - `script.reload`
  - `automation.reload`
- The `applied` field in the response indicates whether auto-apply succeeded.
- If auto-apply fails, the voice response will remind you to reload in Home Assistant manually.

### One sentence to create HA automation

- You can speak one sentence directly to the local voice assistant:
  - `Every morning at 7:00, if the bedroom temperature is below 18°C, turn on the bedroom AC.`
- The system will automatically:
  - Use the LLM to parse natural language into a preset
  - Save the preset
  - Compile it into native Home Assistant `script` + `automation`
  - Export YAML to `server/data/ha_exports/`
- Merge the exported YAML into your Home Assistant configuration, then reload `automation` and `script` to apply.

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
   ```
