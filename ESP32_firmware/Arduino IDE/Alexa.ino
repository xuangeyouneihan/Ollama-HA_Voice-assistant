/*
 * HumbleVoice ESP32 Client
 *
 * This is the main firmware for the ESP32 smart speaker client.
 * It handles:
 * - WiFi connection
 * - Microphone audio capture via I2S
 * - WebSocket streaming to server (server forwards to Home Assistant Assist pipeline)
 * - Audio playback from Home Assistant Assist TTS response
 *
 * Requirements:
 * - Arduino IDE with ESP32 board package
 * - WebSockets library (by links2004)
 * - ArduinoJson library (by Benoit Blanchon)
 *
 * Hardware: ESP32-S3-Box or ESP32-S3 with I2S microphone
 *
 * Author: HumbleVoice Team
 * Date: 2025
 * License: MIT
 */

#include <WiFi.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>
#include <ArduinoJson.h>

// ========= USER CONFIGURATION =========
// CHANGE THESE VALUES BEFORE UPLOADING
const char *ssid = "YOUR_WIFI_SSID";         // Your WiFi network name
const char *password = "YOUR_WIFI_PASSWORD"; // Your WiFi password
const char *server_ip = "192.168.1.100";     // Your server IP address
const int server_port = 8000;                // Server port (default 8000)
const char *wake_word = "Hey Aura";          // Wake word detection (not implemented yet)
// ======================================

// I2S Pin Configuration - Adjust for your hardware
#define I2S_BCLK 5  // Bit clock pin
#define I2S_LRCK 6  // Left/Right clock pin
#define I2S_DIN 4   // Data input pin (microphone)
#define I2S_DOUT -1 // Data output pin (speaker, -1 if not used)

// Audio buffer configuration
const size_t AUDIO_BUFFER_SIZE = 1024;                     // Size of audio chunks to send
const int SAMPLE_RATE = 16000;                             // Audio sample rate (Hz)
const int I2S_BITS_PER_SAMPLE = I2S_BITS_PER_SAMPLE_16BIT; // 16-bit audio

// Global variables
WebSocketsClient webSocket;              // WebSocket client for server communication
uint8_t audio_buffer[AUDIO_BUFFER_SIZE]; // Buffer to store audio data
bool is_connected = false;               // Connection status flag
unsigned long last_ping = 0;             // For WebSocket keep-alive

void setup()
{
  // Initialize serial communication for debugging
  Serial.begin(115200);
  delay(1000); // Wait for serial to initialize

  Serial.println("=== HumbleVoice ESP32 Client Starting ===");
  Serial.println("Initializing WiFi connection...");

  // Connect to WiFi network
  connectToWiFi();

  Serial.println("WiFi connected!");
  Serial.print("ESP32 IP Address: ");
  Serial.println(WiFi.localIP());

  // Initialize WebSocket connection to server
  setupWebSocket();

  // Initialize I2S audio interface for microphone
  setupI2SAudio();

  Serial.println("=== HumbleVoice Client Ready ===");
  Serial.println("Now streaming audio to server (HA Assist pipeline backend)...");
}

void loop()
{
  // Handle WebSocket connection and events
  webSocket.loop();

  // Check for incoming audio data from microphone
  checkAudioData();

  // Send periodic ping to keep WebSocket alive
  handleKeepAlive();

  // Small delay to prevent watchdog timer issues
  delay(1);
}

void connectToWiFi()
{
  /*
   * Connect to WiFi network
   * Retries until connection is established
   */
  WiFi.begin(ssid, password);

  // Wait for connection with timeout
  int attempts = 0;
  const int max_attempts = 30; // 30 seconds timeout

  while (WiFi.status() != WL_CONNECTED && attempts < max_attempts)
  {
    delay(1000);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() != WL_CONNECTED)
  {
    Serial.println("\nFailed to connect to WiFi!");
    Serial.println("Please check your WiFi credentials and try again.");
    // In a real implementation, you might want to restart or enter setup mode
    return;
  }

  Serial.println("");
  Serial.print("Connected to WiFi: ");
  Serial.println(ssid);
}

void setupWebSocket()
{
  /*
   * Initialize WebSocket connection to the HumbleVoice server
   * Sets up event handler and connection parameters
   */
  Serial.println("Setting up WebSocket connection...");

  // Initialize WebSocket with server address and path
  webSocket.begin(server_ip, server_port, "/audio");

  // Set up event handler function
  webSocket.onEvent(websocketEvent);

  // Set reconnect interval (milliseconds)
  webSocket.setReconnectInterval(5000);

  Serial.println("WebSocket setup complete");
}

void setupI2SAudio()
{
  /*
   * Configure I2S interface for microphone input
   * Sets up sample rate, bit depth, and pin assignments
   */
  Serial.println("Setting up I2S audio interface...");

  // Configure I2S driver parameters
  i2s_config_t i2s_config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX), // Master mode, receive only
      .sample_rate = SAMPLE_RATE,                          // 16kHz sample rate
      .bits_per_sample = I2S_BITS_PER_SAMPLE,              // 16-bit samples
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,         // Mono audio (left channel only)
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,   // Standard I2S format
      .intr_alloc_flags = 0,                               // Default interrupt allocation
      .dma_buf_count = 8,                                  // Number of DMA buffers
      .dma_buf_len = 64,                                   // Length of each DMA buffer
      .use_apll = false,                                   // Don't use APLL
      .tx_desc_auto_clear = false,                         // Don't auto-clear TX descriptors
      .fixed_mclk = 0                                      // No fixed MCLK
  };

  // Configure I2S pin assignments
  i2s_pin_config_t pin_config = {
      .bck_io_num = I2S_BCLK,   // Bit clock pin
      .ws_io_num = I2S_LRCK,    // Word select (LRCK) pin
      .data_out_num = I2S_DOUT, // Data output pin (-1 = not used)
      .data_in_num = I2S_DIN    // Data input pin (microphone)
  };

  // Install and configure I2S driver
  esp_err_t err = i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);
  if (err != ESP_OK)
  {
    Serial.printf("Failed to install I2S driver: %d\n", err);
    return;
  }

  // Set pin configuration
  err = i2s_set_pin(I2S_NUM_0, &pin_config);
  if (err != ESP_OK)
  {
    Serial.printf("Failed to set I2S pins: %d\n", err);
    return;
  }

  Serial.println("I2S audio interface configured successfully");
}

void checkAudioData()
{
  /*
   * Check for new audio data from microphone
   * If data is available, send it to the server via WebSocket
   */
  size_t bytes_read = 0;

  // Read audio data from I2S interface
  esp_err_t result = i2s_read(I2S_NUM_0, &audio_buffer, AUDIO_BUFFER_SIZE, &bytes_read, pdMS_TO_TICKS(10));

  if (result == ESP_OK && bytes_read > 0 && webSocket.isConnected())
  {
    // Send audio data to server
    webSocket.sendBIN(audio_buffer, bytes_read);

    // Optional: Print data size for debugging
    // Serial.printf("Sent %d bytes of audio data\n", bytes_read);
  }
  else if (result != ESP_OK)
  {
    // Handle I2S read errors
    Serial.printf("I2S read error: %d\n", result);
  }
}

void handleKeepAlive()
{
  /*
   * Send periodic ping to keep WebSocket connection alive
   * Prevents connection timeout due to inactivity
   */
  unsigned long current_time = millis();

  // Send ping every 30 seconds if connected
  if (webSocket.isConnected() && (current_time - last_ping > 30000))
  {
    webSocket.sendTXT("ping");
    last_ping = current_time;
  }
}

void websocketEvent(WStype_t type, uint8_t *payload, size_t length)
{
  /*
   * Handle WebSocket events from the server
   * Events include connection status, received data, errors, etc.
   */
  switch (type)
  {
  case WStype_DISCONNECTED:
    Serial.println("[WSc] Disconnected from server!");
    is_connected = false;
    break;

  case WStype_CONNECTED:
    Serial.print("[WSc] Connected to server: ");
    Serial.println((char *)payload);
    is_connected = true;
    break;

  case WStype_TEXT:
    Serial.printf("[WSc] Received text message: %s\n", (char *)payload);
    // Server might send status updates or commands as text
    handleTextMessage((char *)payload);
    break;

  case WStype_BIN:
    Serial.printf("[WSc] Received binary audio data, length: %u bytes\n", length);
    // This is audio response from server - need to play it back
    handleBinaryMessage(payload, length);
    break;

  case WStype_ERROR:
    Serial.println("[WSc] WebSocket error occurred!");
    break;

  case WStype_FRAGMENT_TEXT_START:
  case WStype_FRAGMENT_BIN_START:
    Serial.println("[WSc] Fragmented message received (not implemented)");
    break;
  }
}

void handleTextMessage(const char *message)
{
  /*
   * Handle text messages received from server
   * Currently just prints them, but could be used for commands/status
   */
  Serial.print("Server message: ");
  Serial.println(message);

  // Example: Server might send "status:ready" when it's ready to receive audio
  if (strcmp(message, "status:ready") == 0)
  {
    Serial.println("Server is ready to receive audio");
  }
}

void handleBinaryMessage(uint8_t *data, size_t length)
{
  /*
   * Handle binary audio data received from server
   * This is the TTS response that should be played back through speaker
   *
   * TODO: Implement audio playback through I2S speaker output
   */
  Serial.println("Received audio response from server - playback not implemented yet");

  // In the future, this would:
  // 1. Write audio data to I2S speaker output
  // 2. Handle different audio formats (PCM, WAV, etc.)
  // 3. Manage audio buffer for smooth playback

  // Placeholder for future implementation
  // writeAudioToSpeaker(data, length);
}

// Helper functions for future improvements

void writeAudioToSpeaker(uint8_t *audio_data, size_t length)
{
  /*
   * Write audio data to speaker output
   * This function needs to be implemented for audio playback
   */
  // TODO: Implement I2S speaker output
  // This would involve setting up I2S in TX mode and writing data to output
}

void detectWakeWord()
{
  /*
   * Detect wake word in audio stream
   * This would use a lightweight wake word detection library
   *
   * TODO: Integrate wake word detection (like Porcupine or custom implementation)
   */
  // Placeholder for wake word detection
  // Currently audio streams continuously
}

void setLEDStatus(int status)
{
  /*
   * Control status LED based on system state
   *
   * TODO: Add LED control for visual feedback
   */
  // 0 = Off, 1 = WiFi connecting, 2 = Connected, 3 = Processing, 4 = Error
  // This would control an onboard LED for user feedback
}