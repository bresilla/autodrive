#!/usr/bin/env python3
"""
Step 06 — activation gate and the anchor handshake.

Goal: only flip SystemActive on once the conditions are met, then wait for the
Display to answer with a DSAP anchor point.

What this step proves:
  * the activation gate (PROTOCOL.md §7): GPS PPP available + AutoDrive allowed
    + machine inside field + waypoints available
  * SystemActive=true triggers the Display to compute an anchor and start
    broadcasting DSAP
  * RunCommand stays OFF here — we don't have waypoints streamed yet

The field here is a simple square around the start point so the inside-field
check passes in the simulator.

Run:
    ./06_activate_and_anchor.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes

RUN_SECONDS = 12.0

# Datum = the "line" route's first vertex (its start), matching the simulator and
# the later steps. On the real machine, point this at your actual field.
DATUM_LAT, DATUM_LON = routes.geojson_datum(routes.geojson_path("line"))
# A 200 m square centred on the datum (= route start), in ENU metres.
FIELD_ENU = [(-100.0, -100.0), (100.0, -100.0), (100.0, 100.0), (-100.0, 100.0)]
HAVE_WAYPOINTS = True   # pretend a route is loaded (step 08+ make it real)


def inside_field(status: a.MachineStatus) -> bool:
    if status.gps_lat is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, DATUM_LAT, DATUM_LON)
    return a.point_inside_polygon(x, y, FIELD_ENU)


def ready_to_activate(status: a.MachineStatus) -> bool:
    return (status.gps_ppp_available
            and status.autodrive_allowed
            and inside_field(status)
            and HAVE_WAYPOINTS)


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    active = False
    announced_anchor = False
    t0 = time.monotonic()
    last_adjob = -999.0

    while True:
        now = time.monotonic() - t0
        if now >= RUN_SECONDS:
            break
        frame = bus.recv(timeout=0.02)
        if frame is not None:
            a.process_frame(frame, status)

        active = ready_to_activate(status)

        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            data = a.encode_adjob(system_active=active, run_command=False,
                                  current_index=0, total_points=0)
            a.send(bus, a.PGN_ADJOB, data)
            print(f"[{now:5.1f}s] ADJOB systemActive={'Y' if active else '-'} "
                  f"(ppp={yn(status.gps_ppp_available)} allowed={yn(status.autodrive_allowed)} "
                  f"inside={yn(inside_field(status))})")

        if active and status.anchor_lat is not None and not announced_anchor:
            announced_anchor = True
            print(f"\n  ✓ anchor received: lat={status.anchor_lat:.7f} "
                  f"lon={status.anchor_lon:.7f}")
            print("    we may now start streaming waypoints (step 07/08).\n")

    if not announced_anchor:
        print("\nno anchor received — check the gate conditions above.")


def yn(b: bool) -> str:
    return "Y" if b else "-"


if __name__ == "__main__":
    main()
