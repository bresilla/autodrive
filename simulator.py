#!/usr/bin/env python3
"""
simulator.py — a fake OXBO Display on a (virtual) CAN bus.

Run this in one terminal to emulate the Display, so you can exercise the
bring-up steps without a machine. It speaks the Display side of the protocol on
a real SocketCAN interface — use a virtual one (vcan0) for the bench:

    # one-time vcan setup:
    sudo modprobe vcan
    sudo ip link add dev vcan0 type vcan
    sudo ip link set up vcan0

    # terminal A — the fake Display (channel, then optional route name):
    ./simulator.py                 # binds vcan0, plays the "line" route
    ./simulator.py vcan0 uturn     # for bench-testing step 10's U-turn route

    # terminal B — the bring-up steps (CAN_BUS defaults to vcan0):
    ./03_get_location.py

What it does:
  * emits VP1 + VDS + DSSTAT on their schedules,
  * declares GPS PPP available a few seconds after start (PPP acquisition),
  * watches incoming ADJOB: when SystemActive flips on it computes an anchor
    (the current machine position) and starts emitting DSAP,
  * collects incoming ADWPI and, once RunCommand is on, drives a virtual machine
    along them, reporting AutoDrive engaged after a short delay,
  * feeds the moving machine position back out via VP1/VDS so the client's
    progress tracking advances.

Deliberately simple — enough to prove the message flow end to end.
"""

from __future__ import annotations

import math
import struct
import sys
import time

import autodrive as a
import routes


# -- tweakables ---------------------------------------------------------------
# Datum + boot position are set from the route in main() so the virtual machine
# starts on the route the client is driving; these are placeholders.
DATUM_LAT, DATUM_LON = 0.0, 0.0
START_LAT, START_LON = 0.0, 0.0       # where the virtual machine sits at boot
PPP_ACQUIRE_S = 3.0
ENGAGE_DELAY_S = 2.0
MACHINE_SPEED_MPS = 1.5


class DisplayModel:
    """The Display's state machine. Pure logic: feed it frames + time, get frames."""

    def __init__(self):
        self.t = 0.0
        self.machine_lat = START_LAT
        self.machine_lon = START_LON
        self.heading_deg = 0.0
        self.speed_kph = 0.0

        self.autodrive_allowed = True
        self.autodrive_engaged = False

        self.system_active = False
        self.run_command = False
        self.run_started_t: float | None = None
        self.job_id: int | None = None
        self.jobs_started = 0

        self.anchor_lat: float | None = None
        self.anchor_lon: float | None = None
        self.waypoints: dict[int, a.Waypoint] = {}
        self.target_index = 0

        self._next = {a.PGN_VP1: 0.0, a.PGN_VDS: 0.0, a.PGN_DSSTAT: 0.0, a.PGN_DSAP: 0.0}

    # -- inbound (client → display) -------------------------------------------

    def on_frame(self, frame: a.CanFrame) -> None:
        pgn = a.pgn_from_id(frame.arbitration_id)
        if pgn == a.PGN_ADJOB:
            self._on_adjob(frame.data)
        elif pgn == a.PGN_ADWPI:
            wp = a.decode_adwpi(frame.data)
            self.waypoints[wp.index] = wp

    def _on_adjob(self, data: bytes) -> None:
        self.system_active = (data[1] & 0x03) == 0x01
        self.run_command = (data[1] & 0x0C) == 0x04
        job_id = struct.unpack_from("<H", data, 6)[0]

        # A change of Job ID while SystemActive starts a NEW job (spec2.md): the
        # display recomputes the anchor, clears the map, resets the line.
        if self.system_active and job_id != self.job_id:
            self.job_id = job_id
            self.jobs_started += 1
            self.anchor_lat = self.machine_lat      # job start: anchor = current position
            self.anchor_lon = self.machine_lon
            self.waypoints.clear()
            self.target_index = 0
            self._next[a.PGN_DSAP] = self.t         # broadcast the new anchor promptly
            print(f"  [sim] job #{self.jobs_started} started: id={job_id}, anchor = "
                  f"{self.anchor_lat:.7f},{self.anchor_lon:.7f}", file=sys.stderr)
        if self.run_command and self.run_started_t is None:
            self.run_started_t = self.t
            print("  [sim] RunCommand received", file=sys.stderr)
        if not self.run_command:
            self.run_started_t = None
            self.autodrive_engaged = False

    # -- tick: advance physics + return scheduled outbound frames -------------

    def tick(self, t: float, dt: float) -> list[a.CanFrame]:
        self.t = t
        self._drive(dt)
        out: list[a.CanFrame] = []
        if t >= self._next[a.PGN_VP1]:
            self._next[a.PGN_VP1] = t + 0.1
            out.append(self._vp1())
        if t >= self._next[a.PGN_VDS]:
            self._next[a.PGN_VDS] = t + 0.2
            out.append(self._vds())
        if t >= self._next[a.PGN_DSSTAT]:
            self._next[a.PGN_DSSTAT] = t + 0.2
            out.append(self._dsstat())
        if self.anchor_lat is not None and t >= self._next[a.PGN_DSAP]:
            self._next[a.PGN_DSAP] = t + 5.0
            out.append(self._dsap())
        return out

    def _ppp(self) -> bool:
        return self.t >= PPP_ACQUIRE_S

    def _drive(self, dt: float) -> None:
        if self.run_command and self.run_started_t is not None:
            if self.t - self.run_started_t >= ENGAGE_DELAY_S and not self.autodrive_engaged:
                self.autodrive_engaged = True
                print("  [sim] AutoDrive engaged", file=sys.stderr)

        if not (self.run_command and self.autodrive_engaged and self.anchor_lat is not None
                and self.waypoints):
            self.speed_kph = 0.0
            return

        indices = sorted(self.waypoints)
        if self.target_index not in self.waypoints:
            self.target_index = indices[0]
        target = self.waypoints[self.target_index]

        anchor_e, anchor_n = a.wgs_to_enu_approx(self.anchor_lat, self.anchor_lon,
                                                 DATUM_LAT, DATUM_LON)
        tx_e = anchor_e + target.east_cm / 100.0
        tx_n = anchor_n + target.north_cm / 100.0
        cur_e, cur_n = a.wgs_to_enu_approx(self.machine_lat, self.machine_lon,
                                           DATUM_LAT, DATUM_LON)

        de, dn = tx_e - cur_e, tx_n - cur_n
        dist = math.hypot(de, dn)
        step = MACHINE_SPEED_MPS * dt
        if dist <= step:
            cur_e, cur_n = tx_e, tx_n
            if self.target_index < indices[-1]:
                self.target_index += 1
        else:
            cur_e += de / dist * step
            cur_n += dn / dist * step
        if dist > 1e-6:
            self.heading_deg = math.degrees(math.atan2(de, dn)) % 360.0
        self.speed_kph = MACHINE_SPEED_MPS * 3.6

        self.machine_lat, self.machine_lon = a.enu_to_wgs_approx(cur_e, cur_n, DATUM_LAT, DATUM_LON)

    # -- frame builders -------------------------------------------------------

    def _frame(self, pgn: int, data: bytes) -> a.CanFrame:
        return a.CanFrame(arbitration_id=a.j1939_id(pgn, a.SOURCE_DISPLAY), data=data)

    def _vp1(self) -> a.CanFrame:
        b = bytearray(8)
        if self._ppp():
            struct.pack_into("<I", b, 0, a.encode_latlon_u32(self.machine_lat))
            struct.pack_into("<I", b, 4, a.encode_latlon_u32(self.machine_lon))
        else:
            struct.pack_into("<I", b, 0, 0xFFFFFFFF)
            struct.pack_into("<I", b, 4, 0xFFFFFFFF)
        return self._frame(a.PGN_VP1, bytes(b))

    def _vds(self) -> a.CanFrame:
        b = bytearray(8)
        struct.pack_into("<H", b, 0, int(self.heading_deg * 128.0) & 0xFFFF)
        struct.pack_into("<H", b, 2, int(self.speed_kph * 256.0) & 0xFFFF)
        return self._frame(a.PGN_VDS, bytes(b))

    def _dsstat(self) -> a.CanFrame:
        b = bytearray(8)
        if self._ppp():
            b[0] |= 0x40                          # GPS PPP available = 01 @ bits 8-7
        if self.autodrive_engaged:
            b[0] |= 0x10                          # AutoDrive engaged = 01 @ bits 6-5
        if self.autodrive_allowed:
            b[1] |= 0x01                          # AutoDrive allowed = 01 @ bits 2-1
        return self._frame(a.PGN_DSSTAT, bytes(b))

    def _dsap(self) -> a.CanFrame:
        b = bytearray(8)
        struct.pack_into("<I", b, 0, a.encode_latlon_u32(self.anchor_lat))
        struct.pack_into("<I", b, 4, a.encode_latlon_u32(self.anchor_lon))
        return self._frame(a.PGN_DSAP, bytes(b))


def main() -> None:
    channel = sys.argv[1] if len(sys.argv) > 1 else a.CAN_BUS
    route_name = sys.argv[2] if len(sys.argv) > 2 else "line"

    # Sit the virtual machine at the route's start so the bench loop closes.
    global DATUM_LAT, DATUM_LON, START_LAT, START_LON
    DATUM_LAT, DATUM_LON = routes.geojson_datum(routes.geojson_path(route_name))
    START_LAT, START_LON = DATUM_LAT, DATUM_LON

    bus = a.SocketCanBus(channel)
    model = DisplayModel()
    print(f"simulator: fake Display on {channel}, route {route_name!r} "
          f"@ {START_LAT:.7f},{START_LON:.7f} (Ctrl-C to stop)", file=sys.stderr)

    t0 = time.monotonic()
    last = t0
    try:
        while True:
            frame = bus.recv(timeout=0.01)
            if frame is not None:
                model.on_frame(frame)
            now = time.monotonic()
            for out in model.tick(now - t0, now - last):
                bus.send(out)
            last = now
    except KeyboardInterrupt:
        print("\nsimulator: stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
