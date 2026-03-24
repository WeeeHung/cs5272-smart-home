#!/usr/bin/env python3
"""
Simple local command center proxy for ESP32 motor nodes.

This server receives local trigger requests and forwards them to an ESP32 over LAN.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple


LOGGER = logging.getLogger("command_center")
PRESENCE_PORT = 4210
PRESENCE_CACHE: Dict[str, Dict[str, Any]] = {}
PRESENCE_LOCK = threading.Lock()
LOCATION_MAP: Dict[str, str] = {}
LOCATION_LOCK = threading.Lock()
STATE_FILE = Path("state.json")


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load JSON config and enforce required top-level keys."""
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if "esp32_nodes" not in data or "actions" not in data:
        raise ValueError("Config must contain 'esp32_nodes' and 'actions'.")
    return data


def save_state(config: Dict[str, Any], state_path: Path = STATE_FILE) -> None:
    """Persist presence cache, location mapping, and learned node hosts to disk."""
    with PRESENCE_LOCK:
        presence_snapshot = dict(PRESENCE_CACHE)
    with LOCATION_LOCK:
        location_snapshot = dict(LOCATION_MAP)
    nodes_snapshot = dict(config.get("esp32_nodes", {}))

    state_data = {
        "presence_cache": presence_snapshot,
        "location_map": location_snapshot,
        "esp32_nodes": nodes_snapshot,
        "saved_at": time.time(),
    }
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def load_state(config: Dict[str, Any], state_path: Path = STATE_FILE) -> None:
    """Load persisted state from disk into in-memory caches/config."""
    if not state_path.exists():
        return
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to load state file %s: %s", state_path, exc)
        return

    presence_data = data.get("presence_cache", {})
    if isinstance(presence_data, dict):
        with PRESENCE_LOCK:
            PRESENCE_CACHE.clear()
            PRESENCE_CACHE.update(presence_data)

    location_data = data.get("location_map", {})
    if isinstance(location_data, dict):
        with LOCATION_LOCK:
            LOCATION_MAP.clear()
            LOCATION_MAP.update(location_data)

    node_data = data.get("esp32_nodes", {})
    if isinstance(node_data, dict):
        for node_id, node_cfg in node_data.items():
            if node_id not in config["esp32_nodes"]:
                config["esp32_nodes"][node_id] = {}
            if isinstance(node_cfg, dict):
                config["esp32_nodes"][node_id].update(node_cfg)


def post_json(url: str, payload: Dict[str, Any], timeout_s: float = 12.0) -> Tuple[int, str]:
    """POST JSON and return (status_code, response_body)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            return int(resp.getcode()), response_body
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return 0, f"upstream_url_error: {exc}"
    except TimeoutError as exc:
        return 0, f"upstream_timeout: {exc}"
    except Exception as exc:
        return 0, f"upstream_error: {exc}"


def get_text(url: str, timeout_s: float = 0.35) -> Tuple[int, str]:
    """GET text URL and return (status_code, body) or (0, '')."""
    req = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.getcode()), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except Exception:
        return 0, ""


def looks_like_esp32_health(body: str) -> bool:
    """Check whether /health payload belongs to expected ESP32 motor node."""
    return "esp32-motor-node" in body or "\"service\":\"esp32-motor-node\"" in body


def parse_presence_message(msg: str) -> Dict[str, str]:
    """Parse UDP presence message into key/value dict."""
    # Example: ESP32_PRESENCE node=motor_a ip=192.168.1.50 port=80
    out: Dict[str, str] = {}
    if not msg.startswith("ESP32_PRESENCE "):
        return out
    parts = msg.split()
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            out[key] = value
    return out


def presence_listener(bind_host: str = "0.0.0.0", port: int = PRESENCE_PORT) -> None:
    """Listen for ESP32 UDP presence broadcasts and update in-memory cache."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, port))
    LOGGER.info("Presence listener started on UDP %s:%s", bind_host, port)
    while True:
        data, addr = sock.recvfrom(1024)
        msg = data.decode("utf-8", errors="replace").strip()
        parsed = parse_presence_message(msg)
        node = parsed.get("node")
        if not node:
            continue
        ip = parsed.get("ip", addr[0])
        node_port = int(parsed.get("port", "80"))
        with PRESENCE_LOCK:
            PRESENCE_CACHE[node] = {"ip": ip, "port": node_port, "updated_at": time.time()}
        save_state(CommandHandler.config)
        LOGGER.info("Presence update: node=%s ip=%s port=%s", node, ip, node_port)


def try_health_probe(host: str, port: int) -> bool:
    """Probe ESP32 /health endpoint to verify reachable node."""
    code, body = get_text(f"http://{host}:{port}/health")
    return code == 200 and looks_like_esp32_health(body)


def discover_node_host(node_id: str, node_cfg: Dict[str, Any]) -> str:
    """Resolve node host via presence cache, configured host, then subnet sweep."""
    # 1) Presence cache from UDP broadcasts.
    with PRESENCE_LOCK:
        cached = PRESENCE_CACHE.get(node_id)
    if cached and (time.time() - float(cached["updated_at"]) <= 20.0):
        LOGGER.info("Node %s resolved from presence cache: %s", node_id, cached["ip"])
        return str(cached["ip"])

    # 2) Configured host if healthy.
    host = str(node_cfg.get("host", "")).strip()
    port = int(node_cfg.get("port", 80))
    if host and try_health_probe(host, port):
        return host

    # 3) Subnet sweep fallback.
    subnet_cidr = str(node_cfg.get("discover_subnet", "")).strip()
    if not subnet_cidr:
        return host

    LOGGER.info("Sweeping subnet %s for node %s", subnet_cidr, node_id)
    network = ipaddress.ip_network(subnet_cidr, strict=False)
    for ip in network.hosts():
        ip_text = str(ip)
        if try_health_probe(ip_text, port):
            LOGGER.info("Discovered node %s at %s", node_id, ip_text)
            with PRESENCE_LOCK:
                PRESENCE_CACHE[node_id] = {"ip": ip_text, "port": port, "updated_at": time.time()}
            save_state(CommandHandler.config)
            return ip_text
    return host


def normalize_location(location: str) -> str:
    """Normalize location key for consistent lookups."""
    return location.strip().lower()


def update_location_mapping(node_id: str, location: str) -> None:
    """Set or replace location->node mapping."""
    with LOCATION_LOCK:
        LOCATION_MAP[normalize_location(location)] = node_id
    save_state(CommandHandler.config)


def get_node_by_location(location: str) -> str:
    """Return mapped node id for a location, or empty string when missing."""
    with LOCATION_LOCK:
        return LOCATION_MAP.get(normalize_location(location), "")


class CommandHandler(BaseHTTPRequestHandler):
    config: Dict[str, Any] = {}

    def _json_response(self, status_code: int, payload: Dict[str, Any]) -> None:
        """Write JSON response with given status code."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json_response(HTTPStatus.OK, {"ok": True, "service": "pi4-command-center"})
            return
        if self.path == "/nodes":
            # Return recent presence cache entries for visibility/debugging.
            now = time.time()
            with LOCATION_LOCK:
                location_snapshot = dict(LOCATION_MAP)
            with PRESENCE_LOCK:
                snapshot = {
                    node_id: {
                        "ip": node_data["ip"],
                        "port": node_data["port"],
                        "age_s": round(now - float(node_data["updated_at"]), 2),
                        "location": next(
                            (
                                loc
                                for loc, mapped_node in location_snapshot.items()
                                if mapped_node == node_id
                            ),
                            None,
                        ),
                    }
                    for node_id, node_data in PRESENCE_CACHE.items()
                }
            self._json_response(
                HTTPStatus.OK,
                {"ok": True, "nodes": snapshot, "location_map": location_snapshot},
            )
            return
        self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """Accept trigger requests and forward mapped command to target ESP32."""
        if self.path == "/map-location":
            self._handle_map_location()
            return
        if self.path == "/trigger-location":
            self._handle_trigger_location()
            return
        if self.path != "/trigger":
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        content_len = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return

        action_key = str(payload.get("action", "")).strip()
        if not action_key:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_action"})
            return

        action_map = self.config["actions"].get(action_key)
        if action_map is None:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "unknown_action", "action": action_key},
            )
            return

        node_id = action_map.get("node")
        esp32_command = action_map.get("command")
        node = self.config["esp32_nodes"].get(node_id)
        if node is None:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "unknown_node", "node": node_id},
            )
            return

        host = discover_node_host(str(node_id), node)
        if not host:
            self._json_response(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": "node_unreachable", "node": node_id},
            )
            return

        node["host"] = host
        esp32_url = f"http://{host}:{node['port']}/command"
        forward_payload = {"command": esp32_command}
        LOGGER.info("Action=%s -> Node=%s -> %s", action_key, node_id, forward_payload)

        status_code, body = post_json(esp32_url, forward_payload)
        ok = 200 <= status_code < 300
        self._json_response(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "ok": ok,
                "action": action_key,
                "node": node_id,
                "forwarded_to": esp32_url,
                "esp32_status": status_code,
                "esp32_response": body,
            },
        )

    def _read_json_body(self) -> Dict[str, Any]:
        content_len = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _handle_map_location(self) -> None:
        """Set location->node mapping (optionally set node host/ip too)."""
        payload = self._read_json_body()
        node_id = str(payload.get("node", "")).strip()
        location = str(payload.get("location", "")).strip()
        host = str(payload.get("host", "")).strip()
        port = int(payload.get("port", 80))

        if not node_id or not location:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_node_or_location"},
            )
            return

        if node_id not in self.config["esp32_nodes"]:
            self.config["esp32_nodes"][node_id] = {"host": "", "port": port}
        if host:
            self.config["esp32_nodes"][node_id]["host"] = host
        self.config["esp32_nodes"][node_id]["port"] = port

        update_location_mapping(node_id, location)
        save_state(self.config)
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "node": node_id,
                "location": normalize_location(location),
                "host": self.config["esp32_nodes"][node_id].get("host", ""),
                "port": self.config["esp32_nodes"][node_id].get("port", 80),
            },
        )

    def _handle_trigger_location(self) -> None:
        """Trigger action by location -> resolve mapped node -> forward command."""
        payload = self._read_json_body()
        location = str(payload.get("location", "")).strip()
        action_key = str(payload.get("action", "")).strip()
        if not location or not action_key:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_location_or_action"},
            )
            return

        node_id = get_node_by_location(location)
        if not node_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "unknown_location", "location": location},
            )
            return

        action_map = self.config["actions"].get(action_key)
        if action_map is None:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "unknown_action", "action": action_key},
            )
            return

        node = self.config["esp32_nodes"].get(node_id)
        if node is None:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "unknown_node", "node": node_id},
            )
            return

        host = discover_node_host(node_id, node)
        if not host:
            self._json_response(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": "node_unreachable", "node": node_id},
            )
            return

        node["host"] = host
        save_state(self.config)
        esp32_url = f"http://{host}:{node['port']}/command"
        forward_payload = {"command": action_map.get("command")}
        status_code, body = post_json(esp32_url, forward_payload)
        ok = 200 <= status_code < 300
        self._json_response(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "ok": ok,
                "location": normalize_location(location),
                "node": node_id,
                "action": action_key,
                "forwarded_to": esp32_url,
                "esp32_status": status_code,
                "esp32_response": body,
            },
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    """Start command center server and background presence listener."""
    parser = argparse.ArgumentParser(description="PI4 command center proxy server")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Server bind host")
    parser.add_argument("--port", type=int, default=8080, help="Server bind port")
    parser.add_argument(
        "--state-file",
        default="state.json",
        help="Path to persistent state JSON (default: state.json)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    global STATE_FILE
    STATE_FILE = Path(args.state_file)
    cfg = load_config(Path(args.config))
    load_state(cfg, STATE_FILE)
    CommandHandler.config = cfg
    save_state(cfg, STATE_FILE)
    threading.Thread(target=presence_listener, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), CommandHandler)
    LOGGER.info("Command center listening on http://%s:%s", args.host, args.port)
    LOGGER.info("Configured nodes: %s", ", ".join(cfg["esp32_nodes"].keys()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down command center.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
