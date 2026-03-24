# PI4 Command Center (Sample Local Server)

This is a sample server that receives an action request and forwards a command to an ESP32 motor node over your local network.

## Files
- `server.py` - HTTP command center proxy server
- `config.example.json` - action-to-ESP32 mapping example

## 1) Prepare config

```bash
cd "/Users/weehungchiam/Documents/NUS_Y4/Sem 2/CS5272 Embedded Software Design/PI4_command_center"
cp config.example.json config.json
```

Edit `config.json`:
- set ESP32 IP in `esp32_nodes.motor_a.host`
- keep or change action mappings under `actions`

## 2) Run server

```bash
python3 server.py --config config.json --host 0.0.0.0 --port 8080
```

## 3) Trigger action from same LAN

Example request to command center:

```bash
curl -X POST "http://127.0.0.1:8080/trigger" \
  -H "Content-Type: application/json" \
  -d '{"action":"turn_demo"}'
```

If successful, server forwards to ESP32 endpoint:
- `POST http://<esp32_ip>:<esp32_port>/command`
- body: `{"command":"TURN"}` (or mapped command)

## Expected ESP32 API

Your ESP32 should expose:
- `POST /command`
- JSON body: `{"command":"TURN" | "LEFT" | "RIGHT" | "CENTER" | "SEQ"}`
- response: JSON/plain text status

## Health check

```bash
curl "http://127.0.0.1:8080/health"
```
