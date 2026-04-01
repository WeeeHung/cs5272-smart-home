# CS5272 Smart Home Project: Offline Edge Actuator System

A privacy-first, offline-managed hardware system designed to retrofit non-smart household appliances (like standard light switches and fans) into automated devices. This system relies entirely on local edge models (TinyML/TinyLLM) and local network protocols, ensuring zero cloud dependency and absolute user privacy.

## 🏗️ System Architecture

The project is split into two main components:

1. **The Central Brain (Raspberry Pi 4 - 8GB)**
   * **Voice Controller (`voice_controller.py`):** Uses **openWakeWord** to listen for the custom wake word ("Hey Homie"), records audio, transcribes it using **whisper.cpp**, and extracts the user's intent (location and action) using **llama.cpp** (TinyLlama-1.1B).
   * **Command Center (`server.py`):** A lightweight Python HTTP server (Port 8080) that acts as the network router. It listens for UDP beacons from the actuators to dynamically track their IP addresses and forwards the LLM's JSON commands to the correct physical device.

2. **The Actuator Nodes (ESP32)**
   * **Firmware (`ESP32_motors.ino`):** Connects to the local Wi-Fi, constantly broadcasts a UDP presence beacon (Port 4210) to the Pi, and hosts a local HTTP server (Port 80).
   * **Hardware:** Drives modular **SG90 Micro Servos** to physically flip switches or push buttons upon receiving an HTTP POST request (`LEFT`, `RIGHT`, `CENTER`, `SEQ`).

## 🔌 Hardware Requirements

* **1x** Raspberry Pi 4 (8GB RAM recommended)
* **1x** USB Microphone Module
* **1x** Mini Camera Module (for future OpenCV facial recognition integration)
* **1x** 5V 3A Power Supply
* **Multiple** ESP32 Microcontrollers (one per appliance)
* **Multiple** SG90 Micro Servo Motors
* 3D Printed Modular Actuator Housings

## 🚀 Setup & Installation

### 1. Actuator Setup (ESP32)
1. Open `ESP32_motors/ESP32_motors.ino` in the Arduino IDE.
2. Update the Wi-Fi credentials (`WIFI_SSID` and `WIFI_PASSWORD`).
3. Connect your SG90 servo to Pin 18, 5V, and GND.
4. Flash the code to the ESP32. It will automatically connect to Wi-Fi and start broadcasting its UDP beacon.

### 2. Command Center Setup (Raspberry Pi)
1. Ensure the Raspberry Pi is connected to the same local network as the ESP32s.
2. Navigate to the Command Center directory:
   ```bash
   cd PI4_command_center
   ```
3. Start the routing server:
    ```bash
    python3 server.py
    ```
### 3. AI Brain Setup (Raspberry Pi)
Install the required Python libraries inside your virtual environment:
```bash
pip install openwakeword pyaudio numpy
```
For OpenWakeWord’s TensorFlow Lite backend (`inference_framework="tflite"` in `voice_controller.py`), install a TFLite-capable runtime on the Pi as well, for example:
```bash
pip install tensorflow
```
(or use `tflite_runtime` if you prefer a smaller wheel—see the [openWakeWord](https://github.com/dscripka/openWakeWord) install notes for your platform).

#### Custom wake word model (`models/hey_homie.tflite`)
The voice pipeline listens for **“Hey Homie”** using a custom OpenWakeWord model stored as:

`PI_voice_controller/models/hey_homie.tflite`

- **Placement:** Keep the `.tflite` file in that `models/` folder. `voice_controller.py` resolves the path relative to the script, so you do not need to copy the model into the current working directory.
- **Naming:** The model basename (without `.tflite`) must match the key used in predictions. The code uses `WAKE_WORD_NAME = "hey_homie"`, so the file should be named `hey_homie.tflite`. If you replace it with another OpenWakeWord export, rename the file or update `WAKE_WORD_NAME` to match.
- **Detection threshold:** Wake detections fire when the score for `hey_homie` is above `0.5` in `voice_controller.py`; raise it if you get false triggers, or lower it slightly if it misses the phrase.

Download **whisper.cpp** binaries, **llama.cpp**, and the **tinyllama-1.1b-chat.Q4_K_M.gguf** model into your main smarthome directory (paths in `voice_controller.py` expect that layout).

Start the Voice Controller loop from the directory where your `whisper.cpp` / `llama.cpp` paths resolve (typically the repository root):

```bash
python3 PI_voice_controller/voice_controller.py
```
## 🧪 Testing the Pipeline
Before running the full system, test the individual components from your ~/smarthome/ directory to isolate any hardware or software issues.

### Test 1: Microphone Check
Verify that the Raspberry Pi is successfully capturing audio from your USB microphone.

```bash
arecord -D hw:1,0 -d 4 -f S16_LE -r 16000 test_mic.wav
```
Speak into the mic for 4 seconds, then check the playback to ensure clear audio.

### Test 2: Whisper.cpp Transcription
Pass the audio file you just recorded directly into the compiled Whisper binary.

```bash
./whisper.cpp/main -m ./whisper.cpp/models/ggml-base.en.bin -f test_mic.wav -nt
```
### Test 3: TinyLlama.cpp Intent Extraction
Test the language model's ability to parse commands into JSON without using voice input.

```bash
./llama.cpp/llama-cli -m ./models/tinyllama-1.1b-chat.Q4_K_M.gguf -n 30 --temp 0.1 -p "You are a smart home parser. Extract location and action to JSON. Command: 'turn on the living room demo'. JSON Output:"
```
Expected Output: {"location": "living_room", "action": "turn_demo"}

### Test 4: Local Server & ESP32 Actuation
Ensure your ESP32 is powered on, wired to the servo, and running the actuator firmware. With server.py running in one terminal, open a second terminal and simulate a command:

```bash
curl -X POST "[http://127.0.0.1:8080/trigger-location](http://127.0.0.1:8080/trigger-location)" \
  -H "Content-Type: application/json" \
  -d '{"location":"living_room","action":"turn_demo"}'
```
If the physical servo moves, your backend routing and hardware integration are completely functional.