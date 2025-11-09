# HumbleVoice ESP32 Client Firmware

This firmware runs on ESP32 hardware to create a smart speaker client that streams audio to the HumbleVoice server.

## Hardware Requirements

- **ESP32-S3-Box** (recommended) or **ESP32-S3 Dev Board**
- **I2S Microphone** (built-in on ESP32-S3-Box, or external INMP441)
- **I2S Speaker** (optional, for audio playback)
- **Micro-USB or USB-C** for programming

## Arduino IDE Setup

### 1. Install Arduino IDE
Download from [arduino.cc](https://www.arduino.cc/en/software)

### 2. Install ESP32 Board Package
1. Go to **File > Preferences**
2. Add to "Additional Board Manager URLs":