#!/usr/bin/env python3
"""
Step 08 — stream a rolling window of waypoints (ADWPI).

Goal: once anchored, send the route as ADWPI frames using the 100-point window
with 3-point overlap and 10 ms pacing.

What this step proves:
  * batching: stream the next ~100 points, overlapping the previous batch by 3
  * pacing: pause 10 ms after each frame (100 points ≈ 1 second)
  * the "engage after ≥100 points streamed" rule lives here

The route is a built-in straight test line (routes.straight_line) — no planner.

Run:
    ./08_stream_waypoints.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a
import routes

DATUM_LAT, DATUM_LON = 51.0, 5.0


def build_waypoints(route, anchor_lat, anchor_lon):
    anchor_e, anchor_n = a.wgs_to_enu_approx(anchor_lat, anchor_lon, DATUM_LAT, DATUM_LON)
    return [a.Waypoint(index=i,
                       east_cm=round((p.x - anchor_e) * 100.0),
                       north_cm=round((p.y - anchor_n) * 100.0),
                       is_headland=p.is_headland, is_reverse=p.is_reverse)
            for i, p in enumerate(route)]


def stream_window(bus, status, waypoints, current_index):
    """Send waypoints[current-overlap : current+100], 10 ms apart."""
    start = max(0, current_index - a.WINDOW_OVERLAP_POINTS)
    end = min(len(waypoints), current_index + a.FUTURE_POINT_COUNT)
    sent = 0
    for wp in waypoints[start:end]:
        a.drain_rx(bus, status, max_frames=5)
        a.send(bus, a.PGN_ADWPI, a.encode_adwpi(wp))
        sent += 1
        if a.SEND_INTERVAL_S > 0:
            time.sleep(a.SEND_INTERVAL_S)
    return start, end, sent


def main() -> None:
    route = routes.straight_line()
    bus = a.make_bus()
    status = a.MachineStatus()

    # Activate so the Display gives us an anchor.
    t0 = time.monotonic()
    while status.anchor_lat is None and time.monotonic() - t0 < 10.0:
        frame = bus.recv(timeout=0.05)
        if frame is not None:
            a.process_frame(frame, status)
        a.send(bus, a.PGN_ADJOB, a.encode_adjob(True, False, 0, len(route)))

    if status.anchor_lat is None:
        print("no anchor — cannot stream. Is the Display/simulator running, and "
              "are PPP + AutoDrive-allowed set? (run step 06 first)")
        return

    print(f"anchored at {status.anchor_lat:.7f},{status.anchor_lon:.7f}; "
          f"route has {len(route)} points\n")

    waypoints = build_waypoints(route, status.anchor_lat, status.anchor_lon)
    start, end, sent = stream_window(bus, status, waypoints, current_index=0)

    print(f"streamed first window: indices [{start}..{end - 1}], {sent} frames "
          f"(~{sent * a.SEND_INTERVAL_S:.1f}s of bus time)")
    print("first 3 points already passed are re-sent as overlap on the next window.")
    print(f"\n{sent} ≥ 100 points streamed → RunCommand is now allowed (step 09).")


if __name__ == "__main__":
    main()
