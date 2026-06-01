# AutoSteer — GP AutoDrive bring-up runbook

A self-contained implementation of the GP (Oxbo) AutoDrive CAN protocol, plus a
**step-by-step commissioning procedure** for a real machine. You run the scripts
**in order**; each step has a **PASS gate**. *Do not move to the next step until
the current one passes.* If a step fails, fix the cause (each step lists the
usual ones) and re-run that same step.

This folder has no external dependencies beyond `python-can` — the route is a
built-in test path (a straight line or a U-turn), not a field planner. The byte
reference for everything is **[PROTOCOL.md](PROTOCOL.md)**; the protocol code is
in **[`autosteer.py`](autosteer.py)**.

> ⚠️ **SAFETY.** Steps 1–8 are passive or only transmit data — the machine does
> not move. **Steps 9–10 make the machine drive and steer.** From step 9 on:
> clear the area, keep a hand on the e-stop, and have a second person watching.

---

## One switch: `CAN_BUS`

Every script reads the SocketCAN interface from the `CAN_BUS` environment
variable. That is the only thing that changes between bench and machine:

```sh
export CAN_BUS=vcan0     # bench test (virtual bus + the simulator)
export CAN_BUS=can0      # the real machine
export CAN_BUS=fake      # print TX frames only, never receive (offline inspection)
```

Default if unset: `vcan0`.

---

## Bench setup (rehearse with no machine)

Do the whole sequence on a **virtual CAN** bus first — it behaves exactly like a
real one, and the included simulator plays the Display.

1. **Create the virtual bus** (once per boot):

   ```sh
   sudo modprobe vcan
   sudo ip link add dev vcan0 type vcan
   sudo ip link set up vcan0
   ```

2. **Start the fake Display** in its own terminal and leave it running:

   ```sh
   export CAN_BUS=vcan0
   ./simulator.py                 # or: ./simulator.py vcan0
   ```

   It emits VP1/VDS/DSSTAT, acquires GPS PPP after a few seconds, computes an
   anchor when you activate, and drives a virtual machine along the waypoints you
   stream.

3. **Run the steps** in another terminal (`export CAN_BUS=vcan0` there too).

`candump vcan0` in a third terminal shows the raw traffic, exactly as on a real
bus.

---

## Real-machine setup

1. Bring up the real interface at the machine's bitrate (J1939 is usually
   **250 kbit/s**):

   ```sh
   sudo ip link set can0 type can bitrate 250000
   sudo ip link set can0 up
   export CAN_BUS=can0
   ```

   Do **not** run `simulator.py` — the machine's real Display provides the traffic.

2. `candump can0` in a second terminal to watch traffic independently.

3. Edit the field box / datum in [`06_activate_and_anchor.py`](06_activate_and_anchor.py)
   and [`10_full_run.py`](10_full_run.py) to your **actual field**; the
   inside-field gate uses them.

4. Confirm the AutoDrive **source address** (proposal says `29 ?`) in
   [`autosteer.py`](autosteer.py) (`SOURCE_AUTODRIVE`).

---

## The sequence

Each step: **what it proves → command → what you should see → ✅ PASS gate →
❌ if it fails.**

### Step 1 — Can we put a frame on the bus at all?

`01_basic_can.py` builds one ADJOB (systemActive=false) and transmits it.

```sh
./01_basic_can.py
```

You should see the id break down (priority 6 / pgn 0xFFCC / source 29) and one
`TX` line. In `candump` you should see exactly one `18FFCC1D` frame.

- ✅ **PASS:** the script exits cleanly **and** the frame shows up in `candump`.
- ❌ **FAIL:** `python-can missing` → wrong shell; `Network is down` → interface
  not up; frame never in `candump` → wrong `CAN_BUS`, bus not terminated (120 Ω
  each end on a real bus), or wrong bitrate.

**→ Only if the frame is on the wire, go to Step 2.**

### Step 2 — Is the Display talking to us?

`02_listen_raw.py` listens ~6 s and tallies frames by PGN.

```sh
./02_listen_raw.py
```

- ✅ **PASS:** a steady tally of **VP1** (~10/s), **VDS** (~5/s) and **DSSTAT**
  (~5/s). (On the bench, that means `simulator.py` is running.)
- ❌ **FAIL:** zero frames → wrong bus/bitrate, or the Display/simulator isn't
  running. Only some PGNs → note which; a later step that needs it will fail too.

**→ Only if VP1 + VDS + DSSTAT are arriving, go to Step 3.**

### Step 3 — Is the GPS position correct?

`03_get_location.py` decodes VP1/VDS and prints lat/lon/heading/speed.

```sh
./03_get_location.py
```

Early lines may read **"no fix yet"** while PPP is acquired — normal.

- ✅ **PASS:** lat/lon resolve to your **actual position** (cross-check a phone
  GPS, agree within tens of metres); speed/heading sane when moving.
- ❌ **FAIL:** wildly wrong position → lat/lon decode offset or VP1 PGN; stuck at
  "no fix" → no PPP yet, or VP1 reporting `0xFFFFFFFF`.

**→ Only if the position is right, go to Step 4.**

### Step 4 — Are the gate bits readable and true?

`04_read_status.py` decodes DSSTAT. The two that matter are **GPS PPP available**
and **AutoDrive allowed** (the latter is operator-enabled on the display).

```sh
./04_read_status.py
```

- ✅ **PASS:** both `PPP=Y` **and** `allowed=Y` (have the operator enable field
  mode + AutoDrive option); other bits decode without garbage.
- ❌ **FAIL:** `allowed` never Y even when enabled → you're masking a 2-bit J1939
  field with a single bit (PROTOCOL.md §5.1). The decode here is corrected; if it
  still misreads, the bit positions differ from the proposal — ask the vendor.

**→ Only if PPP and allowed both read Y on demand, go to Step 5.**

### Step 5 — Does the Display accept our heartbeat?

`05_send_adjob.py` transmits ADJOB at 1 Hz with **systemActive=off**.

```sh
./05_send_adjob.py
```

- ✅ **PASS:** ADJOB once per second (`byte2=0x00`), and the **display recognises
  an AutoDrive node** (check its UI). No bus errors.
- ❌ **FAIL:** display ignores us → wrong source address (`29 ?`) or PGN.

**→ Only if the display sees us, go to Step 6.**

### Step 6 — Can we start a job and get an anchor?

`06_activate_and_anchor.py` runs the activation gate (PPP + AutoDrive allowed +
inside field + route loaded), sends **systemActive=true** (RunCommand still OFF),
and waits for the Display to broadcast a **DSAP anchor**.

> Set `FIELD_ENU` / `DATUM_*` to your real field and place the machine inside it.

```sh
./06_activate_and_anchor.py
```

- ✅ **PASS:** `✓ anchor received` near the field. Machine has **not moved**.
- ❌ **FAIL:** `inside=-` forever → wrong field polygon/datum or machine outside;
  active on but no anchor → Display didn't start a job (already in one? rejecting
  our systemActive?).

**→ Only once you reliably get an anchor, go to Step 7.**

### Step 7 — Are our waypoint coordinates correct? (offline)

`07_coordinates.py` needs **no bus**. It converts a sample route to
anchor-relative cm, packs ADWPI, and decodes it back.

```sh
./07_coordinates.py
```

- ✅ **PASS:** every point round-trips exactly, magnitudes within the ADWPI range
  (≈ −2.5 km … +8 km of the anchor — PROTOCOL.md §5.2).
- ❌ **FAIL:** offsets beyond range → anchor too far from field (the `−250000 cm`
  offset question).

**→ Once the packing checks out, go to Step 8.**

### Step 8 — Does the line appear on the Display in the right place?

`08_stream_waypoints.py` activates, takes the anchor, and **streams the first
100-point window** (3-point overlap, 10 ms pacing). The machine does **not** move.

```sh
./08_stream_waypoints.py
```

Watch the **display's map**.

- ✅ **PASS:** a line appears **at the correct location and orientation**; ~100
  ADWPI frames over ~1 s.
- ❌ **FAIL:** line mirrored/rotated/offset → east/north **sign** error or
  datum/anchor mismatch. Fix it here — a wrong line here is a wrong line under the
  wheels at step 9. No line at all → Display not accepting ADWPI, or never got
  systemActive/anchor.

**→ Only when the line is drawn correctly, go to Step 9.**

### Step 9 — Run and track (⚠️ THE MACHINE WILL MOVE)

> **STOP.** Area clear. E-stop in hand. Second person watching. Machine placed
> **right in front of the line's start point**.

`09_run_and_track.py` runs the full loop on a straight test line: activate →
stream first window → raise **RunCommand** → machine creeps forward (~1 kph),
AutoSteer engages, progress tracked from GPS, window re-streamed as it advances.

```sh
./09_run_and_track.py
```

- ✅ **PASS:** machine drives the line, `engaged=Y`, progress climbs to the end,
  **no reject code** (DSSTAT reject reason stays 0).
- ❌ **FAIL / ABORT:** doesn't engage + a **reject reason** → read it (PROTOCOL.md
  §9); reset RunCommand and retry. Steers wrong / off the line → **e-stop now**,
  it's the sign/orientation bug from step 8.

**→ Only once it tracks a straight line cleanly, go to Step 10.**

### Step 10 — Full run, with a turn

`10_full_run.py` runs the identical loop on a complete route. Pick it at the top:

```python
ROUTE = "uturn"     # "line" | "uturn"
```

`uturn` is two legs joined by a 180° **headland turn** — it exercises the headland
flag (`[H]` in the progress line) and a real curve, which is the realistic
"next swath" case.

```sh
./10_full_run.py
```

- ✅ **PASS:** the machine drives the whole route including the turn, the headland
  flag shows over the curve, progress tracked, no reject codes.
- ❌ **FAIL:** same diagnosis as step 9. If the curve loses steering, the turn may
  be too sharp — the proposal wants gentle curves and ≤30°/segment (PROTOCOL.md
  §8.5).

`LOOP_TIMEOUT_S` is a bench auto-stop; set it to `None` for an unbounded run.

---

## Quick reference

| Step | Script | Proves | PASS gate |
|------|--------|--------|-----------|
| 1 | `01_basic_can.py` | we can transmit | frame seen in `candump` |
| 2 | `02_listen_raw.py` | Display is talking | VP1+VDS+DSSTAT arriving |
| 3 | `03_get_location.py` | GPS decodes right | lat/lon matches reality |
| 4 | `04_read_status.py` | gate bits readable | PPP=Y and allowed=Y on demand |
| 5 | `05_send_adjob.py` | Display accepts heartbeat | Display sees AutoDrive node |
| 6 | `06_activate_and_anchor.py` | job starts | DSAP anchor received |
| 7 | `07_coordinates.py` | cm packing correct | round-trip exact, in range |
| 8 | `08_stream_waypoints.py` | line drawn correctly | correct line on Display map |
| 9 | `09_run_and_track.py` | **machine follows line** | engaged, tracks, no reject |
| 10 | `10_full_run.py` | **full route incl. turn** | drives line + headland turn |

## Files

| File | Role |
|------|------|
| [`PROTOCOL.md`](PROTOCOL.md) | Protocol reference — every PGN, byte, bit, the sequence. |
| [`GP_AutoDrive_CanMessageProposal_V10.pdf`](GP_AutoDrive_CanMessageProposal_V10.pdf) | The authoritative vendor proposal (byte tables). |
| [`autosteer.py`](autosteer.py) | Shared library: encoders/decoders, J1939, CAN transport. `SOURCE_AUTODRIVE` lives here. |
| [`routes.py`](routes.py) | Built-in test routes: `straight_line()`, `u_turn()`. No planner. |
| [`simulator.py`](simulator.py) | Fake Display — run it on `vcan0` for bench testing. |
| `01`…`10_*.py` | The bring-up steps above. |

## Open vendor questions (resolve before relying on this in the field)

- AutoDrive **source address** — proposal says `29 ?` (`SOURCE_AUTODRIVE`).
- Which value of DSSTAT **Current Direction** means *reverse*.
- ADWPI coordinate offset annotation: `−250000 cm` vs the "−25 km" gloss.
- Exact bit positions of the ADWPI byte-8 flags.
