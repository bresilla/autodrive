#!/usr/bin/env python3
"""
Step 03 — decode the machine GPS position (VP1).

Goal: turn the VP1 payload into latitude/longitude degrees, and handle the
"signal unavailable" case.

What this step proves:
  * the lat/lon u32 encoding: degrees = raw * 1e-7 - 210
  * 0xFFFFFFFF means "no fix" (None here)
  * VDS gives heading and speed alongside

With the simulator, GPS PPP is acquired a few seconds in, so the first frames
report "no fix" — exactly like a real receiver warming up.

Run:
    ./03_get_location.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a

LISTEN_SECONDS = 6.0


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    t0 = time.monotonic()
    last_print = 0.0
    last_vds: bytes | None = None
    while True:
        now = time.monotonic() - t0
        if now >= LISTEN_SECONDS:
            break
        frame = bus.recv(timeout=0.05)
        if frame is not None:
            pgn = a.process_frame(frame, status)
            if pgn == a.PGN_VDS:
                last_vds = frame.data

        if now - last_print >= 0.5:
            last_print = now
            if status.gps_lat is None:
                print(f"[{now:4.1f}s] GPS: no fix yet")
            else:
                head = "?" if status.heading_deg is None else f"{status.heading_deg:6.1f}°"
                print(f"[{now:4.1f}s] lat={status.gps_lat:.7f} lon={status.gps_lon:.7f} "
                      f"hdg={head} spd={status.speed_kph:4.1f} kph "
                      f"vds={hex_bytes(last_vds)}")


def hex_bytes(data: bytes | None) -> str:
    if data is None:
        return "none"
    return " ".join(f"{b:02X}" for b in data)


if __name__ == "__main__":
    main()
