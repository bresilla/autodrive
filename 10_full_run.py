#!/usr/bin/env python3
"""
Step 10 — the full run on a complete test route (straight line or U-turn).

Goal: everything from steps 01-09 wired into one loop, driving a complete route
end to end. No planner, no maptrax — the route is a built-in test path:

    ROUTE = "line"   straight line
    ROUTE = "uturn"  two legs joined by a 180° headland turn (exercises the
                     headland flag and a real curve)

Loop (see PROTOCOL.md §7):
    wait for gates (PPP + AutoDrive allowed + inside field)
      → ADJOB systemActive=true → receive DSAP anchor
      → convert route to anchor-relative cm
      → stream ADWPI windows, raise RunCommand, track progress via ADJOB

⚠️ On a real machine THIS MOVES THE MACHINE. Area clear, e-stop in hand,
   machine placed right in front of the route's start point.

Run:
    ./10_full_run.py
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a
import routes


# =============================================================================
# CONFIG
# =============================================================================

ROUTE = "uturn"             # "line" | "uturn"  (GeoJSON files, see routes.GEOJSON)

# Datum is taken from the loaded route's first vertex at startup; these are just
# placeholders so the module-level gate helpers below have something to bind.
DATUM_LAT, DATUM_LON = 0.0, 0.0
FIELD_MARGIN_M = 15.0       # field box is the route bounding box + this margin

REQUIRE_GPS_PPP = True
REQUIRE_AUTODRIVE_ALLOWED = True
REQUIRE_INSIDE_FIELD = True

RESEND_WINDOW_EVERY_S = 1.0
NEAREST_BACKTRACK = 3
# Keep the nearest-point search local: a window much larger than the machine's
# per-tick advance can snap onto a *parallel* leg of the route (the legs of a
# U-turn are only a few metres apart), and the monotonic clamp below makes that
# jump permanent. ~12 m of look-ahead covers any real advance between estimates.
NEAREST_AHEAD = 25
LOOP_TIMEOUT_S = 120.0      # bench safety stop; set None for an unbounded run


# =============================================================================
# GATING / TRACKING
# =============================================================================

def inside_field(status, field) -> bool:
    if status.gps_lat is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, DATUM_LAT, DATUM_LON)
    return a.point_inside_polygon(x, y, field)


def ready_to_activate(status, field, have_route) -> bool:
    if REQUIRE_GPS_PPP and not status.gps_ppp_available:
        return False
    if REQUIRE_AUTODRIVE_ALLOWED and not status.autodrive_allowed:
        return False
    if REQUIRE_INSIDE_FIELD and not inside_field(status, field):
        return False
    return have_route


def route_to_waypoints(route, anchor_lat, anchor_lon):
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


# =============================================================================
# MAIN LOOP
# =============================================================================

def main() -> None:
    global DATUM_LAT, DATUM_LON
    route, DATUM_LAT, DATUM_LON = routes.ROUTES[ROUTE]()
    field = routes.bounding_field(route, FIELD_MARGIN_M)

    bus = a.make_bus()
    status = a.MachineStatus()

    run_command = False
    current_index = 0
    waypoints: list[a.Waypoint] = []
    anchor_used = None
    t0 = time.monotonic()
    last_adjob = last_window = -999.0

    print(f"full run: route={ROUTE!r} ({len(route)} pts) on {a.CAN_BUS}", file=sys.stderr)

    while LOOP_TIMEOUT_S is None or time.monotonic() - t0 < LOOP_TIMEOUT_S:
        now = time.monotonic() - t0
        frame = bus.recv(timeout=0.02)
        if frame is not None:
            a.process_frame(frame, status)

        active = ready_to_activate(status, field, have_route=bool(route))

        if active and status.anchor_lat is not None:
            anchor = (status.anchor_lat, status.anchor_lon)
            if anchor != anchor_used:
                anchor_used = anchor
                waypoints = route_to_waypoints(route, *anchor)
                current_index = 0
                last_window = -999.0
                print(f"[{now:6.1f}s] anchor {anchor[0]:.7f},{anchor[1]:.7f}; "
                      f"{len(waypoints)} ADWPI points ready", file=sys.stderr)

        if waypoints:
            current_index = estimate_index(status, route, current_index)
            if active and (now - last_window) >= RESEND_WINDOW_EVERY_S:
                reached = stream_window(bus, status, waypoints, current_index)
                last_window = time.monotonic() - t0
                # Run gate (PROTOCOL.md §6): the first batch — ≥100 points, or the
                # whole line if it is shorter — must be on the bus before RunCommand.
                if not run_command and reached >= min(a.FUTURE_POINT_COUNT, len(waypoints)):
                    run_command = True

        if (now - last_adjob) >= a.ADJOB_PERIOD_S:
            last_adjob = now
            a.send(bus, a.PGN_ADJOB, a.encode_adjob(
                system_active=active,
                run_command=active and run_command,
                current_index=current_index,
                total_points=len(waypoints) if waypoints else len(route)))
            if waypoints:
                hl = "H" if route[current_index].is_headland else "-"
                print(f"[{now:6.1f}s] active={'Y' if active else '-'} "
                      f"run={'Y' if (active and run_command) else '-'} "
                      f"engaged={'Y' if status.autosteer_engaged else '-'} "
                      f"progress {current_index}/{len(waypoints)} [{hl}] "
                      f"spd={status.speed_kph:4.1f}kph")

        if waypoints and current_index >= len(waypoints) - 1:
            print(f"\n[{now:6.1f}s] route complete.", file=sys.stderr)
            break


if __name__ == "__main__":
    main()
