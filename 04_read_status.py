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
        - AutoDrive engaged
        - header down
        - current direction
        - AutoDrive reject/interruption reason

Expected 2-bit status encoding:
    00 = off / false
    01 = on / true
    10 = error
    11 = not available

DSSTAT layout:
    Byte 1 bits 8-7: GPS PPP available
    Byte 1 bits 6-5: AutoDrive engaged
    Byte 1 bits 4-3: Header down
    Byte 1 bits 2-1: Current direction

    Byte 2 bits 8-3: AutoDrive interrupt / reject reason
    Byte 2 bits 2-1: AutoDrive allowed

Run:
    ./04_read_status.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a

LISTEN_SECONDS = 6.0
PRINT_PERIOD = 0.5


STATE = {0b00: "00 off", 0b01: "01 ON", 0b10: "10 err", 0b11: "11 n/a"}


def yn(value: bool) -> str:
    return "Y" if value else "-"


def pair(byte: int, hi_bit: int) -> int:
    """The 2-bit field whose high bit is `hi_bit` (1-based), e.g. hi_bit=6 -> bits 6-5."""
    return (byte >> (hi_bit - 2)) & 0b11


def show(status: a.MachineStatus, t: float) -> None:
    print(
        f"[{t:4.1f}s] "
        f"PPP={yn(status.gps_ppp_available)} "
        f"allowed={yn(status.autodrive_allowed)} "
        f"engaged={yn(status.autodrive_engaged)} "
        f"header_down={yn(status.header_down)} "
        f"reverse={yn(status.current_direction_reverse)} "
        f"reject={status.reject_reason}"
    )


def show_raw(data: bytes) -> None:
    """Dump the raw DSSTAT bytes and every 2-bit field, so a wrong Y is obvious."""
    b1, b2 = data[0], data[1]
    print("          raw " + " ".join(f"{x:02X}" for x in data)
          + f"   (byte1={b1:08b} byte2={b2:08b})")
    print(f"          byte1: PPP[8-7]={STATE[pair(b1,8)]}  engaged[6-5]={STATE[pair(b1,6)]}"
          f"  header[4-3]={STATE[pair(b1,4)]}  dir[2-1]={STATE[pair(b1,2)]}")
    print(f"          byte2: allowed[2-1]={STATE[pair(b2,2)]}  reject[8-3]={(b2 >> 2) & 0x3F}")


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    t0 = time.monotonic()
    last_print = -PRINT_PERIOD
    last_dsstat: bytes | None = None

    while True:
        now = time.monotonic() - t0

        if now >= LISTEN_SECONDS:
            break

        frame = bus.recv(timeout=0.05)

        if frame is not None:
            a.process_frame(frame, status)
            if a.pgn_from_id(frame.arbitration_id) == a.PGN_DSSTAT and len(frame.data) >= 8:
                last_dsstat = frame.data

        if now - last_print >= PRINT_PERIOD:
            last_print = now
            show(status, now)
            if last_dsstat is not None:
                show_raw(last_dsstat)

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
    print(f"  AutoDrive engaged : {yn(status.autodrive_engaged)}")
    print(f"  Header down       : {yn(status.header_down)}")
    print(f"  Reverse           : {yn(status.current_direction_reverse)}")
    print(f"  Reject reason     : {status.reject_reason}")


if __name__ == "__main__":
    main()
