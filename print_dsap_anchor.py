#!/usr/bin/env python3
"""
Print the DSAP anchor point.

Passive mode:
    ./print_dsap_anchor.py

Request mode, useful on the bench or when nothing else has activated the job:
    ./print_dsap_anchor.py --request
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print DSAP anchor lat/lon from CAN.")
    parser.add_argument(
        "--request",
        action="store_true",
        help="send ADJOB systemActive=true while waiting, to request a DSAP anchor",
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
        default=1,
        help="ADJOB id used with --request",
    )
    parser.add_argument(
        "--total-points",
        type=int,
        default=0,
        help="ADJOB total point count used with --request",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bus = a.make_bus()
    status = a.MachineStatus()

    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    last_request = -999.0

    print(f"listening on {a.CAN_BUS} for DSAP anchor (PGN 0x{a.PGN_DSAP:04X})...")

    while deadline is None or time.monotonic() < deadline:
        now = time.monotonic()
        if args.request and now - last_request >= a.ADJOB_PERIOD_S:
            last_request = now
            a.send(bus, a.PGN_ADJOB, a.encode_adjob(
                system_active=True,
                run_command=False,
                current_index=0,
                total_points=args.total_points,
                job_id=args.job_id,
            ))

        frame = bus.recv(timeout=0.05)
        if frame is None:
            continue

        pgn = a.process_frame(frame, status)
        if pgn != a.PGN_DSAP:
            continue

        print(a.format_frame("RX", frame))
        lat_raw = struct.unpack_from("<I", frame.data, 0)[0]
        lon_raw = struct.unpack_from("<I", frame.data, 4)[0]
        print(f"raw_anchor_lat=0x{lat_raw:08X} decoded={a.decode_latlon_u32(lat_raw)}")
        print(f"raw_anchor_lon=0x{lon_raw:08X} decoded={a.decode_latlon_u32(lon_raw)}")
        if status.anchor_lat is None or status.anchor_lon is None:
            print("DSAP received, but anchor lat/lon is not a valid field anchor yet")
            return

        print(f"anchor_lat={status.anchor_lat:.7f}")
        print(f"anchor_lon={status.anchor_lon:.7f}")
        return

    print("no DSAP anchor received")


if __name__ == "__main__":
    main()
