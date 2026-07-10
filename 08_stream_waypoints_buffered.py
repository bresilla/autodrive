#!/usr/bin/env python3
"""
Step 08 buffered — keep the Display/AgJunction ahead of the machine.

This is a separate streaming strategy so the original step 08 files stay intact.

Defaults are tuned for rolling 100-point AgJunction updates:

  * preload the first 100 points
  * for a 0..99 window, trigger the next update at point 90
  * each update starts 5 points before that trigger point
  * each update sends 100 frames

Example:
  first preload sends 0..99
  at progress ~90 it sends 85..184 (end-exclusive 185)

Run:
    ./08_stream_waypoints_buffered.py --route line
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autodrive as a
import routes


DEFAULT_MAX_SPACING_M = 1.0
DEFAULT_INITIAL_POINTS = 100
DEFAULT_WINDOW_SIZE = 100
DEFAULT_TRIGGER_BEFORE_END_POINTS = 10
DEFAULT_OVERLAP_BEFORE_TRIGGER_POINTS = 5
DEFAULT_NEAREST_BACKTRACK = 6
DEFAULT_NEAREST_AHEAD = 180
DEFAULT_ADJOB_AHEAD_POINTS = 1
MIN_SPACING_M = 0.3
FIELD_MARGIN_M = 15.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Request anchor, then keep streaming buffered ADWPI waypoint windows.",
    )
    parser.add_argument("--route", choices=sorted(routes.ROUTES), default="line", help="route to stream")
    parser.add_argument("--job-id", type=int, default=None, help="ADJOB id; default is a fresh time-based u16 id")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for a valid DSAP anchor")
    parser.add_argument("--no-inside-gate", action="store_true", help="do not require machine position inside route field")
    parser.add_argument(
        "--max-spacing",
        type=float,
        default=DEFAULT_MAX_SPACING_M,
        help=f"maximum route spacing in metres after interpolation (default: {DEFAULT_MAX_SPACING_M})",
    )
    parser.add_argument(
        "--initial-points",
        type=int,
        default=DEFAULT_INITIAL_POINTS,
        help=f"points to stream immediately after anchoring (default: {DEFAULT_INITIAL_POINTS})",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=DEFAULT_WINDOW_SIZE,
        help=f"frames per later update window (default: {DEFAULT_WINDOW_SIZE})",
    )
    parser.add_argument(
        "--trigger-before-end-points",
        type=int,
        default=DEFAULT_TRIGGER_BEFORE_END_POINTS,
        help="trigger a new update this many points before the current sent window ends "
             f"(default: {DEFAULT_TRIGGER_BEFORE_END_POINTS})",
    )
    parser.add_argument(
        "--overlap-before-trigger-points",
        type=int,
        default=DEFAULT_OVERLAP_BEFORE_TRIGGER_POINTS,
        help="start the new update this many points before the trigger point "
             f"(default: {DEFAULT_OVERLAP_BEFORE_TRIGGER_POINTS})",
    )
    parser.add_argument(
        "--nearest-ahead",
        type=int,
        default=DEFAULT_NEAREST_AHEAD,
        help=f"route points ahead to search for GPS progress (default: {DEFAULT_NEAREST_AHEAD})",
    )
    parser.add_argument(
        "--nearest-backtrack",
        type=int,
        default=DEFAULT_NEAREST_BACKTRACK,
        help=f"route points behind to allow in GPS progress search (default: {DEFAULT_NEAREST_BACKTRACK})",
    )
    parser.add_argument(
        "--adjob-ahead-points",
        type=int,
        default=DEFAULT_ADJOB_AHEAD_POINTS,
        help="report ADJOB current index this many points ahead of estimated progress "
             f"(default: {DEFAULT_ADJOB_AHEAD_POINTS})",
    )
    parser.add_argument(
        "--loop-timeout",
        type=float,
        default=0.0,
        help="seconds to keep the run loop alive after anchoring; 0 means until route complete",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.max_spacing <= 0:
        raise SystemExit("--max-spacing must be greater than zero")
    if args.initial_points <= 0:
        raise SystemExit("--initial-points must be greater than zero")
    if args.window_size <= 0:
        raise SystemExit("--window-size must be greater than zero")
    if args.trigger_before_end_points < 0:
        raise SystemExit("--trigger-before-end-points must be zero or greater")
    if args.overlap_before_trigger_points < 0:
        raise SystemExit("--overlap-before-trigger-points must be zero or greater")
    if args.nearest_ahead < 1:
        raise SystemExit("--nearest-ahead must be at least 1")
    if args.nearest_backtrack < 0:
        raise SystemExit("--nearest-backtrack must be zero or greater")
    if args.adjob_ahead_points < 0:
        raise SystemExit("--adjob-ahead-points must be zero or greater")
    if args.loop_timeout < 0:
        raise SystemExit("--loop-timeout must be zero or greater")


def inside_field(status: a.MachineStatus, field: list[tuple[float, float]], datum_lat: float, datum_lon: float) -> bool:
    if status.gps_lat is None or status.gps_lon is None:
        return False
    x, y = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, datum_lat, datum_lon)
    return a.point_inside_polygon(x, y, field)


def route_for_max_spacing(route_name: str, max_spacing_m: float):
    path = routes.geojson_path(route_name)
    route, dlat, dlon = routes.geojson_route(path, spacing_m=max_spacing_m)
    return path, route, dlat, dlon


def max_route_spacing(route: list[a.RoutePoint]) -> float:
    if len(route) < 2:
        return 0.0
    return max(math.hypot(p1.x - p0.x, p1.y - p0.y) for p0, p1 in zip(route, route[1:]))


def load_line_from_anchor(path: str, spacing_m: float, anchor_lat: float, anchor_lon: float) -> list[a.RoutePoint]:
    return routes.geojson_route(path, spacing_m=spacing_m, datum_lat=anchor_lat, datum_lon=anchor_lon)[0]


def build_waypoints(route: list[a.RoutePoint]) -> list[a.Waypoint]:
    return [
        a.Waypoint(
            index=i,
            east_cm=round(p.x * 100.0),
            north_cm=round(p.y * 100.0),
            is_headland=p.is_headland,
            is_reverse=p.is_reverse,
        )
        for i, p in enumerate(route)
    ]


def route_xy(route: list[a.RoutePoint]) -> list[tuple[float, float]]:
    return [(p.x, p.y) for p in route]


def estimate_progress_index(
    status: a.MachineStatus,
    xy: list[tuple[float, float]],
    anchor_lat: float,
    anchor_lon: float,
    previous: int,
    nearest_backtrack: int,
    nearest_ahead: int,
) -> int:
    """Monotonic GPS progress using projection onto nearby route segments."""
    if status.gps_lat is None or status.gps_lon is None or len(xy) < 2:
        return previous

    px, py = a.wgs_to_enu_approx(status.gps_lat, status.gps_lon, anchor_lat, anchor_lon)
    first_seg = max(0, previous - nearest_backtrack)
    last_seg = min(len(xy) - 2, previous + nearest_ahead)
    best_progress = float(previous)
    best_dist2 = float("inf")

    for i in range(first_seg, last_seg + 1):
        x0, y0 = xy[i]
        x1, y1 = xy[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 == 0:
            continue
        t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg2))
        qx, qy = x0 + t * dx, y0 + t * dy
        dist2 = (px - qx) ** 2 + (py - qy) ** 2
        if dist2 < best_dist2:
            best_dist2 = dist2
            best_progress = i + t

    return min(len(xy) - 1, max(previous, int(best_progress + 0.5)))


def send_adjob(
    bus,
    active: bool,
    run_command: bool,
    current_index: int,
    total_points: int,
    job_id: int,
) -> None:
    a.send(
        bus,
        a.PGN_ADJOB,
        a.encode_adjob(
            system_active=active,
            run_command=active and run_command,
            current_index=current_index,
            total_points=total_points,
            job_id=job_id,
        ),
    )


def stream_range(bus, status: a.MachineStatus, waypoints: list[a.Waypoint], start: int, end: int) -> int:
    sent = 0
    for wp in waypoints[start:end]:
        a.drain_rx(bus, status, max_frames=5)
        a.send(bus, a.PGN_ADWPI, a.encode_adwpi(wp))
        sent += 1
        if a.SEND_INTERVAL_S > 0:
            time.sleep(a.SEND_INTERVAL_S)
    return sent


def update_range(trigger_index: int, total: int, window_size: int, overlap_before_trigger_points: int) -> tuple[int, int]:
    start = max(0, min(total, trigger_index - overlap_before_trigger_points))
    end = min(total, start + window_size)
    return start, end


def next_trigger_index(current_window_end: int, trigger_before_end_points: int) -> int:
    return max(0, current_window_end - trigger_before_end_points)


def print_route_banner(args: argparse.Namespace, point_count: int, actual_max_spacing: float, job_id: int) -> None:
    print(
        f"{args.route} route: {point_count} points, max spacing {actual_max_spacing:.3f} m "
        f"(limit {args.max_spacing:.3f} m), initial={args.initial_points}, "
        f"window={args.window_size}, trigger_before_end={args.trigger_before_end_points}, "
        f"overlap_before_trigger={args.overlap_before_trigger_points}, job_id={job_id}",
        file=sys.stderr,
    )
    if actual_max_spacing < MIN_SPACING_M:
        print(
            f"note: {actual_max_spacing:.3f} m spacing is below the AgJunction {MIN_SPACING_M} m minimum.",
            file=sys.stderr,
        )


def wait_for_anchor(
    bus,
    status: a.MachineStatus,
    args: argparse.Namespace,
    field: list[tuple[float, float]],
    datum_lat: float,
    datum_lon: float,
    point_count: int,
    job_id: int,
) -> bool:
    t0 = time.monotonic()
    last_adjob = -999.0
    active_sent = False
    while not (active_sent and status.anchor_lat is not None) and time.monotonic() - t0 < args.timeout:
        frame = bus.recv(timeout=0.05)
        if frame is not None:
            a.process_frame(frame, status)
        now = time.monotonic() - t0
        inside = args.no_inside_gate or inside_field(status, field, datum_lat, datum_lon)
        active = status.gps_ppp_available and status.autodrive_allowed and inside
        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            if active and not active_sent:
                active_sent = True
                status.anchor_lat = None
                status.anchor_lon = None
            send_adjob(bus, active, False, 0, point_count, job_id)
            print(
                f"[{now:5.1f}s] ADJOB active={'Y' if active else '-'} run=- "
                f"job_id={job_id} total_points={point_count} "
                f"(ppp={'Y' if status.gps_ppp_available else '-'} "
                f"allowed={'Y' if status.autodrive_allowed else '-'} "
                f"inside={'skip' if args.no_inside_gate else ('Y' if inside else '-')})",
            )
    return active_sent and status.anchor_lat is not None and status.anchor_lon is not None


def main() -> None:
    args = parse_args()
    validate_args(args)

    route_path, gate_route, datum_lat, datum_lon = route_for_max_spacing(args.route, args.max_spacing)
    point_count = len(gate_route)
    actual_max_spacing = max_route_spacing(gate_route)
    if point_count > a.PROTOCOL_U16_MAX:
        raise SystemExit(f"{args.route} has {point_count} points after interpolation; protocol max is {a.PROTOCOL_U16_MAX}")

    field = routes.bounding_field(gate_route, FIELD_MARGIN_M)
    job_id = args.job_id if args.job_id is not None else int(time.time()) % (a.PROTOCOL_U16_MAX + 1)
    print_route_banner(args, point_count, actual_max_spacing, job_id)

    bus = a.make_bus()
    status = a.MachineStatus()
    if not wait_for_anchor(bus, status, args, field, datum_lat, datum_lon, point_count, job_id):
        print("no anchor — cannot stream. Check PPP, AutoDrive allowed, inside gate, and Display/simulator.")
        return

    anchor_lat, anchor_lon = status.anchor_lat, status.anchor_lon
    if anchor_lat is None or anchor_lon is None:
        print("no anchor — cannot stream. Is the Display/simulator running?")
        return
    route = load_line_from_anchor(route_path, args.max_spacing, anchor_lat, anchor_lon)
    waypoints = build_waypoints(route)
    xy = route_xy(route)
    print(f"anchored at {anchor_lat:.7f},{anchor_lon:.7f}; {len(waypoints)} waypoints ready\n")

    preload_end = min(len(waypoints), args.initial_points)
    total_frames = stream_range(bus, status, waypoints, 0, preload_end)
    sent_until = preload_end
    current_window_end = preload_end
    batch_no = 1
    current_index = 0
    run_command = sent_until >= min(a.FUTURE_POINT_COUNT, len(waypoints))
    print(
        f"batch {batch_no} preload: sent [0..{preload_end - 1}], frames={preload_end}, "
        f"sent_until={sent_until}, next_trigger={next_trigger_index(current_window_end, args.trigger_before_end_points)}"
    )

    t0 = time.monotonic()
    loop_started = time.monotonic()
    last_adjob = -999.0
    last_report = -999.0
    while current_index < len(waypoints) - 1:
        if args.loop_timeout and time.monotonic() - loop_started >= args.loop_timeout:
            print(f"loop timeout after {args.loop_timeout:.1f}s")
            break

        frame = bus.recv(timeout=0.02)
        if frame is not None:
            a.process_frame(frame, status)

        active = (
            status.gps_ppp_available
            and status.autodrive_allowed
            and (args.no_inside_gate or inside_field(status, field, anchor_lat, anchor_lon))
        )
        current_index = estimate_progress_index(
            status,
            xy,
            anchor_lat,
            anchor_lon,
            current_index,
            args.nearest_backtrack,
            args.nearest_ahead,
        )

        trigger = next_trigger_index(current_window_end, args.trigger_before_end_points)
        if active and sent_until < len(waypoints) and current_index >= trigger:
            start, end = update_range(
                trigger,
                len(waypoints),
                args.window_size,
                args.overlap_before_trigger_points,
            )
            sent = stream_range(bus, status, waypoints, start, end)
            previous_sent_until = sent_until
            sent_until = max(sent_until, end)
            current_window_end = end
            total_frames += sent
            batch_no += 1
            print(
                f"batch {batch_no} update: progress={current_index}/{len(waypoints) - 1} "
                f"trigger={trigger}, sent [{start}..{end - 1}], frames={sent}, "
                f"new=[{previous_sent_until}..{sent_until - 1}], "
                f"ahead_after={sent_until - current_index}"
            )

        now = time.monotonic() - t0
        adjob_index = min(len(waypoints) - 1, current_index + args.adjob_ahead_points)
        if now - last_adjob >= a.ADJOB_PERIOD_S:
            last_adjob = now
            send_adjob(bus, active, run_command, adjob_index, len(waypoints), job_id)
        if now - last_report >= 1.0:
            last_report = now
            print(
                f"[{now:6.1f}s] active={'Y' if active else '-'} "
                f"run={'Y' if (active and run_command) else '-'} "
                f"progress={current_index}/{len(waypoints) - 1} "
                f"adjob_index={adjob_index} sent_until={sent_until}/{len(waypoints)} "
                f"next_trigger={trigger if current_window_end < len(waypoints) else 'done'} "
                f"ahead={sent_until - current_index} spd={status.speed_kph or 0.0:.1f}kph"
            )

        if current_window_end >= len(waypoints) and current_index >= len(waypoints) - 1:
            break

    print(
        f"\nfinished: progress={current_index}/{len(waypoints) - 1}, "
        f"sent_unique={sent_until}/{len(waypoints)}, batches={batch_no}, "
        f"frames={total_frames}"
    )


if __name__ == "__main__":
    main()
