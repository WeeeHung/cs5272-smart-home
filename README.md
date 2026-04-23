# CS5272 Smart Home (Offline Edge Actuator)

This repo is a small end-to-end system with three cooperating parts:

- `PI_voice_controller/`: Raspberry Pi voice/intent pipeline (`voice_controller.py`)
- `PI4_command_center/`: Raspberry Pi HTTP router + UDP presence/discovery (`server.py`)
- `ESP32_motors/`: ESP32 actuator firmware (servo movement + HTTP endpoints)

## Connect to the Raspberry Pi

Ping using:

```bash
ping cs5272-smart-home-ai.local
```

SSH using:

```bash
ssh cs5272smarthome@cs5272-smart-home-ai.local
```

## Build / Compile Instructions

This section lists the minimum compile/build steps for each component.

### 1) Python environment for Pi components

From repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r PI_voice_controller/requirements.txt
```

For Raspberry Pi/Linux with Python 3.13, use the alternate instructions in `PI_voice_controller/README.md`.

### 2) Build `whisper.cpp` and `llama.cpp` binaries

From repo root (if not already built):

```bash
cmake -S whisper.cpp -B whisper.cpp/build
cmake --build whisper.cpp/build -j
cmake -S llama.cpp -B llama.cpp/build
cmake --build llama.cpp/build -j
```

Expected binaries used by this project:

- `whisper.cpp/build/bin/whisper-cli`
- `llama.cpp/build/bin/llama-cli`

Also ensure your GGUF model exists at:

- `models/tinyllama-1.1b-chat.Q4_K_M.gguf`

### 3) Compile/upload ESP32 firmware

From `ESP32_motors/`:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,UploadMode=default .
arduino-cli board list
arduino-cli upload --port <your_port> --fqbn 'esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,UploadMode=default' .
```

If your board profile differs, adjust `--fqbn` accordingly. The detailed ESP32 commands are documented in `ESP32_motors/README.md`.

### 4) Upload ESP32 config file to LittleFS

Set Wi-Fi credentials in `ESP32_motors/data/config.json`, then upload that filesystem image so the file is available on ESP32 as `/config.json`.

## Repo Layout (what each folder contains)

- `PI_voice_controller/`
  - `voice_controller.py`: wake-word -> record -> whisper.cpp transcription -> llama.cpp JSON intent -> trigger PI4
  - `config.example.json`: example config (copy to `config.json` next to the script)
  - `models/`: bundled wake word model files (currently `hey_homie.*`)
  - `requirements*.txt`: Python dependencies
- `PI4_command_center/`
  - `server.py`: HTTP server + UDP presence listener + location mapping persistence
  - `config.example.json`: example `esp32_nodes` and `actions` mapping (copy to `config.json`)
  - `README.md`: PI4 implementation details
  - `state.json`: persistent runtime state (created automatically by the server)
- `ESP32_motors/`
  - `ESP32_motors.ino`: ESP32 firmware (HTTP `/health`, `/command`, and UDP `ESP32_PRESENCE` beacon)
  - `data/config.json`: Wi-Fi credentials that must be uploaded into ESP32 LittleFS as `/config.json`
  - `README.md`: build/upload notes
- `TODO.md`: project TODOs

Top-level files/folders commonly included in the submission zip:

- `README.md` (compile + run guide)
- `PI_voice_controller/`
- `PI4_command_center/`
- `ESP32_motors/`
- `TODO.md`

## Current Communication Contracts (routes + payloads)

### 1) `PI_voice_controller` -> PI4 Command Center

- HTTP endpoint: `POST http://<pi4>:8080/trigger-location`
- request body (JSON): `{"location":"<location>","action":"<action_key>"}`
- what PI4 does with it:
  - normalizes `location` to lowercase
  - resolves `location -> node` from its mapping
  - resolves `action_key -> esp32 command` from `config.json`
  - discovers a reachable ESP32 host (UDP presence cache, `/health` probe, or subnet sweep)
  - forwards the actuator command to the ESP32 node

### 2) PI4 Command Center -> ESP32 Motor Node

- PI4 forwards to: `POST http://<esp32_host>:<esp32_port>/command` (ESP32 port is `80` by default)
- request body (JSON): `{"command":"TURN" | "SEQ" | "LEFT" | "RIGHT" | "CENTER" | "STATUS"}`
- ESP32 response (JSON): `{"ok":true,"command":"<COMMAND>"}` on success, or an error JSON with status `400` on bad input

- ESP32 health:
  - `GET http://<esp32_host>:80/health`
  - response (JSON): `{"ok":true,"service":"esp32-motor-node","node":"<NODE_ID>","ip":"<ip>"}`

### 3) ESP32 Node Discovery (UDP presence)

- ESP32 periodically broadcasts UDP to the PI4 network:
  - payload format (string): `ESP32_PRESENCE node=<NODE_ID> ip=<local_ip> port=80`
  - UDP port: `4210`
- PI4 runs a background listener that updates an in-memory presence cache (and persists state)

## PI4 Command Center HTTP API (what to call from other tools)

All routes run on the PI4 server created by `PI4_command_center/server.py` (default bind `0.0.0.0:8080`).

- `GET /health`
  - response: `{"ok":true,"service":"pi4-command-center"}`
- `GET /nodes`
  - response includes `nodes` (presence cache with age) and `location_map`
- `POST /map-location`
  - request (JSON): `{"node":"motor_a","location":"living_room","host":"<optional_ip>","port":80}`
  - response: mapping confirmation
- `POST /trigger-location`
  - request (JSON): `{"location":"living_room","action":"turn_demo"}`
  - response: whether the command forwarded successfully and what PI4 sent to the ESP32
- `POST /trigger`
  - request (JSON): `{"action":"turn_demo"}`
  - response: forwards the configured action directly to its configured node

## Configuration files (copy `config.example.json` -> `config.json`)

1. `PI4_command_center/config.json`
   - copy from `PI4_command_center/config.example.json`
   - defines:
     - `esp32_nodes.<node_id>.host` (optional; can be discovered)
     - `esp32_nodes.<node_id>.port` (ESP32 HTTP port; default `80`)
     - `esp32_nodes.<node_id>.discover_subnet` (used for subnet sweep fallback)
     - `actions.<action_key>.node` and `actions.<action_key>.command` (command strings like `TURN`, `LEFT`, etc)

2. `PI_voice_controller/config.json`
   - copy from `PI_voice_controller/config.example.json`
   - defines:
     - `command_center_url` (must point to `http://<pi4>:8080/trigger-location`)
     - the allowed `locations` and `actions` for llama.cpp JSON parsing
     - wake-word thresholds

3. `ESP32_motors/data/config.json`
   - this is loaded by the ESP32 firmware from LittleFS at runtime as `/config.json`
   - update `wifi_ssid` / `wifi_password` in `ESP32_motors/data/config.json`
   - then upload the filesystem image together with the sketch

## Typical Run Order (end-to-end)

1. Flash `ESP32_motors/ESP32_motors.ino` to the ESP32.
2. Confirm ESP32 is reachable on the LAN:
   - `GET http://<esp32_ip>/health` should return `service: esp32-motor-node`
   - (optional) verify UDP beacon by watching PI4 logs after it starts
3. Start PI4 Command Center:
   - `cd PI4_command_center`
   - `python3 server.py --config config.json --host 0.0.0.0 --port 8080`
4. Create at least one location mapping:
   - `POST /map-location` with `{"node":"motor_a","location":"living_room",...}`
5. Start the voice controller:
   - ensure `whisper.cpp/` and `llama.cpp/` and the models exist under the repo root (as expected by `voice_controller.py`)
   - `cd <repo_root>`
   - `cp PI_voice_controller/config.example.json PI_voice_controller/config.json`
   - `python3 PI_voice_controller/voice_controller.py`
