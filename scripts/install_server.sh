#!/bin/bash

# HumbleVoice Server Installation Script
# Installs all dependencies for the HumbleVoice server on Linux

set -e  # Exit on any error

echo "========================================="
echo "HumbleVoice Server Installation Script"
echo "========================================="

# Check if running as root (not required)
if [ "$EUID" -eq 0 ]; then
    echo "Warning: Running as root is not recommended"
    echo "Please run this script as a regular user"
    exit 1
fi

# Check if running on Linux
if [[ ! "$OSTYPE" =~ ^linux ]]; then
    echo "This script is designed for Linux systems only"
    exit 1
fi

# Check if running on ARM64 (for Raspberry Pi) or x86_64
ARCH=$(uname -m)
echo "Detected architecture: $ARCH"

# Update system packages
echo "Updating system packages..."
sudo apt update
sudo apt upgrade -y

# Install required system dependencies
echo "Installing system dependencies..."
sudo apt install -y \
    python3 \
    python3-pip \
    git \
    curl \
    wget \
    build-essential \
    cmake \
    libasound2-dev \
    portaudio19-dev \
    python3-dev

# Install Python virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv humblevoice_env
source humblevoice_env/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -r server/requirements.txt

# Install Ollama
echo "Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh

# Start Ollama service
echo "Starting Ollama service..."
sudo systemctl enable ollama
sudo systemctl start ollama

# Wait for Ollama to be ready
echo "Waiting for Ollama to start..."
sleep 10

# Pull default LLM model
echo "Pulling default LLM model (phi3:mini)..."
ollama pull phi3:mini

# Install Whisper.cpp
echo "Installing Whisper.cpp..."
if [ ! -d "whisper.cpp" ]; then
    git clone https://github.com/ggerganov/whisper.cpp.git
    cd whisper.cpp
    make
    cd ..
else
    echo "Whisper.cpp already exists, skipping clone"
fi

# Install Piper TTS
echo "Installing Piper TTS..."
if [ ! -d "piper" ]; then
    if [ "$ARCH" == "aarch64" ] || [ "$ARCH" == "arm64" ]; then
        # ARM64 version for Raspberry Pi
        wget https://github.com/rhasspy/piper/releases/download/2023.11.14/piper_linux_aarch64.tar.gz
        tar -xzf piper_linux_aarch64.tar.gz
    else
        # x86_64 version
        wget https://github.com/rhasspy/piper/releases/download/2023.11.14/piper_linux_x86_64.tar.gz
        tar -xzf piper_linux_x86_64.tar.gz
    fi
else
    echo "Piper already exists, skipping download"
fi

# Download models
echo "Downloading models..."
bash server/models/download_models.sh

# Create systemd service (optional)
echo "Creating systemd service file..."
cat > humblevoice.service << EOF
[Unit]
Description=HumbleVoice Server
After=network.target
Wants=ollama.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
Environment=PATH=$(pwd)/humblevoice_env/bin
ExecStart=$(pwd)/humblevoice_env/bin/python3 server/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo cp humblevoice.service /etc/systemd/system/
sudo systemctl daemon-reload

echo "========================================="
echo "Installation Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "1. Edit server/config.yaml with your settings"
echo "2. Update ESP32 firmware with your server IP"
echo "3. Start the server: python3 server/main.py"
echo "4. Or enable systemd service: sudo systemctl enable --now humblevoice"
echo ""
echo "To start the server manually:"
echo "source humblevoice_env/bin/activate"
echo "python3 server/main.py"
echo ""
echo "Server will be available at: http://$(hostname -I | cut -d' ' -f1):8000"