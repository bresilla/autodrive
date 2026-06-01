#!/usr/bin/env python3
"""
Step 07 — coordinate conversion and ADWPI packing (no bus needed).

Goal: take a route in planner ENU metres, express it as anchor-relative
centimetres, pack it into ADWPI frames, and decode it back to prove the
round-trip.

What this step proves:
  * the three frames: WGS84 deg → datum ENU metres → anchor-relative cm
  * the 20-bit east/north packing with the nibble split in byte 5
  * how headland/reverse flags ride in byte 8
  * encode_adwpi / decode_adwpi are exact inverses

This is pure math — run it anywhere, no CAN.

Run:
    ./07_coordinates.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a

DATUM_LAT, DATUM_LON = 51.0, 5.0

# A tiny route in planner ENU metres: (x=east, y=north, headland?, reverse?)
ROUTE_ENU = [
    (0.0, 0.0, False, False),
    (0.0, 25.0, False, False),
    (0.0, 50.0, True, False),    # headland point: lift header
    (3.0, 50.0, True, True),     # headland + reverse
    (3.0, 25.0, False, False),
    (3.0, 0.0, False, False),
]

# Suppose the Display anchored at this lat/lon (a bit north-east of datum).
ANCHOR_LAT, ANCHOR_LON = 51.0002, 5.0003


def main() -> None:
    anchor_e, anchor_n = a.wgs_to_enu_approx(ANCHOR_LAT, ANCHOR_LON, DATUM_LAT, DATUM_LON)
    print(f"anchor in datum-ENU metres: east={anchor_e:.2f} north={anchor_n:.2f}\n")

    print(f"{'idx':>3} {'east_cm':>9} {'north_cm':>9}  flags        bytes")
    for i, (x, y, hl, rev) in enumerate(ROUTE_ENU):
        wp = a.Waypoint(
            index=i,
            east_cm=round((x - anchor_e) * 100.0),
            north_cm=round((y - anchor_n) * 100.0),
            is_headland=hl,
            is_reverse=rev,
        )
        frame = a.encode_adwpi(wp)
        back = a.decode_adwpi(frame)

        # Prove the round-trip (cm survives exactly; flags survive exactly).
        assert back.index == wp.index
        assert back.east_cm == wp.east_cm
        assert back.north_cm == wp.north_cm
        assert back.is_headland == wp.is_headland
        assert back.is_reverse == wp.is_reverse

        flags = []
        if wp.is_headland:
            flags.append("HEADLAND")
        if wp.is_reverse:
            flags.append("REVERSE")
        flag_str = ",".join(flags) or "-"
        hexbytes = " ".join(f"{b:02X}" for b in frame)
        print(f"{i:>3} {wp.east_cm:>9} {wp.north_cm:>9}  {flag_str:12} {hexbytes}")

    print("\nall waypoints round-tripped encode→decode exactly. ✓")


if __name__ == "__main__":
    main()
