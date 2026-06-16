#!/usr/bin/env python3
"""
Print the DSAP anchor point.

Default mode sends ADJOB SystemActive=true first, then waits for DSAP:
    ./print_dsap_anchor.py

Passive mode only listens:
    ./print_dsap_anchor.py --passive
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print DSAP anchor lat/lon from CAN.")
    parser.add_argument(
        "--passive",
        action="store_true",
        help="only listen for DSAP; do not send ADJOB first",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="seconds to wait before giving up; use 0 to wait forever",
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
    parser.add_argument(
        "--no-inside-gate",
        action="store_true",
        help="do not require the machine position to be inside the route field",
    )
    return parser.parse_args()


def inside_field(status: a.MachineStatus, field: list[tuple[float, float]],
                 datum_lat: float, datum_lon: float) -> bool:
    if status.gps_lat is None or status.gps_lon is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, datum_lat, datum_lon)
    return a.point_inside_polygon(x, y, field)


def ready_to_activate(args: argparse.Namespace, status: a.MachineStatus,
                      field: list[tuple[float, float]], datum_lat: float,
                      datum_lon: float) -> bool:
    if not status.gps_ppp_available or not status.autodrive_allowed:
        return False
    if not args.no_inside_gate and not inside_field(status, field, datum_lat, datum_lon):
        return False
    return True


def yn(value: bool) -> str:
    return "Y" if value else "-"


def hex_bytes(data: bytes | None) -> str:
    if data is None:
        return "none"
    return " ".join(f"{b:02X}" for b in data)


def main() -> None:
    args = parse_args()
    bus = a.make_bus()
    status = a.MachineStatus()
    route, datum_lat, datum_lon = routes.ROUTES[args.route]()
    field = routes.bounding_field(route, args.field_margin)
    total_points = args.total_points if args.total_points is not None else len(route)
    job_id = args.job_id if args.job_id is not None else int(time.time()) % (a.PROTOCOL_U16_MAX + 1)

    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    last_request = -999.0
    last_dsstat: bytes | None = None

    mode = "passively listening" if args.passive else f"sending ADJOB job_id={job_id}, then listening"
    print(f"{mode} on {a.CAN_BUS} for DSAP anchor (PGN 0x{a.PGN_DSAP:04X})...")
    if not args.passive:
        print(f"route={args.route!r} total_points={total_points} inside_gate={'off' if args.no_inside_gate else 'on'}")

    while deadline is None or time.monotonic() < deadline:
        now = time.monotonic()
        if not args.passive and now - last_request >= a.ADJOB_PERIOD_S:
            last_request = now
            active = ready_to_activate(args, status, field, datum_lat, datum_lon)
            inside = inside_field(status, field, datum_lat, datum_lon)
            a.send(bus, a.PGN_ADJOB, a.encode_adjob(
                system_active=active,
                run_command=False,
                current_index=0,
                total_points=total_points,
                job_id=job_id,
            ))
            print(f"TX ADJOB systemActive={yn(active)} run=- job_id={job_id} "
                  f"total_points={total_points} "
                  f"(ppp={yn(status.gps_ppp_available)} allowed={yn(status.autodrive_allowed)} "
                  f"inside={yn(inside) if not args.no_inside_gate else 'skip'} "
                  f"dsstat={hex_bytes(last_dsstat)})")

        frame = bus.recv(timeout=0.05)
        if frame is None:
            continue

        pgn = a.process_frame(frame, status)
        if pgn == a.PGN_DSSTAT:
            last_dsstat = frame.data
        if pgn != a.PGN_DSAP:
            continue

        print(a.format_frame("RX", frame))
        lat_raw = struct.unpack_from("<I", frame.data, 0)[0]
        lon_raw = struct.unpack_from("<I", frame.data, 4)[0]
        print(f"raw_anchor_lat=0x{lat_raw:08X} decoded={a.decode_latlon_u32(lat_raw)}")
        print(f"raw_anchor_lon=0x{lon_raw:08X} decoded={a.decode_latlon_u32(lon_raw)}")
        if status.anchor_lat is None or status.anchor_lon is None:
            print("DSAP received, but anchor lat/lon is not a valid field anchor yet; still waiting")
            continue

        print(f"anchor_lat={status.anchor_lat:.7f}")
        print(f"anchor_lon={status.anchor_lon:.7f}")
        return

    print("no DSAP anchor received")


if __name__ == "__main__":
    main()
