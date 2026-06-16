#!/usr/bin/env python3
"""
Step 05a — start several jobs by bumping the Job ID.

Goal: provoke the display's job system. Per spec2.md, a **change of Job ID while
SystemActive is on** makes the display start a NEW job (recompute anchor, clear
the map, reset the line). This walks the Job ID through a short sequence so you
can watch several jobs appear on the display, one after another.

What this step proves:
  * ADJOB byte7-8 carries the Job ID (u16, 0…65530)
  * each new Job ID (with SystemActive) starts a fresh job on the display
  * a stable Job ID does NOT restart the job — only a change does

RunCommand stays off — nothing drives. The script only sets SystemActive=true
when the normal gates pass (PPP + AutoDrive allowed + inside the route field).
The gate status is printed each cycle so you can tell why a job did or did not
start.

Run:
    ./05_a_multiple_jobs.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes

JOB_IDS = [101, 102, 103, 104]   # the sequence of jobs to start
HOLD_S = 3.0                     # how long to hold each job before the next
FIELD_MARGIN_M = 15.0


def yn(b: bool) -> str:
    return "Y" if b else "-"


def inside_field(status, field, datum_lat, datum_lon) -> bool:
    if status.gps_lat is None or status.gps_lon is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, datum_lat, datum_lon)
    return a.point_inside_polygon(x, y, field)


def main() -> None:
    route, datum_lat, datum_lon = routes.ROUTES["line"]()
    field = routes.bounding_field(route, FIELD_MARGIN_M)
    total_points = len(route)
    bus = a.make_bus()
    status = a.MachineStatus()

    print(f"job-system test: starting jobs {JOB_IDS}, {HOLD_S:.0f}s each, on {a.CAN_BUS}",
          file=sys.stderr)

    t0 = time.monotonic()
    last_adjob = -999.0
    for job_id in JOB_IDS:
        job_start = time.monotonic()
        print(f"\n--- Job ID {job_id} (SystemActive=true) ---")
        while time.monotonic() - job_start < HOLD_S:
            a.drain_rx(bus, status)
            now = time.monotonic() - t0
            if now - last_adjob >= a.ADJOB_PERIOD_S:
                last_adjob = now
                active = (status.gps_ppp_available
                          and status.autodrive_allowed
                          and inside_field(status, field, datum_lat, datum_lon))
                data = a.encode_adjob(system_active=active, run_command=False,
                                      current_index=0, total_points=total_points,
                                      job_id=job_id)
                a.send(bus, a.PGN_ADJOB, data)
                print(f"[{now:5.1f}s] ADJOB job_id={job_id} active={yn(active)} "
                      f"total_points={total_points} "
                      f"(ppp={yn(status.gps_ppp_available)} "
                      f"allowed={yn(status.autodrive_allowed)} "
                      f"inside={yn(inside_field(status, field, datum_lat, datum_lon))} "
                      f"anchor={'Y' if status.anchor_lat is not None else '-'})")
            time.sleep(0.02)

    print(f"\nstarted {len(JOB_IDS)} jobs. The display/simulator should have logged "
          f"a new job for each Job ID change.")


if __name__ == "__main__":
    main()
