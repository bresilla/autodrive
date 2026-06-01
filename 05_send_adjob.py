#!/usr/bin/env python3
"""
Step 05 — send the ADJOB heartbeat.

Goal: transmit our job-state message at 1 Hz. While inactive we still announce
ourselves so the Display knows an AutoDrive node is present.

What this step proves:
  * ADJOB byte2 packs three fields: error nibble | RunCommand | SystemActive
  * the 2-bit "on" encoding: SystemActive on = 0x01, RunCommand on = 0x04
    (NOT 0x08 — that would be the J1939 "error" pattern)
  * a periodic transmit loop

Here we keep SystemActive=false the whole time (gating comes in step 06).

Run:
    ./05_send_adjob.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a

RUN_SECONDS = 5.0


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    t0 = time.monotonic()
    last_adjob = -999.0
    while True:
        now = time.monotonic() - t0
        if now >= RUN_SECONDS:
            break
        # Keep reading so status stays fresh.
        a.drain_rx(bus, status)

        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            data = a.encode_adjob(system_active=False, run_command=False,
                                  current_index=0, total_points=0)
            a.send(bus, a.PGN_ADJOB, data)
            print(f"[{now:4.1f}s] " + a.format_frame("TX",
                  a.CanFrame(a.j1939_id(a.PGN_ADJOB), data)))
            print(f"          byte2=0x{data[1]:02X}  systemActive=off runCommand=off")
        time.sleep(0.02)


if __name__ == "__main__":
    main()
