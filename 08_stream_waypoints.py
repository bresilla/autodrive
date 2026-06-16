#!/usr/bin/env python3
"""
Step 08 — stream a rolling window of waypoints (ADWPI).

Goal: once anchored, send the route as ADWPI frames in one batch (TARGET_POINTS,
currently 120) with 3-point overlap and 10 ms pacing.

What this step proves:
  * batching: stream the first TARGET_POINTS, overlapping the previous batch by 3
  * pacing: pause 10 ms after each frame (120 points ≈ 1.2 seconds)
  * the "engage after ≥100 points streamed" rule lives here

The route is the short line.geojson test line, resampled finely enough to yield
TARGET_POINTS so the first batch hits that count. At 120 points on this line the
spacing dips just under the AgJunction 0.3 m minimum — fine for drawing the line.

Run:
    ./08_stream_waypoints.py
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes

TARGET_POINTS = 120                 # how many waypoints we want to stream
MIN_SPACING_M = 0.3                 # AgJunction minimum point spacing (PROTOCOL.md §8.5)
FIELD_MARGIN_M = 15.0


def inside_field(status, field, datum_lat, datum_lon) -> bool:
    if status.gps_lat is None or status.gps_lon is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, datum_lat, datum_lon)
    return a.point_inside_polygon(x, y, field)


def line_spacing_for_points(target: int):
    """Load line.geojson resampled to ~`target` points (spacing = length/(target-1)).
    Returns (route_path, point_count, spacing_m)."""
    path = routes.geojson_path("line")
    base, dlat, dlon = routes.geojson_route(path)
    length = sum(math.hypot(base[i + 1].x - base[i].x, base[i + 1].y - base[i].y)
                 for i in range(len(base) - 1))
    if length <= 0 or target < 2:
        return path, len(base), routes.WAYPOINT_SPACING_M
    spacing = length / (target - 1)
    route, dlat, dlon = routes.geojson_route(path, spacing_m=spacing)
    return path, len(route), spacing


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


def stream_window(bus, status, waypoints, current_index, count=TARGET_POINTS):
    """Send waypoints[current-overlap : current+count], 10 ms apart."""
    start = max(0, current_index - a.WINDOW_OVERLAP_POINTS)
    end = min(len(waypoints), current_index + count)
    sent = 0
    for wp in waypoints[start:end]:
        a.drain_rx(bus, status, max_frames=5)
        a.send(bus, a.PGN_ADWPI, a.encode_adwpi(wp))
        sent += 1
        if a.SEND_INTERVAL_S > 0:
            time.sleep(a.SEND_INTERVAL_S)
    return start, end, sent


def main() -> None:
    route_path, point_count, spacing = line_spacing_for_points(TARGET_POINTS)
    gate_route, datum_lat, datum_lon = routes.geojson_route(route_path, spacing_m=spacing)
    field = routes.bounding_field(gate_route, FIELD_MARGIN_M)
    job_id = int(time.time()) % (a.PROTOCOL_U16_MAX + 1)
    print(f"line route: {point_count} points at {spacing:.3f} m spacing "
          f"(target {TARGET_POINTS}), job_id={job_id}", file=sys.stderr)
    if spacing < MIN_SPACING_M:
        print(f"note: {spacing:.3f} m spacing is below the AgJunction {MIN_SPACING_M} m "
              f"minimum (fine for drawing the line; tighten the route to fix).", file=sys.stderr)
    bus = a.make_bus()
    status = a.MachineStatus()

    # Activate so the Display gives us an anchor.
    t0 = time.monotonic()
    last_adjob = -999.0
    while status.anchor_lat is None and time.monotonic() - t0 < 10.0:
        frame = bus.recv(timeout=0.05)
        if frame is not None:
            a.process_frame(frame, status)
        now = time.monotonic() - t0
        active = (status.gps_ppp_available
                  and status.autodrive_allowed
                  and inside_field(status, field, datum_lat, datum_lon))
        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            a.send(bus, a.PGN_ADJOB, a.encode_adjob(
                system_active=active,
                run_command=False,
                current_index=0,
                total_points=point_count,
                job_id=job_id,
            ))
            print(f"[{now:4.1f}s] ADJOB systemActive={'Y' if active else '-'} "
                  f"job_id={job_id} total_points={point_count}")

    if status.anchor_lat is None:
        print("no anchor — cannot stream. Is the Display/simulator running, and "
              "are PPP + AutoDrive-allowed set? (run step 06 first)")
        return

    print(f"anchored at {status.anchor_lat:.7f},{status.anchor_lon:.7f}; "
          f"using anchor as waypoint origin\n")

    route = load_line_from_anchor(route_path, spacing, status.anchor_lat, status.anchor_lon)
    waypoints = build_waypoints(route)
    start, end, sent = stream_window(bus, status, waypoints, current_index=0)

    print(f"streamed first window: indices [{start}..{end - 1}], {sent} frames "
          f"(~{sent * a.SEND_INTERVAL_S:.1f}s of bus time)")
    print("first 3 points already passed are re-sent as overlap on the next window.")
    if sent >= a.FUTURE_POINT_COUNT:
        print(f"\n{sent} points streamed (≥ {a.FUTURE_POINT_COUNT}) → RunCommand is now "
              f"allowed (step 09).")
    else:
        print(f"\n{sent} < {a.FUTURE_POINT_COUNT} points streamed → RunCommand NOT yet "
              f"allowed (line too short).")


if __name__ == "__main__":
    main()
