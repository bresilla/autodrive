# GP AutoDrive CAN Protocol

A from-scratch guide to the GP (Oxbo / GPH) AutoDrive CAN protocol, building from
"what is a CAN frame" up to the full message flow implemented in the numbered
bring-up scripts here (`01_*.py` … `10_full_run.py`) and the shared library
[`autodrive.py`](autodrive.py).

Source of truth: [`spec/GP_AutoDrive_CanMessageProposal_V10.pdf`](spec/GP_AutoDrive_CanMessageProposal_V10.pdf).
This document is the prose explanation; the PDF is the authoritative byte tables.

> Status note: the field checklist [`spec/spec2.md`](spec2.md) (2 Jun 2026)
> resolved several items the original proposal left uncertain — the source
> address (we are **29 / `0x1D`**), the ADWPI offset (**−250000 cm**), and that
> the **RunCommand is not yet wired** (the machine is driven by hand; AutoDrive
> only steers). Two items remain open and are flagged inline as **⚠ verify with
> vendor**: the `Current Direction` reverse value and the ADWPI byte-8 flag bits.

---

## Table of contents

1. [The big picture](#1-the-big-picture)
2. [CAN and J1939 in five minutes](#2-can-and-j1939-in-five-minutes)
3. [Coordinate systems](#3-coordinate-systems)
4. [The message catalogue](#4-the-message-catalogue)
5. [Bit-field encoding rules](#5-bit-field-encoding-rules)
6. [Message-by-message byte layouts](#6-message-by-message-byte-layouts)
7. [The full sequence (state machine)](#7-the-full-sequence-state-machine)
8. [Waypoint streaming in detail](#8-waypoint-streaming-in-detail)
9. [Engage, run, and tracking](#9-engage-run-and-tracking)
10. [Feeding it a route](#10-feeding-it-a-route)
11. [Implementation checklist](#11-implementation-checklist)
12. [Glossary](#12-glossary)

---

## 1. The big picture

The harvester already has a working AutoDrive stack:

```
TOPCON AGS-2 receiver  ──serial NMEA──►  AgJunction ECU-S1  ──CAN──►  MC42 Propel (steering valve)
   (GPS + PPP, ~4-10cm)                  (pivot/header transform,           ▲
                                          line following, curvature)         │ engage + angle feedback
                                              ▲                               │
                                   Ethernet   │ line coords                   │
                                              │                               │
                                        OXBO Cab Display  ◄───────────────────┘
                                  (line management, map, alerts)
```

Normally the **Display** does "line management": it builds the AB line and its
parallels and feeds them to the AgJunction. The **AutoDrive computer** (the fifth
component we are building) takes that line-management role *partially* over.

So the conversation we implement is **AutoDrive ⇄ Display**, on **CAN bus 2 (the
Main Display bus)**. The AutoDrive computer:

- **listens** to the Display for GPS, status, and the anchor point, and
- **talks back** with a job-control message and a stream of waypoints.

That is the entire protocol: 6 message types, two of which we send.

---

## 2. CAN and J1939 in five minutes

**CAN frame.** A CAN message is an `arbitration_id` (the "address", 11 or 29 bits)
plus up to 8 data bytes. Lower ID = higher priority on the wire. This protocol uses
**29-bit extended IDs**.

**J1939** is a higher-layer convention on top of 29-bit CAN, used in agriculture and
trucking. It carves the 29-bit ID into fields:

```
 28        26 25                              8 7              0
┌───────────┬──────────────────────────────────┬───────────────┐
│ Priority  │            PGN (18 bits)          │ Source Address│
│  (3 bits) │     Parameter Group Number        │   (8 bits)    │
└───────────┴──────────────────────────────────┴───────────────┘
```

- **Priority** — 0 (highest) to 7. This protocol uses **6** for every message.
- **PGN** — *what kind of message this is*. E.g. ADWPI = PGN 65485 = `0xFFCD`.
- **Source Address (SA)** — *who sent it*. Display = **40** (`0x28`); AutoDrive =
  **29** (`0x1D`, the "In Field Planner"). Both confirmed by [`spec/spec2.md`](spec2.md).

Building the ID (from the script, `j1939_id`):

```python
arbitration_id = (priority << 26) | (pgn << 8) | source_address
#                 (6 << 26)        | (0xFFCD << 8) | 29   →  0x18FFCD1D
```

Extracting the PGN back out of a received frame (`pgn_from_id`):

```python
pgn = (arbitration_id >> 8) & 0x3FFFF
```

> We dispatch incoming frames purely by PGN — we ignore the source address on RX,
> because on this bus only the Display talks to us. The 29-bit form means the SA
> lives in the low byte, which is why shifting right by 8 isolates the PGN.

**DLC** is the data length (always 8 here). **Send rate** is how often a node
repeats the message; receivers treat a value as "current" until superseded.

---

## 3. Coordinate systems

Three coordinate frames appear in this protocol. Keeping them straight is the single
most important thing.

### 3.1 WGS84 degrees (absolute)

Plain latitude/longitude. Used by VP1 (machine position) and DSAP (anchor point).
Encoded as a `uint32` with a fixed scale and offset:

```
degrees = raw × 0.0000001 − 210.0          # 1e-7 deg/bit, offset −210°
raw     = 0xFFFFFFFF  ⇒  value unavailable  (signal dropped / system inactive)
```

The −210° offset lets the unsigned field cover the full −180…+180 range with margin.
Decode in the script is `decode_latlon_u32`.

### 3.2 Anchor-relative centimetres (local)

When a job starts, the Display computes an **anchor point** (field centre or current
machine position) and broadcasts it via DSAP. From then on, **all streamed waypoints
are expressed as centimetres east / north of that anchor** — a compact local frame.

Example from the proposal: `{ -51050, +912 }` means *510.5 m west, 9.12 m north of
the anchor*.

Why: a `int32` of absolute degrees is bulky and awkward to diff. Centimetre offsets
from a nearby anchor fit in 20 bits and are trivial to reason about.

> **Two machines can have two different anchors.** The anchor is per-job, per-machine.
> Never cache an anchor across jobs.

### 3.3 Route ENU metres (our planning frame)

We plan/define the route in a local **ENU** (east-north-up) metric frame relative to a
*datum* (`DATUM_LAT/LON`) — the built-in test routes ([`routes.py`](routes.py)) live in
this frame, and any external planner would too. The bridge's job is to convert route
ENU metres → anchor-relative centimetres. Because both are local tangent-plane frames,
the conversion is just a translation by the anchor's ENU position:

```python
anchor_e, anchor_n = wgs_to_enu_approx(anchor_lat, anchor_lon)   # anchor in datum-ENU metres
east_cm  = round((point.x - anchor_e) * 100.0)
north_cm = round((point.y - anchor_n) * 100.0)
```

`wgs_to_enu_approx` is a small-field flat-earth approximation (`111320 m/deg`,
longitude scaled by `cos(lat)`). Fine for a single field; not for continental
distances.

---

## 4. The message catalogue

Six messages matter for AutoDrive. All are on **bus 2 (Main Display)**, priority **6**,
DLC **8**.

| Abbr   | PGN     | Hex      | Dir (relative to us) | Rate            | Purpose                              |
|--------|---------|----------|----------------------|-----------------|--------------------------------------|
| VP1    | 65267   | `0xFEF3` | Display → AutoDrive  | 100 ms          | Machine GPS lat/lon (header centre)  |
| VDS    | 65256   | `0xFEE8` | Display → AutoDrive  | 200 ms          | Heading / speed / pitch / altitude   |
| DSSTAT | 65482   | `0xFFCA` | Display → AutoDrive  | 200 ms / change | DirectSteer status + header geometry |
| DSAP   | 65483   | `0xFFCB` | Display → AutoDrive  | 5 s / change    | Anchor (job/field) point             |
| ADJOB  | 65484   | `0xFFCC` | **AutoDrive → Display** | 1 s / change | Job state: active, run, progress, err|
| ADWPI  | 65485   | `0xFFCD` | **AutoDrive → Display** | streaming    | One waypoint per frame               |

> **VP1 PGN gotcha.** VP1 is `0xFEF3` (65267), *not* `0xFFEF`. An early version of the
> script had `0xFFEF`, which silently breaks GPS decoding (position stays `None`,
> activation never gates true). This is now fixed in the script and called out here
> because it is an easy transcription error.

The proposal also lists Obstacle Detection (ODSZ/`0xFFBE`), Weed/Trash (WTDSR), Tenderness
(CSAVAL…), and PeaSense PGNs — those are **out of scope** for the AutoDrive bridge.

---

## 5. Bit-field encoding rules

Two encoding conventions repeat across the messages. Get these right and everything
else is bookkeeping.

### 5.1 J1939 2-bit status fields

Many boolean-looking flags are actually **2-bit fields**, because J1939 distinguishes
four states, not two:

| Bits | Meaning  |
|------|----------|
| `00` | off / false / disabled |
| `01` | on / true / enabled    |
| `10` | error                  |
| `11` | not available          |

**Consequence:** you cannot test these with a single-bit mask. "True" means the pair
equals `01`, not "the high bit is set". For a field occupying bits *n* and *n−1*:

```python
is_true = (byte & pair_mask) == on_value
# e.g. bits 8-7 of byte1:  (b1 & 0xC0) == 0x40
#      bits 2-1 of byte1:  (b1 & 0x03) == 0x01
```

When **encoding** an "on", set the *lower* bit of the pair (value `01`), e.g.
RunCommand in bits 4-3 → `0x04`, SystemActive in bits 2-1 → `0x01`.

> Bit numbering here follows the proposal tables: **Bit 1 is the LSB, Bit 8 the MSB**
> of each byte. "Byte1 bits 2-1" = the two least-significant bits of the first data byte.

### 5.2 20-bit packed coordinates (ADWPI)

The east and north offsets are **20-bit** values at **1 cm/bit** with a **−250000 cm**
offset, packed across bytes 3-7 with a nibble split in byte 5:

```
raw   = value_cm − (−250000)      # = value_cm + 250000
value = raw + (−250000)           # decode

byte3 = east[7:0]
byte4 = east[15:8]
byte5 = east[19:16]  (low nibble)  |  north[3:0] (high nibble)
byte6 = north[11:4]
byte7 = north[19:12]
```

Range: 20 bits = 0…1048575 raw → −250000…+798575 cm → roughly **−2.5 km … +8.0 km**
from the anchor.

> **Offset confirmed.** The original spec sheet had **−25000 cm**, which was an
> error; the field checklist ([`spec/spec2.md`](spec2.md)) corrects it to
> **−250000 cm** — the value we implement. The accompanying "25 km" gloss is
> imprecise (−250000 cm is 2.5 km, and 20 bits can't span 25 km anyway), but the
> numeric offset is right.

---

## 6. Message-by-message byte layouts

All multi-byte integer fields are **little-endian** unless the packing is explicitly
bit-level (ADWPI coordinates).

### 6.1 VP1 — Vehicle Position 1  (`0xFEF3`, RX)

Standard SAE message; the header-centre ground position.

| Bytes | Field     | Encoding                         |
|-------|-----------|----------------------------------|
| 1-4   | Latitude  | u32, `1e-7 deg/bit`, offset −210 |
| 5-8   | Longitude | u32, `1e-7 deg/bit`, offset −210 |

`0xFFFFFFFF` in either field ⇒ GPS unavailable. Decoder: `decode_vp1`.

### 6.2 VDS — Vehicle Direction & Speed  (`0xFEE8`, RX)

| Bytes | Field         | Encoding                |
|-------|---------------|-------------------------|
| 1-2   | Compass bearing | u16, `1/128 deg/bit`, no offset |
| 3-4   | Ground speed  | u16, `1/256 km/h per bit`, no offset |
| 5-6   | Pitch angle   | u16, `1/128 deg/bit`, offset −200° |
| 7-8   | Altitude      | u16, `0.125 m/bit`, offset −2500 m |

The script decodes bearing and speed (`decode_vds`); pitch/altitude are available but
unused. `0xFFFF` in the compass or speed field means unavailable. Note the compass
may be wrong/undetermined before the machine has moved.

### 6.3 DSSTAT — DirectSteer Status  (`0xFFCA`, RX)

The status word the AutoDrive computer gates on, plus header geometry. **Byte1 and
Byte2 are 2-bit status fields** (see §5.1).

| Byte | Bits | Field                         | Notes |
|------|------|-------------------------------|-------|
| 1    | 8-7  | GPS PPP available             | gate condition |
| 1    | 6-5  | AutoDrive engaged             | feedback that steering actually took |
| 1    | 4-3  | Header down                   | |
| 1    | 2-1  | Current direction             | forward / reverse — ⚠ which value = reverse, verify |
| 2    | 8-3  | AutoDrive interrupt / reject reason | 6-bit code, 0 = none |
| 2    | 2-1  | AutoDrive allowed             | gate condition (field mode + operator option) |
| 3-4  | —    | Perpendicular distance to line | i16, `1 cm/bit`, offset −1000 cm |
| 5    | —    | Overlap setting               | `1 cm/bit`, offset −125 cm (set before job; fixed 18-25 cm) |
| 6    | —    | Picker reel width (shaft-to-shaft) | `1 cm/bit` |
| 7    | —    | lhTipDistance                 | `1 cm/bit`, no offset |
| 8    | —    | (continuation of byte7 field) | `1 cm/bit`, no offset |

Decoder: `decode_dsstat`. We use PPP-available, AutoDrive-allowed, AutoDrive-engaged,
header-down, current-direction, and reject-reason. The header geometry fields
(overlap, reel width, lhTipDistance) are informational for path placement and are not
currently consumed by the bridge.

### 6.4 DSAP — DirectSteer Anchor Point  (`0xFFCB`, RX)

| Bytes | Field          | Encoding                         |
|-------|----------------|----------------------------------|
| 1-4   | Anchor latitude  | u32, `1e-7 deg/bit`, offset −210 |
| 5-8   | Anchor longitude | u32, `1e-7 deg/bit`, offset −210 |

The proposal says `0xFFFFFFFF` while the system is inactive. On the real machine
we have also observed DSAP as encoded `0.0,0.0` (`00 75 2B 7D 00 75 2B 7D`) before
a usable field anchor exists. Treat both forms as "no valid anchor yet". The
arrival of a valid anchor is the trigger to (re)compute the local-frame waypoint
table. Decoder: `decode_dsap`.

### 6.5 ADJOB — AutoDrive Job  (`0xFFCC`, **TX**)

Our job-state heartbeat. Sent every 1 s or on state change.

| Byte | Bits | Field                | Encoding |
|------|------|----------------------|----------|
| 1    | —    | (reserved)           | 0 |
| 2    | 8-5  | Error code           | 4-bit nibble, 0 = ok (machine halts if ≠ 0) |
| 2    | 4-3  | RunCommand           | 2-bit, on = `01` → `0x04` |
| 2    | 2-1  | SystemActive         | 2-bit, on = `01` → `0x01` |
| 3-4  | —    | Current waypoint index | u16, the point we are **going to** (0 = first), *not* the last passed point |
| 5-6  | —    | Line total point count | u16, **≤ 65530** |
| 7-8  | —    | Job ID               | u16, 0…65530 |

Encoder: `encode_adjob`. The byte2 packing is the most error-prone part:

```python
b[1] = ((error_code & 0x0F) << 4) | (0x04 if run_command else 0) | (0x01 if system_active else 0)
```

> Earlier the script used `0x08` for RunCommand. In a 2-bit field at bits 4-3, `0x08`
> is the pattern `10` = **error**, not `01` = on. Use `0x04`. Now fixed.

> **Job ID starts the job** (per [`spec/spec2.md`](spec2.md)). The Job ID must
> change when you move to a different field; a **change of Job ID together with
> SystemActive** is what makes the display start a *new* job (recompute anchor,
> clear the coverage map). The anchor scripts default to a fresh time-based Job ID;
> pass `--job-id` when you intentionally want to reuse or control it.
>
> **SystemActive** means, to the display: we have confirmed *PPP ready* and have
> *lines ready to stream*. **Current waypoint index** is the point the machine is
> heading toward (index `0` = the very first), which is what the display
> highlights and what we later feed to the AgJunction.

### 6.6 ADWPI — AutoDrive Waypoint Info  (`0xFFCD`, **TX**)

One waypoint per frame. See §5.2 for the 20-bit packing.

| Bytes | Field                       | Encoding |
|-------|-----------------------------|----------|
| 1-2   | Point index                 | u16, 0 = first waypoint of the whole line |
| 3-5.5 | East coordinate to anchor   | 20-bit, `1 cm/bit`, offset −250000 cm |
| 5.5-7 | North coordinate to anchor  | 20-bit, `1 cm/bit`, offset −250000 cm |
| 8     | Flags                       | bit-fields, see below |

Byte 8 flags (2-bit J1939 fields, "on" = `01`):

| Bits | Field             | Mask | Meaning |
|------|-------------------|------|---------|
| 2-1  | Is reverse point  | `0x01`-ish | machine must travel in reverse to reach this point |
| 4-3  | Is headland point | `0x04`-ish | lift header, sharp turn coming, reduce speed |
| rest | Reserved          | —    | future: speed limit, extra steer instructions |

Encoder: `encode_adwpi`. The flag constants live near the top of the script
(`ADWPI_FLAG_REVERSE = 0x01` at bits 2-1, `ADWPI_FLAG_HEADLAND = 0x04` at bits 4-3,
matching the table above) and are deliberately editable because their exact bit
positions were among the questioned items.

> Index semantics: `index[0]` is the very first point of the entire line. If the line
> has 6000 points, `index[5999]` is the last. Indices are stable for the whole job.

---

## 7. The full sequence (state machine)

The choreography from the proposal's sequence table:

```
┌─ Machine in field, not started ────────────────────────────────────────────┐
│  AutoDrive TX: ADJOB (systemActive=false)                                    │
│  Display  RX:  VP1 + VDS (position/speed/compass)                            │
│                DSSTAT (waiting for GPS PPP)                                   │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │  gate conditions met (see below)
                                   ▼
┌─ Activate ────────────────────────────────────────────────────────────────┐
│  AutoDrive TX: ADJOB (systemActive=TRUE, RunCommand still OFF)              │
│  Display does: compute anchor, clear coverage map, reset lines             │
│  Display  RX:  DSAP (anchor point)                                          │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │  anchor received
                                   ▼
┌─ Stream first batch ────────────────────────────────────────────────────────┐
│  AutoDrive TX: ADWPI × N  (first ≥100 points, anchor-relative cm)            │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │  ≥100 points streamed
                                   ▼
┌─ Run ────────────────────────────────────────────────────────────────────────┐
│  AutoDrive TX: ADJOB (systemActive=TRUE + RunCommand=TRUE)                    │
│  Operator drives forward 1-2 kph (RunCommand not wired yet, §9);             │
│  AutoDrive then engages and steers                                            │
│  Display  RX:  DSSTAT (AutoDrive engaged = true), VP1/VDS                     │
└──────────────────────────────────────────────────────────────────────────────┘
                                   │  as machine advances
                                   ▼
┌─ Track & re-stream ───────────────────────────────────────────────────────────┐
│  AutoDrive TX: ADJOB (current waypoint index = progress)                      │
│                ADWPI × N  (next batch as points are consumed)                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Activation gate (the four conditions of §4)

`SystemActive` may turn **on** only when *all* hold (`ready_for_system_active`):

1. **GPS PPP available** — `DSSTAT` bits 8-7 of byte1 == on. (`REQUIRE_GPS_PPP`)
2. **AutoDrive allowed** — `DSSTAT` byte2 bits 2-1 == on; operator + field mode.
   (`REQUIRE_AUTODRIVE_ALLOWED`)
3. **Machine inside field** — VP1 position, converted to ENU, passes a
   point-in-polygon test against the field boundary. (`REQUIRE_INSIDE_FIELD`,
   `point_inside_field`)
4. **Waypoints available** — we have a route loaded before the loop.

The `REQUIRE_*` flags exist to relax individual gates for bench testing only.

### Run gate (§6)

`RunCommand` may turn **on** only when:

- GPS PPP available **and** AutoDrive allowed (same DSSTAT bits), **and**
- the system was activated before, **and**
- **at least the first 100 points have been streamed.**

In the script `run_command` is flipped true only after `send_future_window` has run at
least once, satisfying the "≥100 points streamed" rule.

---

## 8. Waypoint streaming in detail

### 8.1 Batching

You may stream the **entire** line, or a **rolling 100-point window**. The script uses
a window (`FUTURE_POINT_COUNT = 100`).

- "When passing point 75, send the next 100." → re-send the window as the machine
  advances (`RESEND_WINDOW_EVERY_S = 1.0`).
- Each window **overlaps the previous by ≥3 points** (`WINDOW_OVERLAP_POINTS = 3`) —
  required so the AgJunction's smoother has continuity. The script starts each window
  at `max(0, current_index − 3)`.

```python
start = max(0, current_index - WINDOW_OVERLAP_POINTS)
end   = min(len(waypoints), current_index + FUTURE_POINT_COUNT)
for point in waypoints[start:end]:
    bus.send(ADWPI(point));  time.sleep(SEND_INTERVAL_S)
```

### 8.2 Pacing

Only a small number of CAN frames can be committed at once, so pause **10 ms after
each frame** (`SEND_INTERVAL_S = 0.010`). Therefore:

- 100 points ≈ 1 second.
- An entire 20k-point line ≈ 200 seconds (slow! prefer the window).
- If too slow, the proposal permits sending 2 points per 10 ms tick to halve the time.
  The script does not currently do this.

### 8.3 Progress tracking

The machine's current index is estimated from GPS by nearest-point search over a small
window of the route, monotonic (never goes backwards) — `estimate_index`, bounded by
`NEAREST_BACKTRACK` / `NEAREST_AHEAD`. That index drives both the ADJOB progress field
(the point we report we are heading *to*, §6.5) and where the next ADWPI window starts.

> Keep `NEAREST_AHEAD` small (≈25 points). A search window wider than the machine's
> real per-step advance can snap onto a **parallel leg** of the route — the legs of a
> headland turn are only metres apart — and the monotonic clamp then makes that wrong
> jump permanent.

### 8.4 Line changes on the fly

If the planned line changes mid-job, **keep the points already passed** — only points
*ahead* of the current index may change; the total count may grow or shrink. Passed
points are never re-sent.

### 8.5 Curve-path limits (§6.1.2, towards the AgJunction)

The steering controller is fussy about geometry. For a curved line it wants:

- **Point spacing:** min **0.3 m**, max **4.5 m**.
- **Max delta angle between two segments:** **30°**.
- Up to 100 points per batch, ≥3-point overlap.

The bridge resamples to `WAYPOINT_SPACING_M = 0.5` m (inside the spacing band). The
30°-per-segment limit is *not* currently enforced — if your tour contains sharp turn
connectors, validate or smooth them before streaming. The proposal explicitly
recommends **gentle curves only** to start.

---

## 9. Engage, run, and tracking

> **RunCommand is not wired yet.** Per the field checklist
> ([`spec/spec2.md`](spec2.md)), the display currently does **nothing** with our
> RunCommand bit (you can exercise it from the display's Diagnose screen, but it
> does not propel the machine). So forward motion is **manual** — the operator
> drives the joystick — while the AgJunction does the **steering**. We still send
> RunCommand so the protocol is in place for when it is enabled. The bench
> simulator, by contrast, *does* drive the virtual machine on RunCommand so the
> loop can be rehearsed end to end.

How a run actually goes today:

- The AgJunction cannot engage from a standstill, so make a **flying start**: park
  ~5 m before the first point, drive straight at it on the joystick, and engage
  AutoDrive as you reach the point. **The first metre or two has no guidance.**
- Drive **slowly, 1–2 kph** — earlier curve tests were rough; turning the steering
  PID up may help. When AutoDrive actually engages, `DSSTAT` reports **AutoDrive
  engaged = true** (possibly a few seconds later). It's not guaranteed the
  AgJunction follows a curved line cleanly.
- **PPP must be true RTK** (the RTK icon purple) — a FLOAT fix is *not* good enough.

**Halting and resuming.** Ease off the joystick to stop. (Resetting the
`RunCommand` bit is how the AutoDrive side will halt once it is wired.) On resume,
the same flying-start caveat applies: the machine may pass a few waypoints and be
slightly off-line before re-engaging.

**Errors.** If AutoDrive refuses to engage, the machine halts and DSSTAT byte2 carries
an **interrupt/reject reason** code (manual override, bad GPS, cannot acquire line, …).
The AutoDrive side can retry by resetting RunCommand. Conversely, the AutoDrive system
reports its own faults via the **ADJOB error code** (byte2 bits 8-5); a non-zero error
code halts the machine.

---

## 10. Feeding it a route

The bridge is route-source agnostic. All it needs is an ordered list of points in
local ENU metres with two flags each — that's the `RoutePoint` type in
[`autodrive.py`](autodrive.py):

```python
RoutePoint(x, y, is_headland=False, is_reverse=False)   # x=east m, y=north m, from datum
```

The pipeline from there is fixed:

```
route: list[RoutePoint]   (ENU metres from datum — routes.py, or any planner)
        │  wait for DSAP anchor
        │  route_to_waypoints  → translate to anchor-relative cm (×100)
        ▼
waypoints: list[Waypoint]  (index, east_cm, north_cm, is_headland, is_reverse)
        │  encode_adwpi  → ADWPI frames
        ▼
CAN bus 2
```

- `is_headland=True` → ADWPI headland flag → header lifts, speed drops, sharp turn.
- `is_reverse=True` → ADWPI reverse flag → machine backs up to reach the point.

This folder ships two routes as **GeoJSON `LineString`s** — `line.geojson` and
`u_field.geojson` — loaded by [`routes.py`](routes.py)'s `geojson_route()`, which
takes the first vertex as the ENU datum, resamples to `WAYPOINT_SPACING_M` (0.5 m,
inside the §6.1.2 band), and flags the headland turn from the path's curvature. To
drive a real field plan, drop in your own GeoJSON line (or build the `RoutePoint`
list from any planner, keeping points within the §6.1.2 spacing/angle limits) and
hand it to the same loop; nothing else changes. The synthetic `straight_line()` /
`u_turn()` generators remain as no-file fallbacks.

---

## 11. Implementation checklist

A pragmatic order to bring this up on real hardware (see [README.md](README.md) for
the step-by-step runbook):

- [ ] **Bus up.** `export CAN_BUS=can0`, `python-can` available, confirm with
      `candump can0`. Bench-test first with `CAN_BUS=vcan0` + `./simulator.py`.
- [ ] **RX first.** Set all `REQUIRE_*` gates relevant and watch that VP1/VDS/DSSTAT/
      DSAP decode to sane values (lat/lon near the field, plausible speed/heading).
      If GPS stays `None`, re-check the VP1 PGN is `0xFEF3`.
- [ ] **Verify 2-bit decoding.** Compare decoded PPP/allowed/engaged against the
      display's own UI. If a flag reads false when the UI says true, you are masking a
      2-bit field with a single bit (§5.1).
- [ ] **TX ADJOB (inactive).** Send `systemActive=false` heartbeat; confirm the
      display sees the AutoDrive node.
- [ ] **Activate.** Let the gate conditions go true, send `systemActive=true`
      (RunCommand still off), confirm the display computes and broadcasts a **DSAP**
      anchor.
- [ ] **Stream first 100.** Send ADWPI window; verify the display draws the line in
      the right place (anchor-relative cm math is the usual culprit if it's offset or
      mirrored — check east/north sign and the 20-bit packing).
- [ ] **Run.** Set RunCommand (`0x04`). It does **not** move the machine yet (§9) —
      drive forward manually (1–2 kph, flying start) and confirm AutoDrive engaged in
      DSSTAT. **Have a person at the e-stop.**
- [ ] **Track.** Confirm ADJOB progress index advances and windows re-stream with the
      3-point overlap.
- [ ] **⚠ Resolve remaining vendor unknowns:** DSSTAT Current Direction reverse value,
      and the exact byte-8 flag bit positions. (Source address `29`/`0x1D` and the
      −250000 cm offset are confirmed by [`spec/spec2.md`](spec2.md).)

---

## 12. Glossary

| Term | Meaning |
|------|---------|
| **AutoDrive** | The new "Autonomous Driving Computer" — the node this bridge implements. |
| **DirectSteer / Display** | OXBO cab display; does line management + map; our CAN peer. |
| **AgJunction (ECU-S1)** | Steering ECU that follows the line and commands the wheels. |
| **MC42 Propel** | Actuates the steering valve; reports actual angle. |
| **PPP** | Precise Point Positioning — satellite GPS correction, ~4-10 cm. |
| **Anchor** | Per-job local origin; waypoints are cm east/north of it. |
| **Headland** | Field edge turn-strip; header lifts, machine turns, speed drops. |
| **Swath / AB line** | A pass across the field; the line the machine follows. |
| **Tour** | The full ordered path including turn connectors between swaths. |
| **ENU** | East-North-Up local tangent frame (our route/planning frame). |
| **PGN** | Parameter Group Number — J1939 message-type identifier. |
| **SA** | Source Address — J1939 sender identifier (low byte of the 29-bit ID). |
| **RunCommand** | The "go" bit; machine drives/steers only while it is on. |

---

*Implementation: the numbered scripts + [`autodrive.py`](autodrive.py).
Authoritative byte tables: [`spec/GP_AutoDrive_CanMessageProposal_V10.pdf`](spec/GP_AutoDrive_CanMessageProposal_V10.pdf).*
