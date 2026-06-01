#!/usr/bin/env python3
"""
Step 04 — decode DirectSteer status (DSSTAT).

Goal: read the status word the AutoDrive logic gates on. This is where the
J1939 2-bit field trap lives.

What this step proves:
  * DSSTAT byte1/byte2 hold 2-bit status fields (00=off, 01=on, 10=err, 11=N/A)
  * "true" means the pair == 01 — you cannot use a single-bit mask
  * the two gate bits: GPS PPP available, AutoDrive allowed
  * feedback bits: AutoSteer engaged, header down, current direction, reject reason

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


def show(status: a.MachineStatus, t: float) -> None:
    print(f"[{t:4.1f}s] "
          f"PPP={yn(status.gps_ppp_available)} "
          f"allowed={yn(status.autodrive_allowed)} "
          f"engaged={yn(status.autosteer_engaged)} "
          f"header_down={yn(status.header_down)} "
          f"reverse={yn(status.current_direction_reverse)} "
          f"reject={status.reject_reason}")


def yn(b: bool) -> str:
    return "Y" if b else "-"


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    t0 = time.monotonic()
    last_print = -1.0
    while True:
        now = time.monotonic() - t0
        if now >= LISTEN_SECONDS:
            break
        frame = bus.recv(timeout=0.05)
        if frame is not None:
            a.process_frame(frame, status)
        if now - last_print >= 0.5:
            last_print = now
            show(status, now)

    print("\nThese two must both be Y before we may set SystemActive:")
    print(f"  GPS PPP available : {yn(status.gps_ppp_available)}")
    print(f"  AutoDrive allowed : {yn(status.autodrive_allowed)}")


if __name__ == "__main__":
    main()
