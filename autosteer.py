"""
autosteer.py — shared GP AutoDrive protocol library.

This is the single source of truth for the numbered bring-up scripts in this
folder (01_..., 02_..., ...). Each script imports the pieces it needs from here
so the steps stay short and the protocol logic lives in one place.

Everything here is derived from PROTOCOL.md / the V10 proposal. Bit-field
encodings follow J1939 2-bit status semantics (00=off, 01=on, 10=error, 11=N/A).

Pure protocol + CAN transport, no external dependencies beyond python-can. The
route comes from routes.py (a straight line or a U-turn) — see PROTOCOL.md §10
for feeding it a route from anywhere else.
"""

from __future__ import annotations

import dataclasses
import math
import os
import struct
import time


# =============================================================================
# CONFIG — edit to match your bench / machine
# =============================================================================

# One switch for every script: the SocketCAN interface to use.
#   - bench test:   export CAN_BUS=vcan0   (with ./simulator.py running as Display)
#   - real machine: export CAN_BUS=can0
CAN_BUS = os.environ.get("CAN_BUS", "vcan0")

J1939_PRIORITY = 6
SOURCE_DISPLAY = 40           # the cab display
SOURCE_AUTODRIVE = 29         # us — proposal marks this "29 ?"; verify with vendor

# PGNs (see PROTOCOL.md §4)
PGN_VP1 = 0xFEF3              # NOT 0xFFEF — common transcription trap
PGN_VDS = 0xFEE8
PGN_DSSTAT = 0xFFCA
PGN_DSAP = 0xFFCB
PGN_ADJOB = 0xFFCC
PGN_ADWPI = 0xFFCD

# ADWPI 20-bit packed coordinate (see PROTOCOL.md §5.2)
ADWPI_COORD_OFFSET_CM = -250_000
ADWPI_COORD_RAW_MAX = (1 << 20) - 1

# ADWPI byte-8 flags. 2-bit J1939 fields, "on" = 01. Editable — bit positions
# were among the questioned items in the proposal.
ADWPI_FLAG_REVERSE = 0x01     # bits 2-1
ADWPI_FLAG_HEADLAND = 0x04    # bits 4-3

# Streaming behaviour (see PROTOCOL.md §8)
FUTURE_POINT_COUNT = 100
WINDOW_OVERLAP_POINTS = 3
SEND_INTERVAL_S = 0.010       # 10 ms pause between frames
ADJOB_PERIOD_S = 1.0


# =============================================================================
# DATA TYPES
# =============================================================================

@dataclasses.dataclass
class CanFrame:
    arbitration_id: int
    data: bytes


@dataclasses.dataclass
class RoutePoint:
    """A planned point in local ENU metres (east=x, north=y) plus flags."""
    x: float
    y: float
    is_headland: bool = False
    is_reverse: bool = False


@dataclasses.dataclass
class Waypoint:
    index: int
    east_cm: int
    north_cm: int
    is_headland: bool = False
    is_reverse: bool = False


@dataclasses.dataclass
class MachineStatus:
    gps_lat: float | None = None
    gps_lon: float | None = None
    speed_kph: float = 0.0
    heading_deg: float | None = None
    gps_ppp_available: bool = False
    autodrive_allowed: bool = False
    autosteer_engaged: bool = False
    header_down: bool = False
    current_direction_reverse: bool = False
    reject_reason: int = 0
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    last_rx_s: float = 0.0


# =============================================================================
# J1939 ID HELPERS  (PROTOCOL.md §2)
# =============================================================================

def j1939_id(pgn: int, source: int = SOURCE_AUTODRIVE) -> int:
    """Build a 29-bit extended CAN id from priority, PGN and source address."""
    return ((J1939_PRIORITY & 0x7) << 26) | ((pgn & 0x3FFFF) << 8) | (source & 0xFF)


def pgn_from_id(arbitration_id: int) -> int:
    """Extract the 18-bit PGN from a 29-bit id (ignores the source address)."""
    return (arbitration_id >> 8) & 0x3FFFF


def source_from_id(arbitration_id: int) -> int:
    return arbitration_id & 0xFF


# =============================================================================
# SCALAR DECODERS  (PROTOCOL.md §3, §6)
# =============================================================================

def unavailable_u32(raw: int) -> bool:
    return raw == 0xFFFFFFFF


def decode_latlon_u32(raw: int) -> float | None:
    """u32 → degrees, 1e-7 deg/bit, offset -210. 0xFFFFFFFF means unavailable."""
    if unavailable_u32(raw):
        return None
    return raw * 0.0000001 - 210.0


def encode_latlon_u32(deg: float | None) -> int:
    """Inverse of decode_latlon_u32 — used by the simulator to fake VP1/DSAP."""
    if deg is None:
        return 0xFFFFFFFF
    return int(round((deg + 210.0) / 0.0000001)) & 0xFFFFFFFF


# =============================================================================
# MESSAGE DECODERS (Display → us)
# =============================================================================

def decode_vp1(data: bytes, status: MachineStatus) -> None:
    if len(data) < 8:
        return
    lat_raw = struct.unpack_from("<I", data, 0)[0]
    lon_raw = struct.unpack_from("<I", data, 4)[0]
    status.gps_lat = decode_latlon_u32(lat_raw)
    status.gps_lon = decode_latlon_u32(lon_raw)
    status.last_rx_s = time.monotonic()


def decode_vds(data: bytes, status: MachineStatus) -> None:
    if len(data) < 8:
        return
    compass = struct.unpack_from("<H", data, 0)[0]
    speed = struct.unpack_from("<H", data, 2)[0]
    status.heading_deg = compass / 128.0
    status.speed_kph = speed / 256.0
    status.last_rx_s = time.monotonic()


def decode_dsap(data: bytes, status: MachineStatus) -> None:
    if len(data) < 8:
        return
    lat_raw = struct.unpack_from("<I", data, 0)[0]
    lon_raw = struct.unpack_from("<I", data, 4)[0]
    status.anchor_lat = decode_latlon_u32(lat_raw)
    status.anchor_lon = decode_latlon_u32(lon_raw)
    status.last_rx_s = time.monotonic()


def decode_dsstat(data: bytes, status: MachineStatus) -> None:
    """DSSTAT byte1/byte2 are J1939 2-bit status fields — test for == 01."""
    if len(data) < 8:
        return
    b1 = data[0]
    b2 = data[1]
    status.gps_ppp_available = (b1 & 0xC0) == 0x40          # bits 8-7
    status.autosteer_engaged = (b1 & 0x30) == 0x10          # bits 6-5
    status.header_down = (b1 & 0x0C) == 0x04                # bits 4-3
    status.current_direction_reverse = (b1 & 0x03) == 0x01  # bits 2-1, verify value
    status.autodrive_allowed = (b2 & 0x03) == 0x01          # byte2 bits 2-1
    status.reject_reason = (b2 >> 2) & 0x3F                 # byte2 bits 8-3
    status.last_rx_s = time.monotonic()


def process_frame(frame: CanFrame, status: MachineStatus) -> int:
    """Dispatch one received frame into status by PGN. Returns the PGN."""
    pgn = pgn_from_id(frame.arbitration_id)
    if pgn == PGN_VP1:
        decode_vp1(frame.data, status)
    elif pgn == PGN_VDS:
        decode_vds(frame.data, status)
    elif pgn == PGN_DSSTAT:
        decode_dsstat(frame.data, status)
    elif pgn == PGN_DSAP:
        decode_dsap(frame.data, status)
    return pgn


# =============================================================================
# MESSAGE ENCODERS (us → Display)
# =============================================================================

def clamp_u16(value: int) -> int:
    return max(0, min(0xFFFF, int(value)))


def encode_adjob(system_active: bool, run_command: bool, current_index: int,
                 total_points: int, job_id: int = 1, error_code: int = 0) -> bytes:
    """
    ADJOB 0xFFCC (PROTOCOL.md §6.5):
      byte1: reserved
      byte2: error nibble (8-5) | RunCommand (4-3, on=0x04) | SystemActive (2-1, on=0x01)
      byte3-4: current waypoint index (u16)
      byte5-6: line total point count (u16)
      byte7-8: job id (u16)
    """
    b = bytearray(8)
    b[0] = 0
    b[1] = ((error_code & 0x0F) << 4) | (0x04 if run_command else 0) | (0x01 if system_active else 0)
    struct.pack_into("<H", b, 2, clamp_u16(current_index))
    struct.pack_into("<H", b, 4, clamp_u16(total_points))
    struct.pack_into("<H", b, 6, clamp_u16(job_id))
    return bytes(b)


ADWPI_COORD_MIN_CM = ADWPI_COORD_OFFSET_CM                       # -250000 cm
ADWPI_COORD_MAX_CM = ADWPI_COORD_RAW_MAX + ADWPI_COORD_OFFSET_CM  # +798575 cm


class CoordinateRangeError(ValueError):
    """A waypoint offset falls outside the 20-bit ADWPI range (PROTOCOL.md §5.2)."""


def cm_to_adwpi_raw(cm: int) -> int:
    """cm east/north of the anchor → 20-bit raw. Raises if out of range rather than
    silently saturating — an over-range point means the anchor is too far from the
    field, and quietly folding it onto the boundary would draw a wrong line."""
    raw = cm - ADWPI_COORD_OFFSET_CM
    if not 0 <= raw <= ADWPI_COORD_RAW_MAX:
        raise CoordinateRangeError(
            f"{cm} cm is outside the ADWPI range "
            f"[{ADWPI_COORD_MIN_CM}, {ADWPI_COORD_MAX_CM}] cm")
    return raw


def adwpi_raw_to_cm(raw: int) -> int:
    return raw + ADWPI_COORD_OFFSET_CM


def encode_adwpi(point: Waypoint) -> bytes:
    """
    ADWPI 0xFFCD (PROTOCOL.md §5.2, §6.6):
      byte1-2: point index (u16, 0 = first)
      byte3-5.5: east, 20 bits, 1 cm/bit, offset -250000 cm
      byte5.5-7: north, 20 bits, 1 cm/bit, offset -250000 cm
      byte8: flags (reverse, headland, reserved)
    """
    east_raw = cm_to_adwpi_raw(point.east_cm)
    north_raw = cm_to_adwpi_raw(point.north_cm)

    flags = 0
    if point.is_headland:
        flags |= ADWPI_FLAG_HEADLAND
    if point.is_reverse:
        flags |= ADWPI_FLAG_REVERSE

    b = bytearray(8)
    struct.pack_into("<H", b, 0, clamp_u16(point.index))
    b[2] = east_raw & 0xFF
    b[3] = (east_raw >> 8) & 0xFF
    b[4] = ((east_raw >> 16) & 0x0F) | ((north_raw & 0x0F) << 4)
    b[5] = (north_raw >> 4) & 0xFF
    b[6] = (north_raw >> 12) & 0xFF
    b[7] = flags
    return bytes(b)


def decode_adwpi(data: bytes) -> Waypoint:
    """Inverse of encode_adwpi — handy for tests and for a fake Display."""
    index = struct.unpack_from("<H", data, 0)[0]
    east_raw = data[2] | (data[3] << 8) | ((data[4] & 0x0F) << 16)
    north_raw = ((data[4] >> 4) & 0x0F) | (data[5] << 4) | (data[6] << 12)
    flags = data[7]
    return Waypoint(
        index=index,
        east_cm=adwpi_raw_to_cm(east_raw),
        north_cm=adwpi_raw_to_cm(north_raw),
        # 2-bit J1939 fields: isolate the whole pair, "on" only when it equals 01.
        # 10 (error) / 11 (not available) must read false (see PROTOCOL.md §5.1).
        is_headland=(flags & (ADWPI_FLAG_HEADLAND * 3)) == ADWPI_FLAG_HEADLAND,
        is_reverse=(flags & (ADWPI_FLAG_REVERSE * 3)) == ADWPI_FLAG_REVERSE,
    )


# =============================================================================
# GEO HELPERS  (PROTOCOL.md §3)
# =============================================================================

def wgs_to_enu_approx(lat: float, lon: float, datum_lat: float, datum_lon: float) -> tuple[float, float]:
    """Small-field flat-earth WGS84 → east,north metres relative to datum."""
    lat0 = math.radians(datum_lat)
    north = (lat - datum_lat) * 111_320.0
    east = (lon - datum_lon) * 111_320.0 * math.cos(lat0)
    return east, north


def enu_to_wgs_approx(east: float, north: float, datum_lat: float, datum_lon: float) -> tuple[float, float]:
    """Inverse of wgs_to_enu_approx — east,north metres → lat,lon degrees."""
    lat0 = math.radians(datum_lat)
    lat = datum_lat + north / 111_320.0
    lon = datum_lon + east / (111_320.0 * math.cos(lat0))
    return lat, lon


def point_inside_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


# =============================================================================
# CAN TRANSPORT  (PROTOCOL.md §2)
# =============================================================================

def format_frame(direction: str, frame: CanFrame) -> str:
    data = " ".join(f"{x:02X}" for x in frame.data)
    pgn = pgn_from_id(frame.arbitration_id)
    return f"{direction} 0x{frame.arbitration_id:08X} pgn=0x{pgn:04X} [{len(frame.data)}] {data}"


class SocketCanBus:
    """Thin wrapper over python-can for a real SocketCAN interface."""

    def __init__(self, channel: str):
        try:
            import can
        except Exception as exc:  # pragma: no cover - host dependent
            raise SystemExit("python-can missing. Enter the nix develop shell.") from exc
        import atexit
        self.can = can
        self.bus = can.Bus(interface="socketcan", channel=channel)
        atexit.register(self.bus.shutdown)   # clean close, no shutdown warning

    def send(self, frame: CanFrame) -> None:
        self.bus.send(self.can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=True,
        ))

    def recv(self, timeout: float = 0.0) -> CanFrame | None:
        msg = self.bus.recv(timeout=timeout)
        if msg is None:
            return None
        return CanFrame(arbitration_id=msg.arbitration_id, data=bytes(msg.data))


def make_bus(channel: str = CAN_BUS):
    """Open the SocketCAN interface named by CAN_BUS (vcan0 for bench, can0 for the machine)."""
    return SocketCanBus(channel)


# =============================================================================
# SHARED CONVENIENCES used across the numbered scripts
# =============================================================================

def send(bus, pgn: int, data: bytes) -> None:
    bus.send(CanFrame(arbitration_id=j1939_id(pgn), data=data))


def drain_rx(bus, status: MachineStatus, max_frames: int = 50) -> list[int]:
    """Consume all immediately-available frames into status. Returns PGNs seen."""
    seen: list[int] = []
    for _ in range(max_frames):
        frame = bus.recv(timeout=0.0)
        if frame is None:
            break
        seen.append(process_frame(frame, status))
    return seen
