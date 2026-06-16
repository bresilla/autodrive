#!/usr/bin/env python3
"""
Step 08 — stream a rolling window of waypoints (ADWPI).

Goal: once anchored, send the route as ADWPI frames. The route is interpolated
to at least one point per metre by default, then every point is streamed with
10 ms pacing.

What this step proves:
  * full route: stream all waypoints by default
  * optional batching: use --batch-size to cap ADWPI frames per batch
  * pacing: pause 10 ms after each frame
  * the "engage after ≥100 points streamed" rule lives here

The route is loaded from GeoJSON and resampled so long straight LineString
segments get interpolated before ADWPI frames are sent.

Run:
    ./08_stream_waypoints.py
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes

DEFAULT_MAX_SPACING_M = 1.0         # interpolate route points at least every metre
DEFAULT_BATCH_SIZE = 0              # 0 = stream the whole route in one batch
MIN_SPACING_M = 0.3                 # AgJunction minimum point spacing (PROTOCOL.md §8.5)
FIELD_MARGIN_M = 15.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Request anchor, then stream all ADWPI waypoints.")
    parser.add_argument(
        "--route",
        choices=sorted(routes.ROUTES),
        default="line",
        help="route to stream",
    )
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="ADJOB id to send; default is a fresh time-based u16 id",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="seconds to wait for a valid DSAP anchor before giving up",
    )
    parser.add_argument(
        "--no-inside-gate",
        action="store_true",
        help="do not require the machine position to be inside the route field",
    )
    parser.add_argument(
        "--max-spacing",
        type=float,
        default=DEFAULT_MAX_SPACING_M,
        help=f"maximum route spacing in metres after interpolation (default: {DEFAULT_MAX_SPACING_M})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="maximum ADWPI frames per batch, including overlap; 0 means all points (default: 0)",
    )
    return parser.parse_args()


def inside_field(status, field, datum_lat, datum_lon) -> bool:
    if status.gps_lat is None or status.gps_lon is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, datum_lat, datum_lon)
    return a.point_inside_polygon(x, y, field)


def route_for_max_spacing(route_name: str, max_spacing_m: float):
    """Load a GeoJSON route resampled so adjacent points are at most max_spacing_m."""
    path = routes.geojson_path(route_name)
    route, dlat, dlon = routes.geojson_route(path, spacing_m=max_spacing_m)
    return path, route, dlat, dlon


def max_route_spacing(route) -> float:
    if len(route) < 2:
        return 0.0
    return max(math.hypot(p1.x - p0.x, p1.y - p0.y)
               for p0, p1 in zip(route, route[1:]))


def load_line_from_anchor(path, spacing_m, anchor_lat, anchor_lon):
    """Read line.geojson with the machine anchor as ENU origin."""
    return routes.geojson_route(path, spacing_m=spacing_m,
                                datum_lat=anchor_lat, datum_lon=anchor_lon)[0]


def build_waypoints(route):
    return [a.Waypoint(index=i,
                       east_cm=round(p.x * 100.0),
                       north_cm=round(p.y * 100.0),
                       is_headland=p.is_headland, is_reverse=p.is_reverse)
            for i, p in enumerate(route)]


def stream_window(bus, status, waypoints, current_index, count):
    """Send one batch, including overlap, capped to count ADWPI frames."""
    start = max(0, current_index - a.WINDOW_OVERLAP_POINTS)
    end = min(len(waypoints), start + count)
    sent = 0
    for wp in waypoints[start:end]:
        a.drain_rx(bus, status, max_frames=5)
        a.send(bus, a.PGN_ADWPI, a.encode_adwpi(wp))
        sent += 1
        if a.SEND_INTERVAL_S > 0:
            time.sleep(a.SEND_INTERVAL_S)
    return start, end, sent


def stream_all_waypoints(bus, status, waypoints, batch_size: int):
    """Stream every waypoint, resending a 3-point overlap between batches."""
    if batch_size == 0:
        batch_size = len(waypoints)
    current_index = 0
    batches = []
    total_frames = 0
    while current_index < len(waypoints):
        start, end, sent = stream_window(bus, status, waypoints, current_index, batch_size)
        batches.append((start, end, sent))
        total_frames += sent
        if end >= len(waypoints):
            break
        current_index = end
    return batches, total_frames


def main() -> None:
    args = parse_args()
    if args.max_spacing <= 0:
        raise SystemExit("--max-spacing must be greater than zero")
    if args.batch_size < 0:
        raise SystemExit("--batch-size must be zero or greater")

    route_path, gate_route, datum_lat, datum_lon = route_for_max_spacing(args.route, args.max_spacing)
    point_count = len(gate_route)
    actual_max_spacing = max_route_spacing(gate_route)
    if point_count > a.PROTOCOL_U16_MAX:
        raise SystemExit(f"{args.route} has {point_count} points after interpolation; "
                         f"protocol max is {a.PROTOCOL_U16_MAX}")

    field = routes.bounding_field(gate_route, FIELD_MARGIN_M)
    job_id = args.job_id if args.job_id is not None else int(time.time()) % (a.PROTOCOL_U16_MAX + 1)
    batch_label = "all" if args.batch_size == 0 else str(args.batch_size)
    print(f"{args.route} route: {point_count} points, max spacing {actual_max_spacing:.3f} m "
          f"(limit {args.max_spacing:.3f} m), batch_size={batch_label}, job_id={job_id}",
          file=sys.stderr)
    if actual_max_spacing < MIN_SPACING_M:
        print(f"note: {actual_max_spacing:.3f} m spacing is below the AgJunction {MIN_SPACING_M} m "
              f"minimum (fine for drawing the line; tighten the route to fix).", file=sys.stderr)
    bus = a.make_bus()
    status = a.MachineStatus()

    # Activate so the Display gives us an anchor.
    t0 = time.monotonic()
    last_adjob = -999.0
    active_sent = False
    while not (active_sent and status.anchor_lat is not None) and time.monotonic() - t0 < args.timeout:
        frame = bus.recv(timeout=0.05)
        if frame is not None:
            a.process_frame(frame, status)
        now = time.monotonic() - t0
        active = (status.gps_ppp_available
                  and status.autodrive_allowed
                  and (args.no_inside_gate or inside_field(status, field, datum_lat, datum_lon)))
        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            if active and not active_sent:
                active_sent = True
                status.anchor_lat = None
                status.anchor_lon = None
            a.send(bus, a.PGN_ADJOB, a.encode_adjob(
                system_active=active,
                run_command=False,
                current_index=0,
                total_points=point_count,
                job_id=job_id,
            ))
            print(f"[{now:4.1f}s] ADJOB systemActive={'Y' if active else '-'} "
                  f"job_id={job_id} total_points={point_count} "
                  f"(ppp={'Y' if status.gps_ppp_available else '-'} "
                  f"allowed={'Y' if status.autodrive_allowed else '-'} "
                  f"inside={'skip' if args.no_inside_gate else ('Y' if inside_field(status, field, datum_lat, datum_lon) else '-')})")

    if not active_sent:
        print("no active job request sent — cannot stream. Check PPP, AutoDrive allowed, "
              "and inside gate; use --no-inside-gate only for field diagnostics.")
        return

    if status.anchor_lat is None:
        print("no anchor — cannot stream. Is the Display/simulator running, and "
              "are PPP + AutoDrive-allowed set? (run step 06 first)")
        return

    print(f"anchored at {status.anchor_lat:.7f},{status.anchor_lon:.7f}; "
          f"using anchor as waypoint origin\n")

    route = load_line_from_anchor(route_path, args.max_spacing, status.anchor_lat, status.anchor_lon)
    waypoints = build_waypoints(route)
    batches, total_frames = stream_all_waypoints(bus, status, waypoints, args.batch_size)

    for i, (start, end, sent) in enumerate(batches, start=1):
        print(f"batch {i}: streamed indices [{start}..{end - 1}], {sent} frames")
    print(f"\nstreamed all {len(waypoints)} waypoints in {len(batches)} batches, "
          f"{total_frames} ADWPI frames including overlap "
          f"(~{total_frames * a.SEND_INTERVAL_S:.1f}s of bus time)")
    if total_frames >= a.FUTURE_POINT_COUNT:
        print(f"{total_frames} frames streamed (≥ {a.FUTURE_POINT_COUNT}) → RunCommand is now "
              f"allowed (step 09).")
    else:
        print(f"{total_frames} < {a.FUTURE_POINT_COUNT} frames streamed → RunCommand NOT yet "
              f"allowed (line too short).")


if __name__ == "__main__":
    main()
