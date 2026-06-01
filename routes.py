"""
routes.py — routes for bench and field runs.

Two sources, same output type — a list of autosteer.RoutePoint in local ENU
metres (x = east, y = north) with is_headland / is_reverse flags:

  * GeoJSON LineStrings (line.geojson, u_field.geojson) — real paths in WGS84,
    loaded via geojson_route(); this is what the steps use by default.
  * straight_line() / u_turn() — synthetic fallbacks, no file needed.

The bridge converts either to anchor-relative centimetres at run time.
"""

from __future__ import annotations

import json
import math
import os

import autosteer as a

_HERE = os.path.dirname(os.path.abspath(__file__))

# Named GeoJSON routes shipped alongside this file.
GEOJSON = {
    "line": "line.geojson",
    "uturn": "u_field.geojson",
}


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


# =============================================================================
# GEOJSON ROUTES
# =============================================================================

WAYPOINT_SPACING_M = 0.5        # AgJunction wants 0.3-4.5 m (PROTOCOL.md §8.5)
HEADLAND_WINDOW_M = 2.5         # span used to measure turn rate
HEADLAND_TURN_DEG = 12.0        # heading change over that span => headland
HEADLAND_GAP_CLOSE_M = 6.0      # bridge short non-flagged gaps in the turn band


def geojson_path(name: str) -> str:
    """Absolute path of a named GeoJSON route (e.g. "line", "uturn")."""
    return os.path.join(_HERE, GEOJSON[name])


def geojson_datum(path: str) -> tuple[float, float]:
    """(lat, lon) of the LineString's first vertex — used as the ENU datum."""
    lon, lat = _load_linestring(path)[0]
    return lat, lon


def geojson_route(path: str, spacing_m: float = WAYPOINT_SPACING_M
                  ) -> tuple[list[a.RoutePoint], float, float]:
    """
    Load a GeoJSON LineString as a route. Returns (route, datum_lat, datum_lon).

    The first vertex is the datum, so the route starts at ENU origin (same
    convention as the synthetic routes). Vertices are converted to ENU metres,
    resampled to `spacing_m`, and headland-flagged where the path turns.
    """
    coords = _load_linestring(path)
    datum_lon, datum_lat = coords[0]
    enu = [a.wgs_to_enu_approx(lat, lon, datum_lat, datum_lon) for lon, lat in coords]
    resampled = _resample(enu, spacing_m)
    return _mark_headland(resampled, spacing_m), datum_lat, datum_lon


def _load_linestring(path: str) -> list[tuple[float, float]]:
    """Pull the first LineString's [lon, lat] vertices out of a GeoJSON file."""
    gj = json.load(open(path))
    geom = gj["features"][0]["geometry"]
    if geom["type"] != "LineString":
        raise ValueError(f"{path}: expected a LineString, got {geom['type']!r}")
    return [(float(lon), float(lat)) for lon, lat in geom["coordinates"]]


def _resample(vertices: list[tuple[float, float]], spacing_m: float
              ) -> list[tuple[float, float]]:
    """Resample a polyline to ~`spacing_m` even steps, endpoints included."""
    if len(vertices) < 2:
        return list(vertices)
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    total = cum[-1]
    n = max(1, round(total / spacing_m))
    out, seg = [], 0
    for k in range(n + 1):
        target = min(total, k * total / n)
        while seg < len(cum) - 2 and cum[seg + 1] < target:
            seg += 1
        seg_len = cum[seg + 1] - cum[seg]
        f = 0.0 if seg_len == 0 else (target - cum[seg]) / seg_len
        x0, y0 = vertices[seg]
        x1, y1 = vertices[seg + 1]
        out.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))
    return out


def _mark_headland(enu: list[tuple[float, float]], spacing_m: float
                   ) -> list[a.RoutePoint]:
    """Flag is_headland where the path turns: heading change over a short span
    exceeds HEADLAND_TURN_DEG, with small gaps closed into one band."""
    pts = [a.RoutePoint(x, y) for x, y in enu]
    w = max(1, round(HEADLAND_WINDOW_M / spacing_m))

    def heading(p, q):
        return math.atan2(q[0] - p[0], q[1] - p[1])

    for i in range(w, len(pts) - w):
        a1 = heading(enu[i - w], enu[i])
        a2 = heading(enu[i], enu[i + w])
        turn = abs(math.degrees(math.atan2(math.sin(a2 - a1), math.cos(a2 - a1))))
        if turn >= HEADLAND_TURN_DEG:
            pts[i].is_headland = True

    gap = max(1, round(HEADLAND_GAP_CLOSE_M / spacing_m))
    flagged = [i for i, p in enumerate(pts) if p.is_headland]
    for lo, hi in zip(flagged, flagged[1:]):
        if 0 < hi - lo <= gap:
            for k in range(lo + 1, hi):
                pts[k].is_headland = True
    return pts


# Route registry. Each entry returns (route, datum_lat, datum_lon).
ROUTES = {
    "line": lambda: geojson_route(geojson_path("line")),
    "uturn": lambda: geojson_route(geojson_path("uturn")),
}
