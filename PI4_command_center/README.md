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
- set `discover_subnet` to your LAN (example: `192.168.1.0/24`)
- keep or change action mappings under `actions`

## 2) Run server

```bash
python3 server.py --config config.json --host 0.0.0.0 --port 8080
```

Optional custom state file path:

```bash
python3 server.py --config config.json --state-file state.json
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

## 4) Location mapping workflow (multi-node)

By default, nodes have no location assigned.

### Set location -> node mapping

```bash
curl -X POST "http://127.0.0.1:8080/map-location" \
  -H "Content-Type: application/json" \
  -d '{"node":"motor_a","location":"living_room","host":"10.142.55.208","port":80}'
```

- `host` and `port` are optional if node is discoverable via presence/sweep.
- Location keys are normalized to lowercase.

### Trigger by location

```bash
curl -X POST "http://127.0.0.1:8080/trigger-location" \
  -H "Content-Type: application/json" \
  -d '{"location":"living_room","action":"turn_demo"}'
```

Server resolves:
1. location -> node
2. node -> current IP (presence cache / health probe / sweep)
3. action -> command
4. forwards to ESP32 `/command`

## Discovery behavior

Before each trigger, server resolves node IP in this order:
1. Recent ESP32 UDP presence broadcast (`ESP32_PRESENCE`) cache
2. Configured host health probe (`GET /health`)
3. Subnet sweep over `discover_subnet` probing `GET /health`

When found, that IP is used as destination for the command.

## Expected ESP32 API

Your ESP32 should expose:
- `POST /command`
- JSON body: `{"command":"TURN" | "LEFT" | "RIGHT" | "CENTER" | "SEQ"}`
- response: JSON/plain text status
- `GET /health` returning service id `esp32-motor-node`
- periodic UDP presence broadcast to port `4210` with node id and IP

## Health check

```bash
curl "http://127.0.0.1:8080/health"
```

## Inspect discovered nodes

```bash
curl "http://127.0.0.1:8080/nodes"
```

`/nodes` now also includes the current `location_map`.

## Persistent state on disk

Server now persists runtime mapping/cache to `state.json` (same folder by default):
- `location_map` (location -> node)
- `presence_cache` (node -> last seen IP/port/time)
- `esp32_nodes` (including learned/updated host info)

State is loaded on startup and updated whenever discovery/mapping changes.
