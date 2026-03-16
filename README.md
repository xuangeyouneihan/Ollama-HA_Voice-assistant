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

### Auto apply after task change

- 创建/修改/删除任务后，服务会自动尝试调用：
  - `script.reload`
  - `automation.reload`
- 返回结果中的 `applied` 字段会给出自动生效是否成功。
- 如果自动生效失败，语音回复会提示你在 HA 手动重载。

### One sentence to create HA automation

- 你可以直接对本地语音助手说一句：
  - `每天早上7点如果卧室温度低于18度就打开卧室空调`
- 程序会自动执行：
  - 使用 LLM 解析自然语言为 preset
  - 自动保存 preset
  - 自动编译成 HA 原生 `script` + `automation`
  - 导出 YAML 到 `server/data/ha_exports/`
- 把导出的 YAML 合并到 Home Assistant 配置后，重载 `automation` 与 `script` 即可生效。

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
