#!/usr/bin/env python3
"""
Step 05b — end a job and start a fresh one (the "new field" cycle).

Goal: test the full job lifecycle, not just back-to-back Job IDs. Each round:
SystemActive **off** (the current job ends), a pause, then SystemActive **on**
with a NEW Job ID (a fresh job starts — new anchor, cleared map). This is what
happens when you finish one field and drive into the next (spec2.md: the Job ID
must change when entering another field).

What this step proves:
  * SystemActive off → on cleanly bookends a job
  * a new Job ID on reactivation starts a distinct job (not a resume)
  * the display issues a fresh anchor (DSAP) per job — watch it change if the
    machine has moved between jobs

⚠️ RunCommand stays off — nothing drives. On a real machine the display may need
   PPP + AutoDrive-allowed before it accepts SystemActive.

Run:
    ./05_b_job_restart.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autosteer as a

JOB_IDS = [201, 202, 203]   # one job per "field"
ACTIVE_S = 3.0              # hold each job active
GAP_S = 1.5                # SystemActive off between jobs


def yn(b: bool) -> str:
    return "Y" if b else "-"


def send_adjob(bus, active: bool, job_id: int, t0: float) -> None:
    now = time.monotonic() - t0
    data = a.encode_adjob(system_active=active, run_command=False,
                          current_index=0, total_points=0, job_id=job_id)
    a.send(bus, a.PGN_ADJOB, data)
    print(f"[{now:5.1f}s] ADJOB job_id={job_id} active={yn(active)}")


def hold(bus, status, active: bool, job_id: int, t0: float, seconds: float) -> None:
    """Transmit ADJOB at 1 Hz for `seconds`, draining RX in between."""
    last = -999.0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        a.drain_rx(bus, status)
        if time.monotonic() - last >= a.ADJOB_PERIOD_S:
            last = time.monotonic()
            send_adjob(bus, active, job_id, t0)
        time.sleep(0.02)


def main() -> None:
    bus = a.make_bus()
    status = a.MachineStatus()

    print(f"job-restart test: {len(JOB_IDS)} jobs with off→on cycles, on {a.CAN_BUS}",
          file=sys.stderr)

    t0 = time.monotonic()
    last_anchor = None
    for i, job_id in enumerate(JOB_IDS):
        print(f"\n--- Job {i + 1}/{len(JOB_IDS)}: id={job_id} ---")
        hold(bus, status, active=True, job_id=job_id, t0=t0, seconds=ACTIVE_S)

        anchor = (status.anchor_lat, status.anchor_lon)
        if status.anchor_lat is not None and anchor != last_anchor:
            print(f"    anchor now {status.anchor_lat:.7f},{status.anchor_lon:.7f}")
            last_anchor = anchor

        if i < len(JOB_IDS) - 1:
            print(f"    ending job {job_id} (SystemActive=off for {GAP_S:.1f}s)")
            hold(bus, status, active=False, job_id=job_id, t0=t0, seconds=GAP_S)

    print(f"\nran {len(JOB_IDS)} job cycles. Each reactivation with a new Job ID "
          f"should show as a fresh job on the display/simulator.")


if __name__ == "__main__":
    main()
