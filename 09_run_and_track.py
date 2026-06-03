#!/usr/bin/env python3
"""
Step 09 — run command, progress tracking, and re-streaming.

Goal: the full control loop on the line.geojson test line. Activate, stream the
first window, raise RunCommand, track progress from GPS, and re-stream the window
as the machine advances.

What this step proves:
  * the run gate: RunCommand may turn on only after ≥100 points streamed
  * estimating the current waypoint index from GPS (monotonic nearest-point)
  * re-sending the rolling window so points ahead are always known
  * reporting progress in ADJOB's current-index field

⚠️ On a real machine the RunCommand does NOT drive it yet (PROTOCOL.md §9) — you
   drive forward on the joystick (1-2 kph, flying start) and AutoSteer only steers.
   Area clear, e-stop in hand. On the bench the simulator drives for you.

Run:
    ./09_run_and_track.py
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a
import routes

# Datum is taken from the loaded route's first vertex in main(); placeholder here.
DATUM_LAT, DATUM_LON = 0.0, 0.0
RUN_SECONDS = 60.0
RESEND_EVERY_S = 1.0
NEAREST_BACKTRACK = 3
# Keep the nearest-point search local: a window much larger than the machine's
# per-tick advance can snap onto a *parallel* leg of the route (the legs of a
# U-turn are only a few metres apart), and the monotonic clamp below makes that
# jump permanent. ~12 m of look-ahead covers any real advance between estimates.
NEAREST_AHEAD = 25


def build_waypoints(route, anchor_lat, anchor_lon):
    ae, an = a.wgs_to_enu_approx(anchor_lat, anchor_lon, DATUM_LAT, DATUM_LON)
    return [a.Waypoint(index=i, east_cm=round((p.x - ae) * 100.0),
                       north_cm=round((p.y - an) * 100.0),
                       is_headland=p.is_headland, is_reverse=p.is_reverse)
            for i, p in enumerate(route)]


def estimate_index(status, route, previous):
    if status.gps_lat is None:
        return previous
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, DATUM_LAT, DATUM_LON)
    start = max(0, previous - NEAREST_BACKTRACK)
    end = min(len(route), previous + NEAREST_AHEAD)
    best, best_d = previous, float("inf")
    for i in range(start, end):
        d = math.hypot(route[i].x - x, route[i].y - y)
        if d < best_d:
            best, best_d = i, d
    return max(previous, best)


def stream_window(bus, status, waypoints, current_index):
    """Stream the rolling window. Returns the exclusive end index reached, so the
    caller can tell how much of the line is on the bus (for the run gate)."""
    start = max(0, current_index - a.WINDOW_OVERLAP_POINTS)
    end = min(len(waypoints), current_index + a.FUTURE_POINT_COUNT)
    for wp in waypoints[start:end]:
        a.drain_rx(bus, status, max_frames=5)
        a.send(bus, a.PGN_ADWPI, a.encode_adwpi(wp))
        time.sleep(a.SEND_INTERVAL_S)
    return end


def main() -> None:
    global DATUM_LAT, DATUM_LON
    route, DATUM_LAT, DATUM_LON = routes.geojson_route(routes.geojson_path("line"))
    bus = a.make_bus()
    status = a.MachineStatus()

    run_command = False
    current_index = 0
    waypoints: list[a.Waypoint] = []
    t0 = time.monotonic()
    last_adjob = -999.0
    last_window = -999.0

    while True:
        now = time.monotonic() - t0
        if now >= RUN_SECONDS:
            break
        frame = bus.recv(timeout=0.02)
        if frame is not None:
            a.process_frame(frame, status)

        active = status.gps_ppp_available and status.autodrive_allowed

        if active and status.anchor_lat is not None and not waypoints:
            waypoints = build_waypoints(route, status.anchor_lat, status.anchor_lon)
            print(f"[{now:5.1f}s] anchored; {len(waypoints)} waypoints prepared")

        if waypoints:
            current_index = estimate_index(status, route, current_index)
            if active and (now - last_window) >= RESEND_EVERY_S:
                reached = stream_window(bus, status, waypoints, current_index)
                last_window = now
                # Run gate (PROTOCOL.md §6): the first batch — ≥100 points, or the
                # whole line if it is shorter — must be on the bus before RunCommand.
                if not run_command and reached >= min(a.FUTURE_POINT_COUNT, len(waypoints)):
                    run_command = True
                    print(f"[{now:5.1f}s] first batch streamed ({reached} pts) → RunCommand allowed")

        if (now - last_adjob) >= a.ADJOB_PERIOD_S:
            last_adjob = now
            a.send(bus, a.PGN_ADJOB, a.encode_adjob(
                system_active=active,
                run_command=active and run_command,
                current_index=current_index,
                total_points=len(waypoints) if waypoints else len(route)))
            if waypoints:
                print(f"[{now:5.1f}s] progress {current_index:3}/{len(waypoints)}  "
                      f"engaged={'Y' if status.autosteer_engaged else '-'}  "
                      f"spd={status.speed_kph:4.1f}kph")

        if waypoints and current_index >= len(waypoints) - 1:
            print(f"\n[{now:5.1f}s] reached the last waypoint — done.")
            break


if __name__ == "__main__":
    main()
