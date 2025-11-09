#!/bin/bash

# HumbleVoice Model Downloader
# Downloads required models for STT, TTS, and other components

set -e  # Exit on error

echo "========================================="
echo "HumbleVoice Model Downloader"
echo "========================================="

# Create models directory if it doesn't exist
mkdir -p models
cd models

# Download Whisper.cpp STT model
echo "Downloading Whisper.cpp STT model..."
if [ ! -f "ggml-tiny.en.bin" ]; then
    echo "Downloading tiny.en model (~75MB)..."
    wget -O ggml-tiny.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin
    echo "STT model downloaded successfully"
else
    echo "STT model already exists, skipping download"
fi

# Download Piper TTS model
echo "Downloading Piper TTS model..."
if [ ! -f "en_US-lessac-medium.onnx" ]; then
    echo "Downloading en_US-lessac-medium model (~58MB)..."
    wget -O en_US-lessac-medium.onnx https://github.com/rhasspy/piper/releases/download/2023.11.14/en_US-lessac-medium.onnx
    echo "TTS model downloaded successfully"
else
    echo "TTS model already exists, skipping download"
fi

# Optional: Download larger STT model (uncomment if needed)
# echo "Downloading medium.en model (~500MB)..."
# wget -O ggml-medium.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin

# Optional: Download different TTS voice (uncomment if needed)
# echo "Downloading different TTS voice..."
# wget -O en_US-kathleen-low.onnx https://github.com/rhasspy/piper/releases/download/2023.11.14/en_US-kathleen-low.onnx

echo "========================================="
echo "Model Download Complete!"
echo "========================================="
echo ""
echo "Models downloaded to: $(pwd)"
echo ""
echo "STT Model: ggml-tiny.en.bin (75MB)"
echo "TTS Model: en_US-lessac-medium.onnx (58MB)"
echo ""
echo "Total size: ~133MB"