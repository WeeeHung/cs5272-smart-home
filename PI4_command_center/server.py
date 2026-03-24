#!/usr/bin/env python3
"""
Simple local command center proxy for ESP32 motor nodes.

This server receives local trigger requests and forwards them to an ESP32 over LAN.
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple


LOGGER = logging.getLogger("command_center")


def load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if "esp32_nodes" not in data or "actions" not in data:
        raise ValueError("Config must contain 'esp32_nodes' and 'actions'.")
    return data


def post_json(url: str, payload: Dict[str, Any], timeout_s: float = 3.0) -> Tuple[int, str]:
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


class CommandHandler(BaseHTTPRequestHandler):
    config: Dict[str, Any] = {}

    def _json_response(self, status_code: int, payload: Dict[str, Any]) -> None:
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
        self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
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

        esp32_url = f"http://{node['host']}:{node['port']}/command"
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

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="PI4 command center proxy server")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Server bind host")
    parser.add_argument("--port", type=int, default=8080, help="Server bind port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    CommandHandler.config = cfg

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
