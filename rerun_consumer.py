#!/usr/bin/env python3
"""
Consume api_server.py state and visualize it in Rerun.

It logs live geospatial entities on a Rerun map:
  * autodrive/position       point at the machine position
  * autodrive/anchorpoint    point at the DSAP anchor
  * autodrive/machine_line   heading line starting at the machine position
  * autodrive/trail          recent machine path, bounded by --trail-seconds

Run:
    ./rerun_consumer.py
    ./rerun_consumer.py --api http://172.30.0.137:8080/state
    ./rerun_consumer.py --save autodrive.rrd --no-spawn
"""

from __future__ import annotations

import argparse
import collections
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


DEFAULT_API = "http://172.30.0.137:8080/state"
DEFAULT_POLL_HZ = 5.0
MACHINE_LINE_LENGTH_M = 8.0
DEFAULT_TRAIL_SECONDS = 1800.0


LatLon = tuple[float, float]
TrailPoint = tuple[float, LatLon]


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


def machine_line(position: LatLon, heading_deg: float | None, length_m: float) -> list[LatLon] | None:
    if heading_deg is None:
        return None
    heading_rad = math.radians(heading_deg)
    east = math.sin(heading_rad) * length_m
    north = math.cos(heading_rad) * length_m
    end_lat, end_lon = a.enu_to_wgs_approx(east, north, position[0], position[1])
    return [position, (end_lat, end_lon)]


def movement_heading_deg(previous: LatLon, current: LatLon) -> float | None:
    east, north = a.wgs_to_enu_approx(current[0], current[1], previous[0], previous[1])
    if math.hypot(east, north) < 0.05:
        return None
    return math.degrees(math.atan2(east, north)) % 360.0


def heading_from_state(position: dict[str, Any], trail: collections.deque[TrailPoint]) -> float | None:
    heading = position.get("heading_deg")
    if heading is not None:
        return float(heading)
    if len(trail) < 2:
        return None
    return movement_heading_deg(trail[-2][1], trail[-1][1])


def map_blueprint(zoom: float):
    import rerun.blueprint as rrb

    return rrb.Blueprint(
        rrb.MapView(
            origin="autodrive",
            contents="autodrive/**",
            name="AutoDrive Map",
            zoom=zoom,
            background=rrb.MapProvider.OpenStreetMap,
        ),
        collapse_panels=True,
    )


def configure_rerun(args: argparse.Namespace) -> None:
    try:
        import rerun as rr
    except Exception as exc:
        raise SystemExit("rerun-sdk missing. Enter the nix develop shell.") from exc

    blueprint = map_blueprint(args.zoom)
    rr.init("autodrive_rest_consumer", default_blueprint=blueprint)
    if args.save:
        rr.save(args.save)
    if args.connect_grpc:
        rr.connect_grpc(args.connect_grpc)
    if args.spawn:
        rr.spawn()
    rr.send_blueprint(blueprint)


def prune_trail(trail: collections.deque[TrailPoint], now_s: float, keep_s: float) -> None:
    while trail and now_s - trail[0][0] > keep_s:
        trail.popleft()


def log_to_rerun(
    state: dict[str, Any],
    line_length_m: float,
    trail: collections.deque[TrailPoint],
    heading_history: collections.deque[TrailPoint],
    trail_seconds: float,
) -> bool:
    import rerun as rr

    now_s = time.time()
    position = state.get("position") or {}
    position_latlon = latlon_from(position)
    anchor_latlon = latlon_from(state.get("anchorpoint"))
    logged_any = False

    if position_latlon is not None:
        heading_history.append((now_s, position_latlon))
        while len(heading_history) > 2:
            heading_history.popleft()

        rr.log(
            "autodrive/position",
            rr.GeoPoints(
                lat_lon=[position_latlon],
                colors=[[0, 180, 255]],
                radii=rr.Radius.ui_points(12.0),
            ),
            static=True,
        )
        logged_any = True

        if trail_seconds > 0:
            trail.append((now_s, position_latlon))
            prune_trail(trail, now_s, trail_seconds)

        line = machine_line(position_latlon, heading_from_state(position, heading_history), line_length_m)
        if line is not None:
            rr.log(
                "autodrive/machine_line",
                rr.GeoLineStrings(
                    lat_lon=line,
                    colors=[[255, 170, 0]],
                    radii=rr.Radius.ui_points(3.0),
                ),
                static=True,
            )

        if trail_seconds > 0:
            if len(trail) >= 2:
                rr.log(
                    "autodrive/trail",
                    rr.GeoLineStrings(
                        lat_lon=[point for _, point in trail],
                        colors=[[0, 120, 255]],
                        radii=rr.Radius.ui_points(2.0),
                    ),
                    static=True,
                )

    if anchor_latlon is not None:
        rr.log(
            "autodrive/anchorpoint",
            rr.GeoPoints(
                lat_lon=[anchor_latlon],
                colors=[[0, 220, 120]],
                radii=rr.Radius.ui_points(12.0),
            ),
            static=True,
        )
        logged_any = True

    return logged_any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Visualize AutoDrive REST API state in Rerun.")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API state URL (default: {DEFAULT_API})")
    parser.add_argument("--poll-hz", default=DEFAULT_POLL_HZ, type=float, help=f"Poll rate (default: {DEFAULT_POLL_HZ})")
    parser.add_argument("--timeout", default=2.0, type=float, help="HTTP request timeout in seconds")
    parser.add_argument("--line-length", default=MACHINE_LINE_LENGTH_M, type=float, help="Machine heading line length in metres")
    parser.add_argument(
        "--trail-seconds",
        default=DEFAULT_TRAIL_SECONDS,
        type=float,
        help=f"Seconds of recent position trail to keep; use 0 to disable (default: {DEFAULT_TRAIL_SECONDS})",
    )
    parser.add_argument("--zoom", default=18.0, type=float, help="Initial Rerun map zoom level")
    parser.add_argument("--connect-grpc", help="Connect to an existing Rerun gRPC server URL")
    parser.add_argument("--save", help="Write a Rerun .rrd recording instead of only viewing live")
    parser.add_argument("--no-spawn", dest="spawn", action="store_false", help="Do not spawn the Rerun viewer")
    parser.set_defaults(spawn=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.poll_hz <= 0:
        raise SystemExit("--poll-hz must be greater than zero")
    if args.trail_seconds < 0:
        raise SystemExit("--trail-seconds must be zero or greater")

    configure_rerun(args)
    poll_period_s = 1.0 / args.poll_hz
    trail: collections.deque[TrailPoint] = collections.deque()
    heading_history: collections.deque[TrailPoint] = collections.deque()

    print(f"Reading {args.api}", file=sys.stderr)
    print("Logging geospatial data to a Rerun map. Press Ctrl-C to stop.", file=sys.stderr)

    while True:
        try:
            state = fetch_state(args.api, args.timeout)
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"API read failed: {exc}", file=sys.stderr)
            time.sleep(poll_period_s)
            continue

        log_to_rerun(state, args.line_length, trail, heading_history, args.trail_seconds)

        time.sleep(poll_period_s)


if __name__ == "__main__":
    main()
