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
import routes

# The real U-turn field path (u_field.geojson). Its datum is the route start.
ROUTE_NAME = "uturn"


def main() -> None:
    route, datum_lat, datum_lon = routes.geojson_route(routes.geojson_path(ROUTE_NAME))

    # Suppose the Display anchored at the route start (the usual case: anchor =
    # machine position when the job begins, and we begin at the first point).
    anchor_lat, anchor_lon = datum_lat, datum_lon
    anchor_e, anchor_n = a.wgs_to_enu_approx(anchor_lat, anchor_lon, datum_lat, datum_lon)
    print(f"route {ROUTE_NAME!r}: {len(route)} points, "
          f"datum/anchor {datum_lat:.7f},{datum_lon:.7f}\n")

    waypoints = [
        a.Waypoint(index=i,
                   east_cm=round((p.x - anchor_e) * 100.0),
                   north_cm=round((p.y - anchor_n) * 100.0),
                   is_headland=p.is_headland, is_reverse=p.is_reverse)
        for i, p in enumerate(route)
    ]

    # Round-trip every waypoint, and confirm each packs within the ADWPI range
    # (encode_adwpi raises CoordinateRangeError if a point is out of range).
    east_max = north_max = 0
    for wp in waypoints:
        back = a.decode_adwpi(a.encode_adwpi(wp))
        assert back.index == wp.index
        assert back.east_cm == wp.east_cm
        assert back.north_cm == wp.north_cm
        assert back.is_headland == wp.is_headland
        assert back.is_reverse == wp.is_reverse
        east_max = max(east_max, abs(wp.east_cm))
        north_max = max(north_max, abs(wp.north_cm))

    # Show a sample: the first few points and the headland turn transition.
    first_hl = next((i for i, p in enumerate(route) if p.is_headland), None)
    sample = list(range(3))
    if first_hl is not None:
        sample += [first_hl - 1, first_hl, first_hl + 1]
    sample.append(len(waypoints) - 1)

    print(f"{'idx':>4} {'east_cm':>9} {'north_cm':>9}  flags        bytes")
    for i in sorted(set(j for j in sample if 0 <= j < len(waypoints))):
        wp = waypoints[i]
        flags = ",".join(f for f, on in
                         (("HEADLAND", wp.is_headland), ("REVERSE", wp.is_reverse)) if on) or "-"
        hexbytes = " ".join(f"{b:02X}" for b in a.encode_adwpi(wp))
        print(f"{i:>4} {wp.east_cm:>9} {wp.north_cm:>9}  {flags:12} {hexbytes}")

    print(f"\nmax offset from anchor: east {east_max} cm, north {north_max} cm "
          f"(range {a.ADWPI_COORD_MIN_CM}..{a.ADWPI_COORD_MAX_CM}).")
    print(f"all {len(waypoints)} waypoints round-tripped encode→decode exactly, "
          f"all in range. ✓")


if __name__ == "__main__":
    main()
