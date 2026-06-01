"""
routes.py — built-in test routes for bench runs.

No planner, no maptrax — just enough geometry to prove the bridge works:
a straight line and a U-turn (two legs joined by a 180° headland turn).

Each route is a list of autosteer.RoutePoint in local ENU metres
(x = east, y = north), with is_headland / is_reverse flags where relevant.
The bridge converts these to anchor-relative centimetres at run time.
"""

from __future__ import annotations

import math

import autosteer as a


def straight_line(length_m: float = 80.0, spacing_m: float = 0.5) -> list[a.RoutePoint]:
    """A straight line heading north from the origin."""
    n = max(1, int(round(length_m / spacing_m)))
    return [a.RoutePoint(0.0, i * spacing_m) for i in range(n + 1)]


def u_turn(leg_len_m: float = 30.0, radius_m: float = 1.5,
           spacing_m: float = 0.5) -> list[a.RoutePoint]:
    """
    Two parallel legs joined by a 180° headland turn:

        leg 1: north along x=0 from y=0 to y=leg_len
        turn : semicircle (radius_m) over the top, marked is_headland
        leg 2: south along x=2*radius back to y=0

    The leg spacing equals the turn diameter (2*radius), so the turn is a clean
    half-circle — a realistic "next swath" headland turn.
    """
    gap = 2.0 * radius_m
    pts: list[a.RoutePoint] = []

    n = max(1, int(round(leg_len_m / spacing_m)))
    for i in range(n + 1):                       # leg 1, north
        pts.append(a.RoutePoint(0.0, i * spacing_m))

    cx, cy = radius_m, leg_len_m                  # turn centre
    arc_len = math.pi * radius_m
    steps = max(2, int(round(arc_len / spacing_m)))
    for i in range(1, steps + 1):                 # semicircle, angle pi -> 0
        ang = math.pi * (1.0 - i / steps)
        pts.append(a.RoutePoint(
            x=cx + radius_m * math.cos(ang),
            y=cy + radius_m * math.sin(ang),
            is_headland=True))

    for i in range(1, n + 1):                      # leg 2, south
        pts.append(a.RoutePoint(gap, leg_len_m - i * spacing_m))

    return pts


def bounding_field(route: list[a.RoutePoint], margin_m: float = 15.0) -> list[tuple[float, float]]:
    """A rectangular field polygon around a route, for the inside-field gate."""
    xs = [p.x for p in route]
    ys = [p.y for p in route]
    x0, x1 = min(xs) - margin_m, max(xs) + margin_m
    y0, y1 = min(ys) - margin_m, max(ys) + margin_m
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


ROUTES = {
    "line": straight_line,
    "uturn": u_turn,
}
