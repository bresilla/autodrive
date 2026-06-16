#!/usr/bin/env python3
"""
Continuously log SocketCAN traffic to hourly candump-style files.

Run:
    ./hourly_can_logger.py /var/log/autodrive-can
    ./hourly_can_logger.py /var/log/autodrive-can --can-bus can0

Each file is named like:
    can0_20260616_1400.log

Lines use candump's common text shape:
    (1780495918.207498)  can0  18FFCB28   [8]  0C 25 E3 9B 44 9F D9 7F
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a


DEFAULT_ROTATE_SECONDS = 3600


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log CAN frames to hourly candump-style files.")
    parser.add_argument("directory", type=Path, help="directory where log files will be written")
    parser.add_argument("--can-bus", default=a.CAN_BUS, help=f"SocketCAN interface (default: {a.CAN_BUS})")
    parser.add_argument("--prefix", default=None, help="log filename prefix (default: CAN bus name)")
    parser.add_argument(
        "--rotate-seconds",
        type=int,
        default=DEFAULT_ROTATE_SECONDS,
        help=f"rotation interval in seconds (default: {DEFAULT_ROTATE_SECONDS})",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=1,
        help="flush after this many frames; use 1 for safest systemd logging",
    )
    return parser.parse_args()


def rotation_start(ts: float, interval_s: int) -> int:
    return int(ts // interval_s) * interval_s


def log_path(directory: Path, prefix: str, bucket_start: int) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M", time.localtime(bucket_start))
    return directory / f"{prefix}_{stamp}.log"


def candump_line(channel: str, frame: a.CanFrame, ts: float) -> str:
    data = " ".join(f"{byte:02X}" for byte in frame.data)
    return f"({ts:.6f})  {channel}  {frame.arbitration_id:08X}   [{len(frame.data)}]  {data}\n"


def main() -> None:
    args = parse_args()
    if args.rotate_seconds <= 0:
        raise SystemExit("--rotate-seconds must be greater than zero")
    if args.flush_every <= 0:
        raise SystemExit("--flush-every must be greater than zero")

    args.directory.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix or args.can_bus
    bus = a.make_bus(args.can_bus)

    stop = False

    def request_stop(signum, frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    current_bucket: int | None = None
    fh = None
    frame_count = 0

    print(f"logging {args.can_bus} to {args.directory}", file=sys.stderr)
    try:
        while not stop:
            now = time.time()
            bucket = rotation_start(now, args.rotate_seconds)
            if bucket != current_bucket:
                if fh is not None:
                    fh.flush()
                    fh.close()
                current_bucket = bucket
                path = log_path(args.directory, prefix, bucket)
                fh = path.open("a", buffering=1)
                print(f"writing {path}", file=sys.stderr)

            frame = bus.recv(timeout=0.5)
            if frame is None:
                continue

            assert fh is not None
            fh.write(candump_line(args.can_bus, frame, time.time()))
            frame_count += 1
            if frame_count % args.flush_every == 0:
                fh.flush()
    finally:
        if fh is not None:
            fh.flush()
            fh.close()
        print("stopped CAN logger", file=sys.stderr)


if __name__ == "__main__":
    main()
