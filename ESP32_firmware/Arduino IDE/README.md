# HumbleVoice ESP32 Firmware

ESP32 client firmware for streaming microphone audio to server `/audio` over WebSocket.

## Hardware

- ESP32-S3-Box (recommended) or ESP32-S3 dev board
- I2S microphone (built-in or external)
- Optional I2S speaker for playback experiments
- USB cable for flashing

## Arduino IDE Setup

1. Install Arduino IDE.
2. Install ESP32 board package.
3. Install required libraries:
   - WebSockets (links2004)
   - ArduinoJson (Benoit Blanchon)

## Firmware Config

Edit `Alexa.ino`:

- `ssid`
- `password`
- `server_ip`
- `server_port` (default `8000`)

WebSocket path is hardcoded to `/audio`.

## Run Flow

1. Start server bridge on host:

```bash
python server/main.py
```

2. Flash ESP32 firmware.
3. Open serial monitor and verify:
   - WiFi connected
   - WebSocket connected
   - Binary audio frames being sent

## Important Limitation

- `handleBinaryMessage` playback on ESP32 is still not fully implemented. The firmware can receive TTS bytes, but local speaker playback path is currently placeholder.
