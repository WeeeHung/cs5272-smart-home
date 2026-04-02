# CS5272 Smart Home Project: Offline Edge Actuator System

A privacy-first, offline-managed hardware system designed to retrofit non-smart household appliances (like standard light switches and fans) into automated devices. This system relies entirely on local edge models (TinyML/TinyLLM) and local network protocols, ensuring zero cloud dependency and absolute user privacy.

## 🏗️ System Architecture

The project is split into two main components:

1. **The Central Brain (Raspberry Pi 4 - 8GB)**
  - **Voice Controller (`voice_controller.py`):** Uses **openWakeWord** to listen for the custom wake word ("Hey Homie"), records audio, transcribes it using **whisper.cpp**, and extracts the user's intent (location and action) using **llama.cpp** (TinyLlama-1.1B).
  - **Command Center (`server.py`):** A lightweight Python HTTP server (Port 8080) that acts as the network router. It listens for UDP beacons from the actuators to dynamically track their IP addresses and forwards the LLM's JSON commands to the correct physical device.
2. **The Actuator Nodes (ESP32)**
  - **Firmware (`ESP32_motors.ino`):** Connects to the local Wi-Fi, constantly broadcasts a UDP presence beacon (Port 4210) to the Pi, and hosts a local HTTP server (Port 80).
  - **Hardware:** Drives modular **SG90 Micro Servos** to physically flip switches or push buttons upon receiving an HTTP POST request (`LEFT`, `RIGHT`, `CENTER`, `SEQ`).

## 🔌 Hardware Requirements

- **1x** Raspberry Pi 4 (8GB RAM recommended)
- **1x** USB Microphone Module
- **1x** Mini Camera Module (for future OpenCV facial recognition integration)
- **1x** 5V 3A Power Supply
- **Multiple** ESP32 Microcontrollers (one per appliance)
- **Multiple** SG90 Micro Servo Motors
- 3D Printed Modular Actuator Housings

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
3. Copy and edit config if needed (`cp config.example.json config.json`), then start the routing server (from `PI4_command_center`):
  ```bash
  python3 server.py --config config.json --host 0.0.0.0 --port 8080
  ```

### 3. AI Brain Setup (Raspberry Pi)

Install the required Python libraries inside your virtual environment.

**Most setups** (Python 3.12 or earlier, or Windows):

```bash
pip install -r PI_voice_controller/requirements.txt
```

**Raspberry Pi / Linux with Python 3.13:** PyPI’s `openwakeword==0.6.0` requires **`tflite-runtime`** (no cp313 Linux wheels), so you stay on **0.4.x** and hit `AudioFeatures... wakeword_models`. Installing **from GitHub** fixes TFLite via **`ai-edge-litert`**, but upstream also requires **`speexdsp-ns`** on Linux, which still has **no Python 3.13 wheel**. Our voice pipeline does **not** enable Speex noise suppression, so install openWakeWord **without** pulling that dependency, then install the rest:

```bash
pip install 'openwakeword @ git+https://github.com/dscripka/openWakeWord.git' --no-deps
pip install -r PI_voice_controller/requirements-py313-linux.txt
```

(`git pull` first so `requirements-py313-linux.txt` exists on the Pi.)

**Preprocessor models:** Installing openWakeWord from Git does not always ship `melspectrogram.tflite` and `embedding_model.tflite` under `site-packages`. On first run, `voice_controller.py` downloads them from the [v0.5.1 release](https://github.com/dscripka/openWakeWord/releases/tag/v0.5.1) into `PI_voice_controller/models/.openwakeword_cache/` (gitignored). The Pi needs outbound HTTPS once for that, unless you copy those two files in by hand.

See [openWakeWord](https://github.com/dscripka/openWakeWord) for other platform notes.

#### Microphone / `Invalid sample rate` (PyAudio `-9997`)

The pipeline needs **16 kHz mono** for openWakeWord and Whisper. The Pi’s **default** device often rejects **16 kHz** (`-9997`). Many **USB mics** only expose **48 kHz or 44.1 kHz** through ALSA; `voice_controller.py` now tries those rates and **resamples to 16 kHz** with SciPy.

**Check that the OS sees your USB mic**

```bash
lsusb                          # USB devices
arecord -l                     # ALSA capture cards (card N, device M → often plughw:N,M)
arecord -D plughw:1,0 -d 3 -f S16_LE -r 48000 /tmp/test.wav   # adjust card; play with aplay
```

**PortAudio indices** (what this script uses) may differ from ALSA card numbers:

```bash
python3 -c "import pyaudio as py; p=py.PyAudio(); \
  [print(i, p.get_device_info_by_index(i)['name'], int(p.get_device_info_by_index(i)['defaultSampleRate'])) \
   for i in range(p.get_device_count()) if p.get_device_info_by_index(i)['maxInputChannels']>0]; p.terminate()"
```

If auto-pick fails, force the USB line’s index:

- `PI_voice_controller/config.json`: `"input_device_index": <integer>`
- Or: `PI_VOICE_INPUT_DEVICE=<integer> python3 PI_voice_controller/voice_controller.py`

On boot, prefer the USB mic as default (optional): `sudo raspi-config` → audio, or PulseAudio/PipeWire default source.

If wake word works but **`OSError: -9985 Device unavailable`** appears when recording the command, that was usually the wake stream still holding ALSA; the controller **closes** that stream before opening the command recorder (fixed in current `voice_controller.py`).

#### Custom wake word model (`models/hey_homie.tflite`)

The voice pipeline listens for **“Hey Homie”** using a custom OpenWakeWord model stored as:

`PI_voice_controller/models/hey_homie.tflite`

- **Placement:** Keep the `.tflite` file in that `models/` folder. `voice_controller.py` resolves the path relative to the script, so you do not need to copy the model into the current working directory.
- **Naming:** The model basename (without `.tflite`) must match the key used in predictions. The code uses `WAKE_WORD_NAME = "hey_homie"`, so the file should be named `hey_homie.tflite`. If you replace it with another OpenWakeWord export, rename the file or update `WAKE_WORD_NAME` to match.
- **Detection threshold:** Wake detections fire when the score for `hey_homie` is above `0.5` in `voice_controller.py`; raise it if you get false triggers, or lower it slightly if it misses the phrase.

Place **whisper.cpp**, **llama.cpp**, and the **tinyllama-1.1b-chat.Q4_K_M.gguf** model under the **repository root** (`cs5272-smart-home/`) like this:

```
cs5272-smart-home/
  whisper.cpp/          # build → build/bin/whisper-cli; put ggml-base.en in whisper.cpp/models/
  llama.cpp/            # build → build/bin/llama-cli
  models/
    tinyllama-1.1b-chat.Q4_K_M.gguf
  PI_voice_controller/
```

`voice_controller.py` resolves those paths from the repo root, so you can run it from any working directory:

```bash
python3 PI_voice_controller/voice_controller.py
```

## 🧪 Testing the Pipeline

Before running the full system, test the individual components from the **repository root** (where `whisper.cpp/` and `llama.cpp/` live) to isolate any hardware or software issues.

### Test 1: Microphone Check

Verify that the Raspberry Pi is successfully capturing audio from your USB microphone.

```bash
arecord -l   # find your USB mic card/device, e.g. card 3 -> -D plughw:3,0
arecord -D plughw:3,0 -d 4 -f S16_LE -r 16000 test_mic.wav
```

Speak into the mic for 4 seconds, then check the playback to ensure clear audio.

### Test 2: Whisper.cpp Transcription

Pass the audio file you just recorded into the compiled Whisper CLI (run from the repo root). Newer whisper.cpp builds put the binary in `build/bin/` and use **whisper-cli** instead of deprecated `main`:

```bash
./whisper.cpp/build/bin/whisper-cli -m ./whisper.cpp/models/ggml-base.en.bin -f test_mic.wav -nt
```

If you only have `ggml-tiny.en.bin`, substitute that for the `-m` path.

### Test 3: TinyLlama.cpp Intent Extraction

Test the language model's ability to parse commands into JSON without using voice input.

```bash
./llama.cpp/build/bin/llama-cli -m ./models/tinyllama-1.1b-chat.Q4_K_M.gguf -n 30 --temp 0.1 -p "You are a smart home parser. Extract location and action to JSON. Command: 'turn on the living room demo'. JSON Output:"
```

Expected Output: {"location": "living_room", "action": "turn_demo"}

### Test 4: Local Server & ESP32 Actuation

Ensure your ESP32 is powered on, wired to the servo, and running the actuator firmware. With `server.py` running in one terminal, open a second terminal and simulate the same JSON the voice pipeline sends.

**Location mapping:** `/trigger-location` resolves a **location string** to a node using the Command Center’s persisted map (see `PI4_command_center/README.md`). If you have not mapped `living_room` yet, do that once (adjust `node`, `host`, and `port` to match your setup):

```bash
curl -X POST "http://127.0.0.1:8080/map-location" \
  -H "Content-Type: application/json" \
  -d '{"node":"motor_a","location":"living_room","host":"192.168.1.50","port":80}'
```

Then trigger by location:

```bash
curl -X POST "http://127.0.0.1:8080/trigger-location" \
  -H "Content-Type: application/json" \
  -d '{"location":"living_room","action":"turn_demo"}'
```

If the physical servo moves, your backend routing and hardware integration are working.

### Test 5: Full end-to-end test (wake word → ESP32)

This exercises the same path as normal use: **microphone → OpenWakeWord → recording → Whisper → TinyLlama → HTTP POST to Command Center → ESP32**.

**Prerequisites**

- Component tests above pass where they apply (mic, Whisper, LLM JSON, and Test 4 curl).
- `hey_homie.tflite` is in `PI_voice_controller/models/`, and Python deps (`openwakeword`, `pyaudio`, `numpy`, plus TFLite backend) are installed.
- `whisper-cli` and `llama-cli` are built under `whisper.cpp/build/bin/` and `llama.cpp/build/bin/` inside the repo; `voice_controller.py` finds them automatically from the repo root.
- ESP32 is on the LAN, firmware running, and the Command Center can reach it (UDP presence / configured host—same as Test 4).
- **Location map:** For commands that mention `living_room` or `bedroom`, the server must already map that location to a node (use `/map-location` or a restored `state.json`). The LLM prompt in `voice_controller.py` only advertises those locations and actions: `turn_demo`, `left_once`, `right_once`.

**Procedure**

1. On the Pi, from `PI4_command_center`, start the Command Center and leave it running:
   ```bash
   cd PI4_command_center
   python3 server.py --config config.json --host 0.0.0.0 --port 8080
   ```
2. In a **second** terminal, start the voice loop (any cwd; paths are fixed to the repo root):
   ```bash
   python3 PI_voice_controller/voice_controller.py
   ```
3. Wait for `Waiting for wake word 'hey homie'...`.
4. Say the wake phrase clearly, then speak a short command that the model can map to JSON, for example: *“Turn on the living room demo”* or *“Living room turn demo”* (goal: transcript + LLM output like `{"location": "living_room", "action": "turn_demo"}`).
5. Watch the terminal: wake detection → `Recording saved.` → Whisper transcript → `LLM Output:` with JSON → `Triggering command center:` → `Success! Status: 200` (or an error if routing fails).
6. **Success criteria:** HTTP 200 from the Command Center **and** the expected physical motion on the mapped ESP32.

**If something fails**

- **`unknown_location`:** Map the location with `/map-location` (or align your spoken phrase with an already-mapped location).
- **`node_unreachable`:** Fix ESP32 Wi‑Fi, `discover_subnet` / static `host` in `config.json`, or UDP presence (see `PI4_command_center/README.md`).
- **Wake word never fires:** Adjust the `0.5` threshold in `voice_controller.py`, reduce background noise, or confirm the model name matches `WAKE_WORD_NAME`.
- **Bad or empty JSON from the LLM:** Keep commands short and aligned with the allowed locations/actions; check `llama-cli` and the GGUF path.