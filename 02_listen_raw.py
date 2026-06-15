#!/usr/bin/env python3
"""
Step 02 — listen to the bus and identify messages.

Goal: receive frames and recognise which PGN each one is. No decoding of the
payload yet — just routing by message type.

What this step proves:
  * the recv() loop and timeouts
  * mapping a received id back to a known PGN name
  * which messages the Display sends us (VP1, VDS, DSSTAT, DSAP)

Bench: run ./simulator.py on vcan0 first, then this. Machine: point at can0.

Run:
    ./02_listen_raw.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a

LISTEN_SECONDS = 6.0

PGN_NAMES = {
    a.PGN_VP1: "VP1   (GPS position)",
    a.PGN_VDS: "VDS   (heading/speed)",
    a.PGN_DSSTAT: "DSSTAT(status)",
    a.PGN_DSAP: "DSAP  (anchor)",
    a.PGN_ADJOB: "ADJOB (job state)",
    a.PGN_ADWPI: "ADWPI (waypoint)",
}


def main() -> None:
    bus = a.make_bus()
    print(f"listening on {a.CAN_BUS} for ~{LISTEN_SECONDS:.0f}s ...\n")

    counts: dict[int, int] = {}
    t0 = time.monotonic()
    while time.monotonic() - t0 < LISTEN_SECONDS:
        frame = bus.recv(timeout=0.05)
        if frame is None:
            continue
        pgn = a.pgn_from_id(frame.arbitration_id)
        name = PGN_NAMES.get(pgn, f"unknown 0x{pgn:04X}")
        counts[pgn] = counts.get(pgn, 0) + 1
        if counts[pgn] <= 2:   # show the first couple of each, then just tally
            print(a.format_frame("RX", frame), "←", name)

    if not counts:
        print("no frames received. Is the Display (or ./simulator.py) running on "
              f"{a.CAN_BUS}, at the right bitrate?")
        return

    print("\nframe tally:")
    for pgn, n in sorted(counts.items()):
        print(f"  {PGN_NAMES.get(pgn, hex(pgn)):24} {n:4} frames")


if __name__ == "__main__":
    main()
