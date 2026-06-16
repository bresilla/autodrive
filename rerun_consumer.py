#!/usr/bin/env python3
"""
Consume api_server.py state and visualize it in Rerun.

It logs three live 2D entities:
  * autodrive/position       point at the machine position
  * autodrive/anchorpoint    point at the DSAP anchor
  * autodrive/machine_line   heading line starting at the machine position

Run:
    ./rerun_consumer.py
    ./rerun_consumer.py --api http://OTHER_MACHINE_IP:8080/state
    ./rerun_consumer.py --save autodrive.rrd --no-spawn
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a


DEFAULT_API = "http://127.0.0.1:8080/state"
DEFAULT_POLL_HZ = 5.0
MACHINE_LINE_LENGTH_M = 8.0


LatLon = tuple[float, float]
Point2D = list[float]


def fetch_state(url: str, timeout_s: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def latlon_from(section: dict[str, Any] | None) -> LatLon | None:
    if not section:
        return None
    lat = section.get("lat")
    lon = section.get("lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def choose_origin(state: dict[str, Any], current: LatLon | None) -> LatLon | None:
    anchor = latlon_from(state.get("anchorpoint"))
    if anchor is not None:
        return anchor
    return current


def to_xy(point: LatLon, origin: LatLon) -> Point2D:
    east, north = a.wgs_to_enu_approx(point[0], point[1], origin[0], origin[1])
    return [east, north]


def machine_line(position_xy: Point2D, heading_deg: float | None, length_m: float) -> list[Point2D] | None:
    if heading_deg is None:
        return None
    heading_rad = math.radians(heading_deg)
    east = math.sin(heading_rad) * length_m
    north = math.cos(heading_rad) * length_m
    return [position_xy, [position_xy[0] + east, position_xy[1] + north]]


def configure_rerun(args: argparse.Namespace) -> None:
    try:
        import rerun as rr
    except Exception as exc:
        raise SystemExit("rerun-sdk missing. Enter the nix develop shell.") from exc

    rr.init("autodrive_rest_consumer")
    if args.save:
        rr.save(args.save)
    if args.connect_grpc:
        rr.connect_grpc(args.connect_grpc)
    if args.spawn:
        rr.spawn()


def log_to_rerun(state: dict[str, Any], origin: LatLon, line_length_m: float) -> bool:
    import rerun as rr

    rr.set_time("poll", sequence=int(state.get("_sequence", 0)))
    rr.set_time("time", timestamp=time.time())

    position = state.get("position") or {}
    position_latlon = latlon_from(position)
    anchor_latlon = latlon_from(state.get("anchorpoint"))
    logged_any = False

    if position_latlon is not None:
        position_xy = to_xy(position_latlon, origin)
        rr.log(
            "autodrive/position",
            rr.Points2D(
                [position_xy],
                colors=[[0, 180, 255]],
                radii=[0.7],
                labels=["position"],
                show_labels=True,
            ),
        )
        logged_any = True

        line = machine_line(position_xy, position.get("heading_deg"), line_length_m)
        if line is not None:
            rr.log(
                "autodrive/machine_line",
                rr.LineStrips2D(
                    [line],
                    colors=[[255, 170, 0]],
                    radii=[0.25],
                    labels=["heading"],
                    show_labels=True,
                ),
            )

    if anchor_latlon is not None:
        anchor_xy = to_xy(anchor_latlon, origin)
        rr.log(
            "autodrive/anchorpoint",
            rr.Points2D(
                [anchor_xy],
                colors=[[0, 220, 120]],
                radii=[0.9],
                labels=["anchor"],
                show_labels=True,
            ),
        )
        logged_any = True

    return logged_any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Visualize AutoDrive REST API state in Rerun.")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API state URL (default: {DEFAULT_API})")
    parser.add_argument("--poll-hz", default=DEFAULT_POLL_HZ, type=float, help=f"Poll rate (default: {DEFAULT_POLL_HZ})")
    parser.add_argument("--timeout", default=2.0, type=float, help="HTTP request timeout in seconds")
    parser.add_argument("--line-length", default=MACHINE_LINE_LENGTH_M, type=float, help="Machine heading line length in metres")
    parser.add_argument("--connect-grpc", help="Connect to an existing Rerun gRPC server URL")
    parser.add_argument("--save", help="Write a Rerun .rrd recording instead of only viewing live")
    parser.add_argument("--no-spawn", dest="spawn", action="store_false", help="Do not spawn the Rerun viewer")
    parser.set_defaults(spawn=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.poll_hz <= 0:
        raise SystemExit("--poll-hz must be greater than zero")

    configure_rerun(args)
    poll_period_s = 1.0 / args.poll_hz
    origin: LatLon | None = None
    sequence = 0

    print(f"Reading {args.api}", file=sys.stderr)
    print("Logging to Rerun. Press Ctrl-C to stop.", file=sys.stderr)

    while True:
        try:
            state = fetch_state(args.api, args.timeout)
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"API read failed: {exc}", file=sys.stderr)
            time.sleep(poll_period_s)
            continue

        position_latlon = latlon_from(state.get("position"))
        if origin is None:
            origin = choose_origin(state, position_latlon)
            if origin is not None:
                print(f"Rerun origin lat={origin[0]:.7f} lon={origin[1]:.7f}", file=sys.stderr)

        if origin is not None:
            state["_sequence"] = sequence
            log_to_rerun(state, origin, args.line_length)
            sequence += 1

        time.sleep(poll_period_s)


if __name__ == "__main__":
    main()
