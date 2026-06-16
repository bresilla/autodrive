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

The field gate is a rectangle around the selected route, so use the same route
you plan to stream later.

Run:
    ./06_activate_and_anchor.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes

RUN_SECONDS = 12.0

# Datum is set from the selected route in main(); placeholders let helpers bind.
DATUM_LAT, DATUM_LON = 0.0, 0.0
HAVE_WAYPOINTS = True   # pretend a route is loaded (step 08+ make it real)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send ADJOB when gates pass, then wait for DSAP anchor.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=RUN_SECONDS,
        help=f"seconds to run before giving up (default: {RUN_SECONDS})",
    )
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="ADJOB id to send; default is a fresh time-based u16 id",
    )
    parser.add_argument(
        "--total-points",
        type=int,
        default=None,
        help="ADJOB total point count; default is the loaded route point count",
    )
    parser.add_argument(
        "--route",
        choices=sorted(routes.ROUTES),
        default="line",
        help="route used for total point count and inside-field gate",
    )
    parser.add_argument(
        "--field-margin",
        type=float,
        default=15.0,
        help="metres around the route used for the inside-field gate",
    )
    return parser.parse_args()


def inside_field(status: a.MachineStatus, field: list[tuple[float, float]]) -> bool:
    if status.gps_lat is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, DATUM_LAT, DATUM_LON)
    return a.point_inside_polygon(x, y, field)


def ready_to_activate(status: a.MachineStatus, field: list[tuple[float, float]]) -> bool:
    return (status.gps_ppp_available
            and status.autodrive_allowed
            and inside_field(status, field)
            and HAVE_WAYPOINTS)


def main() -> None:
    global DATUM_LAT, DATUM_LON
    args = parse_args()
    route, DATUM_LAT, DATUM_LON = routes.ROUTES[args.route]()
    field = routes.bounding_field(route, args.field_margin)
    total_points = args.total_points if args.total_points is not None else len(route)
    bus = a.make_bus()
    status = a.MachineStatus()
    job_id = args.job_id if args.job_id is not None else int(time.time()) % (a.PROTOCOL_U16_MAX + 1)

    active = False
    announced_anchor = False
    announced_request = False
    last_dsstat: bytes | None = None
    t0 = time.monotonic()
    last_adjob = -999.0

    print(f"activation/anchor test on {a.CAN_BUS}: job_id={job_id} route={args.route!r} total_points={total_points}")
    print("waiting for gate, then sending ADJOB SystemActive=true to request DSAP anchor")

    while True:
        now = time.monotonic() - t0
        if now >= args.timeout:
            break
        frame = bus.recv(timeout=0.02)
        if frame is not None:
            pgn = a.process_frame(frame, status)
            if pgn == a.PGN_DSSTAT:
                last_dsstat = frame.data

        active = ready_to_activate(status, field)

        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            data = a.encode_adjob(system_active=active, run_command=False,
                                  current_index=0, total_points=total_points,
                                  job_id=job_id)
            a.send(bus, a.PGN_ADJOB, data)
            print(f"[{now:5.1f}s] ADJOB systemActive={'Y' if active else '-'} "
                  f"run=- job_id={job_id} total_points={total_points} "
                  f"(ppp={yn(status.gps_ppp_available)} allowed={yn(status.autodrive_allowed)} "
                  f"inside={yn(inside_field(status, field))} dsstat={hex_bytes(last_dsstat)})")
            if active and not announced_request:
                announced_request = True
                print("    job request sent; waiting for valid DSAP anchor")

        if active and status.anchor_lat is not None and not announced_anchor:
            announced_anchor = True
            print(f"\n  ✓ anchor received: lat={status.anchor_lat:.7f} "
                  f"lon={status.anchor_lon:.7f}")
            print("    we may now start streaming waypoints (step 07/08).\n")

    if not announced_anchor:
        print("\nno valid anchor received — check the gate conditions and DSAP payload above.")


def yn(b: bool) -> str:
    return "Y" if b else "-"


def hex_bytes(data: bytes | None) -> str:
    if data is None:
        return "none"
    return " ".join(f"{b:02X}" for b in data)


if __name__ == "__main__":
    main()
