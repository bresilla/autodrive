#!/usr/bin/env python3
"""
Step 04 — decode DirectSteer status (DSSTAT).

Goal:
    Read the DSSTAT frame and print the status fields used by AutoDrive.

What this step proves:
    * DSSTAT byte 1 and byte 2 contain packed 2-bit status fields.
    * A 2-bit boolean is only true when the value is 01.
    * Do not decode these fields with a single-bit mask.
    * Gate conditions:
        - GPS PPP available
        - AutoDrive allowed
    * Feedback/status:
        - AutoSteer engaged
        - header down
        - current direction
        - AutoSteer reject/interruption reason

Expected 2-bit status encoding:
    00 = off / false
    01 = on / true
    10 = error
    11 = not available

DSSTAT layout:
    Byte 1 bits 8-7: GPS PPP available
    Byte 1 bits 6-5: AutoSteer engaged
    Byte 1 bits 4-3: Header down
    Byte 1 bits 2-1: Current direction

    Byte 2 bits 8-3: AutoSteer interrupt / reject reason
    Byte 2 bits 2-1: AutoDrive allowed

Run:
    ./04_read_status.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a

LISTEN_SECONDS = 6.0
PRINT_PERIOD = 0.5


def yn(value: bool) -> str:
    return "Y" if value else "-"


def show(status: a.MachineStatus, t: float) -> None:
    print(
        f"[{t:4.1f}s] "
        f"PPP={yn(status.gps_ppp_available)} "
        f"allowed={yn(status.autodrive_allowed)} "
        f"engaged={yn(status.autosteer_engaged)} "
        f"header_down={yn(status.header_down)} "
        f"reverse={yn(status.current_direction_reverse)} "
        f"reject={status.reject_reason}"
    )


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    t0 = time.monotonic()
    last_print = -PRINT_PERIOD

    while True:
        now = time.monotonic() - t0

        if now >= LISTEN_SECONDS:
            break

        frame = bus.recv(timeout=0.05)

        if frame is not None:
            a.process_frame(frame, status)

        if now - last_print >= PRINT_PERIOD:
            last_print = now
            show(status, now)

    print()
    print("Gate check before setting SystemActive:")
    print(f"  GPS PPP available : {yn(status.gps_ppp_available)}")
    print(f"  AutoDrive allowed : {yn(status.autodrive_allowed)}")

    if status.gps_ppp_available and status.autodrive_allowed:
        print("  Result            : OK, SystemActive may be set")
    else:
        print("  Result            : NOT OK, do not set SystemActive")

    print()
    print("Feedback:")
    print(f"  AutoSteer engaged : {yn(status.autosteer_engaged)}")
    print(f"  Header down       : {yn(status.header_down)}")
    print(f"  Reverse           : {yn(status.current_direction_reverse)}")
    print(f"  Reject reason     : {status.reject_reason}")


if __name__ == "__main__":
    main()
