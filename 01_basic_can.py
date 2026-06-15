#!/usr/bin/env python3
"""
Step 01 — basic CAN communication.

Goal: open a bus and send one frame. Nothing protocol-specific yet beyond
building a J1939 id.

What this step proves:
  * a CAN frame = arbitration id + up to 8 data bytes
  * how the 29-bit J1939 id splits into priority / PGN / source address
  * opening the SocketCAN interface named by CAN_BUS (vcan0 bench / can0 machine)

Run:
    ./01_basic_can.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a



def explain_id(pgn: int) -> None:
    arb = a.j1939_id(pgn)
    print(f"PGN 0x{pgn:04X} → id 0x{arb:08X}")
    print(f"  priority = {(arb >> 26) & 0x7}")
    print(f"  pgn      = 0x{a.pgn_from_id(arb):04X}")
    print(f"  source   = {a.source_from_id(arb)}  (AutoDrive)")


def main() -> None:
    # The id is just three packed fields; build one and take it back apart.
    explain_id(a.PGN_ADJOB)

    bus = a.make_bus()

    # Send a single ADJOB heartbeat announcing we exist but are NOT active.
    data = a.encode_adjob(system_active=False, run_command=False,
                          current_index=0, total_points=0)
    print("\nsending one ADJOB (systemActive=false):")
    print(" ", a.format_frame("TX", a.CanFrame(a.j1939_id(a.PGN_ADJOB), data)))
    a.send(bus, a.PGN_ADJOB, data)

    print("\nframe sent. In a real setup the display now sees an AutoDrive node.")


if __name__ == "__main__":
    main()
