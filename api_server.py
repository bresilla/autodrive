#!/usr/bin/env python3
"""
REST API for querying live AutoDrive machine state.

The server passively listens to the configured SocketCAN bus and exposes the
latest decoded VP1/VDS/DSSTAT/DSAP state as JSON. It does not transmit ADJOB or
start/stop AutoDrive.

Run:
    ./api_server.py
    ./api_server.py --host 0.0.0.0 --port 8080 --can-bus can0

From another computer on the same network:
    curl http://MACHINE_IP:8080/state
    curl http://MACHINE_IP:8080/position
    curl http://MACHINE_IP:8080/anchorpoint
    curl http://MACHINE_IP:8080/status
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
STALE_AFTER_S = 2.0
ENDPOINTS = ["/", "/state", "/position", "/anchorpoint", "/status", "/health"]


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def nullable_pair(lat: float | None, lon: float | None) -> dict[str, float | None]:
    return {"lat": lat, "lon": lon}


class SharedState:
    def __init__(self, can_bus: str):
        self.can_bus = can_bus
        self.started_mono = time.monotonic()
        self.status = a.MachineStatus()
        self.lock = threading.Lock()
        self.frames_seen = 0
        self.last_pgn: int | None = None
        self.can_error: str | None = None

    def update_from_frame(self, frame: a.CanFrame) -> None:
        with self.lock:
            self.last_pgn = a.process_frame(frame, self.status)
            self.frames_seen += 1
            self.can_error = None

    def set_can_error(self, error: BaseException) -> None:
        with self.lock:
            self.can_error = f"{type(error).__name__}: {error}"

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        with self.lock:
            status = copy.copy(self.status)
            frames_seen = self.frames_seen
            last_pgn = self.last_pgn
            can_error = self.can_error

        last_rx_age_s = None
        if status.last_rx_s:
            last_rx_age_s = max(0.0, now - status.last_rx_s)

        online = last_rx_age_s is not None and last_rx_age_s <= STALE_AFTER_S
        return {
            "timestamp": iso_now(),
            "can_bus": self.can_bus,
            "online": online,
            "uptime_s": round(now - self.started_mono, 3),
            "frames_seen": frames_seen,
            "last_pgn": None if last_pgn is None else f"0x{last_pgn:04X}",
            "last_rx_age_s": None if last_rx_age_s is None else round(last_rx_age_s, 3),
            "can_error": can_error,
            "position": {
                **nullable_pair(status.gps_lat, status.gps_lon),
                "heading_deg": status.heading_deg,
                "speed_kph": status.speed_kph,
            },
            "anchorpoint": nullable_pair(status.anchor_lat, status.anchor_lon),
            "status": {
                "gps_ppp_available": status.gps_ppp_available,
                "autodrive_allowed": status.autodrive_allowed,
                "autodrive_engaged": status.autodrive_engaged,
                "header_down": status.header_down,
                "current_direction_reverse": status.current_direction_reverse,
                "reject_reason": status.reject_reason,
            },
        }


def can_reader(shared: SharedState, stop_event: threading.Event) -> None:
    try:
        bus = a.make_bus(shared.can_bus)
    except BaseException as exc:
        shared.set_can_error(exc)
        return

    while not stop_event.is_set():
        try:
            frame = bus.recv(timeout=0.1)
        except Exception as exc:
            shared.set_can_error(exc)
            time.sleep(0.5)
            continue
        if frame is not None:
            shared.update_from_frame(frame)


def make_handler(shared: SharedState):
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "AutoDriveApi/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_common_headers()
            self.end_headers()

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            snapshot = shared.snapshot()

            if path == "/":
                body: object = {
                    "name": "AutoDrive REST API",
                    "endpoints": ["/state", "/position", "/anchorpoint", "/status", "/health"],
                }
            elif path == "/state":
                body = snapshot
            elif path == "/position":
                body = snapshot["position"]
            elif path == "/anchorpoint":
                body = snapshot["anchorpoint"]
            elif path == "/status":
                body = snapshot["status"]
            elif path == "/health":
                if snapshot["online"] and not snapshot["can_error"]:
                    status_code = HTTPStatus.OK
                else:
                    status_code = HTTPStatus.SERVICE_UNAVAILABLE
                self._send_json(snapshot, status_code)
                return
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return

            self._send_json(body, HTTPStatus.OK)

        def _send_json(self, body: object, status_code: HTTPStatus) -> None:
            data = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status_code)
            self._send_common_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_common_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")

    return ApiHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expose live AutoDrive CAN state over HTTP JSON.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--can-bus", default=a.CAN_BUS, help=f"SocketCAN interface (default: {a.CAN_BUS})")
    return parser.parse_args()


def display_base_url(host: str, port: int) -> str:
    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    return f"http://{display_host}:{port}"


def print_startup_banner(host: str, port: int, can_bus: str) -> None:
    base_url = display_base_url(host, port)
    print(f"AutoDrive REST API listening on http://{host}:{port}", file=sys.stderr)
    print(f"Reading CAN bus {can_bus!r}. Press Ctrl-C to stop.", file=sys.stderr)
    print("Available REST endpoints:", file=sys.stderr)
    for endpoint in ENDPOINTS:
        print(f"  {base_url}{endpoint}", file=sys.stderr)


def main() -> None:
    args = parse_args()
    shared = SharedState(args.can_bus)
    stop_event = threading.Event()
    reader = threading.Thread(target=can_reader, args=(shared, stop_event), daemon=True)
    reader.start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(shared))
    print_startup_banner(args.host, args.port, args.can_bus)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        reader.join(timeout=1.0)


if __name__ == "__main__":
    main()
