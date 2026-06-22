# Garage Gateway — Design & Development Log

A living record of how this app is built and everything that has changed
across releases, including App Store certification feedback and how each
note was resolved.

App id: `com.fredhill.garage-gateway` · Author: Fred Hill ·
Repo: https://github.com/fredhill/homey-garage-gateway

---

## 1. What it is

A Homey app for **local-network** control of [iSmartGate](https://www.ismartgate.com/)
(PRO / LITE / MINI) and GogoGate2 garage-door and gate controllers. No
cloud dependency — it talks directly to the hub on the LAN. Real-time
open/closed status, control from the tile or Flows, automatic temperature
and battery reporting for wireless tilt sensors, and gate support
(including momentary "pulse" gates).

Built in **Python** on Homey's Python runtime (SDK 3), not the Node.js
SDK — see the framework convention used across this project.

---

## 2. Architecture

### 2.1 Component map

```
GarageGatewayApp (app.py)
  └─ shared state: app.door_state[(gateway_id, door_id)] -> snapshot dict

Drivers:
  garage-gateway  (the hub)   — owns the API + polling loop
  garage-door     (a door)    — one per configured garage door
  garage-gate     (a gate)    — one per configured gate (new in 1.0.3)

Shared:
  opening_device.py — OpeningDeviceBase, the common door/gate behaviour
```

### 2.2 The hub & polling model — `drivers/garage-gateway/device.py`

One `GatewayDevice` owns the single `ismartgate` API instance and the
polling loop. After every successful poll it writes a per-door snapshot
into `app.door_state` and notifies every paired door **and** gate device
to refresh.

Polling cadence adapts to state:

| Condition | Interval |
|---|---|
| Any door/gate open | `poll_interval_open` (default 15 s) |
| All closed | `poll_interval_closed` (default 60 s) |
| ≥2 consecutive network errors | 120 s backoff |
| Credentials rejected | 600 s backoff + device marked unavailable |

The long credential backoff is deliberate: iSmartGate firmware can lock
the admin account after repeated failed logins, so we back off hard and
let a human fix the password rather than hammering with known-bad creds.

### 2.3 Shared opening behaviour — `opening_device.py`

A garage door and a sensor-backed gate are the same model (a position
sensor + an open/close actuation), so that logic lives once in
`OpeningDeviceBase`. Each driver's `device.py` subclasses it and supplies
its own Flow-card id map (`door_*` vs `gate_*`). The module sits at the
app root; the driver `device.py` files prepend the app root to `sys.path`
before importing it, so resolution doesn't depend on how the on-device
runtime configures `sys.path`.

### 2.4 Devices & capabilities

| Capability | Where | Notes |
|---|---|---|
| `door_status` (enum) | door, sensor-gate | open / closed / opening / closing / undefined (read-only tile) |
| `garagedoor_closed` (bool) | door, sensor-gate | HomeKit/Google compatible; the setter Flows toggle to actuate |
| `measure_temperature` | wireless sensor | added dynamically when the API reports it |
| `measure_battery` | wireless sensor | derived from sensor voltage (CR123A curve) |
| `alarm_battery` | wireless sensor | low-battery alarm (new in 1.0.3) |
| `button` | pulse gate | momentary one-pulse control for sensorless gates (new in 1.0.3) |
| `alarm_connectivity` | hub | raised when the hub is unreachable |

Wireless capabilities are added/removed **lazily** as the API reports (or
stops reporting) a sensor, so a wired door doesn't show empty sensor tiles.

### 2.5 Flow cards

Custom cards fill the gaps Homey doesn't auto-generate from capabilities:

- **Door triggers:** opened, closed, left-open, started-opening,
  started-closing, status-changed (status token).
- **Door conditions:** is-open, is-closed. **Action:** toggle.
- **Gate** mirrors the door set with `gate_*` ids, plus a **pulse** action
  for momentary gates.

`opening` / `closing` fire **optimistically** when the app sends a command,
because the hub's info endpoint only reports terminal open/closed — this is
disclosed in each card's hint.

### 2.6 Pairing

The hub pairs first (credentials entered on the app settings page, then
moved to Homey's encrypted device store and cleared from plaintext). Door
and gate drivers then list the hub's configured openings, **split by the
hub's `gate` flag** so each opening appears under exactly one driver.
Sensorless gates pair as a `button` device.

### 2.7 Security model

- Credentials live in the **encrypted device store**; the plaintext copy in
  app settings is cleared once pairing succeeds.
- The settings page treats the password as **write-only** (never read back
  into the DOM) and validates the host against a pattern that rejects URL
  schemes/paths.
- `permissions: []` — the app requests no special Homey permissions.
- Logs avoid credentials and full request URLs.

### 2.8 Key engineering decisions

- **Background-task safety net (`_spawn`)** — every fire-and-forget
  coroutine attaches a done-callback that retrieves and logs exceptions, so
  a stray error can never surface as an unretrieved-task crash. (Lesson
  carried over from the BenQ projector app.)
- **Command debounce** — consecutive open/close commands inside 1 s are
  rejected (and surfaced, not silently dropped) to protect the motor.
- **`opened` → `open` mapping** — the library reports `opened`; the
  capability enum expects `open`. Mapped at the write boundary, otherwise
  Homey silently rejects the value and the tile freezes.
- **Flow-card listeners registered once per driver** — see 1.0.3 fixes.

---

## 3. App Store certification history

The part worth keeping: what each reviewer flagged and how it was resolved.

| Build / ver | Outcome | Reviewer feedback | Resolution |
|---|---|---|---|
| v1.0.0 | Initial submission | — | Baseline: local control, status, temp/battery, core Flows. |
| v1.0.1 | **Rejected** | **Guideline 1.5 (icons):** app & driver icons had a solid colored background, which Homey renders as a solid black shape that hides the illustration. | Removed the background `<rect>` from every icon; redrew them as single-color silhouettes on a transparent background using `fill-rule="evenodd"` for the cut-out details. → v1.0.2 |
| v1.0.2 (Build 3) | **Approved** ✅ | **Guideline 1.4 (images), non-blocking:** the garage-door *driver image* (product photo) had a non-white background (driveway / wall / greenery visible). | Replaced the door image with one on a pure white background; also flattened the hub image to fully opaque white. → addressed in v1.0.3 |
| v1.0.3 (Build 4) | **Submitted** 2026-06-19 | _awaiting review_ | Addressed the 1.4 image note **and** shipped gate support + new Flows + audit fixes (below). |

Pattern so far: the reviewer is thorough about the icon (1.5) and image
(1.4) guidelines. Both are now satisfied — transparent single-color icons,
device-on-white driver images, `energy.batteries` declared.

---

## 4. Development log (chronological)

### v1.0.0 — Initial release (May 2026)
Local control of iSmartGate/GogoGate2 garage doors. `door_status` +
`garagedoor_closed` tile, automatic temperature/battery for wireless tilt
sensors, Flow triggers (opened / closed / left-open), conditions
(is-open / is-closed), toggle action. Pre-submission hardening borrowed
from the BenQ app: the `_spawn` exception net, adaptive polling, network
and credential backoff, and careful credential handling.

### v1.0.1 — Visual polish (2026-06-07)
Redesigned app and driver icons in an outline / negative-space style and
added product photos for the hub and door driver images. **Rejected** on
resubmission for guideline 1.5 (see table above).

### v1.0.2 — Icon background fix (Build 3, approved)
Removed the solid background fill so icons render as single-color
silhouettes on a transparent background. Ran a full security scan (clean).
**Approved and went live**, with a non-blocking 1.4 note about the
garage-door driver image background.

### v1.0.3 — Gate support, Flows & audit fixes (Build 4, submitted 2026-06-19)

**Features**
- **New `garage-gate` driver.** Openings the hub flags as gates pair as a
  dedicated Gate device. Sensor-backed gates get the full open/close model;
  sensorless/pulse gates become momentary **button** devices that fire a
  single `activate()`.
- **Shared `OpeningDeviceBase`** extracted; the door device became a thin
  subclass and the gate reuses the same base.
- **New Flow cards:** started-opening / started-closing triggers,
  status-changed trigger (status token), gate **pulse** action, and an
  `alarm_battery` low-battery alarm for wireless sensors.
- Hub gained `activate_door()`; snapshot now carries `gate` / `mode` /
  `permission`.

**Image / store (resolves the 1.4 note)**
- New garage-door and gate driver images on a pure white background
  (generated with Google "Nano Banana" / Gemini, then background-cleaned
  and resized to 500×500 + 75×75). Hub image flattened to opaque white.
  New transparent, single-color gate icon.

**Bugs found & fixed during the on-device test / audit**
1. **Multi-device crash (critical).** The Python SDK *raises*
   `AlreadyExists` if a Flow-card run listener is registered twice — so the
   per-device registration crashed the **second** door's `on_init` once
   more than one door was paired. Fixed by registering toggle / condition /
   pulse listeners **once per driver** and resolving the target from the
   card's hydrated `device` argument. (Latent since v1.0.2; surfaced when a
   wireless sensor was added as door 2.)
2. **GogoGate2 poll crash (high, present since v1.0.2).** `int(events)`
   raised `TypeError` because the library always returns `events=None` for
   GogoGate2 → every poll failed and the hub showed permanently
   unavailable. Fixed with `int(... or 0)`.
3. **Gates never refreshed.** The post-poll notifier only iterated the
   garage-door driver; gates would never update. Now notifies both drivers,
   and button gates no-op their refresh (no status capabilities).
4. **Sensorless gates lied in Flows.** toggle / is-open / is-closed on a
   button gate acted on meaningless state; they now raise a clear "use the
   Pulse action" error.

**Verification:** validated at `--level publish`, booted clean on a real
Homey Pro (both doors initialised, ~95 s polling, zero errors), then soaked
for a week-plus in daily use with no crashes before submission.

---

## 5. Known limitations & future ideas

- **Optimistic opening/closing** — these triggers fire on command, not on
  confirmed motion, because the hub's info endpoint only reports terminal
  states. Physical/remote operations won't show the transition. By design;
  disclosed in the card hints.
- **Door→gate reconfiguration** — if a user changes an opening's type in
  the iSmartGate web UI after pairing, the stale device lingers and the same
  opening could be paired under both drivers. Low-impact edge case; a future
  build could mark the stale device unavailable when the `gate` flag flips.
- **Camera** — doors can have an attached camera; a snapshot capability is a
  possible future enhancement.
- **Battery voltage scaling** assumes a CR123A (~3.0 V fresh, ~2.4 V
  cutoff); revisit if other sensor types appear.

---

## 6. Build, test & release

The `homey` CLI runs via `npx` (no global install); Python apps build in
Docker, so Docker Desktop must be running for `run` / `install` / `publish`.

```bash
cd ~/Developer/homey-garage-gateway

npx homey app validate --level publish   # structural check (publish bar)
npx homey app run                         # temporary live session + logs
npx homey app install                     # permanent local install on Homey
npx homey app publish                     # upload a build to the App Store
```

Release steps:
1. Bump `version` in `.homeycompose/app.json` and add a
   `.homeychangelog.json` entry.
2. `npx homey app validate --level publish`.
3. Commit, push.
4. `npx homey app publish` → answer **No** to the version-bump prompt (the
   version is already set; it then reuses the changelog entry).
5. Open the build link and submit for certification from the dashboard.

> Note: when publishing, the CLI only prompts for a changelog if it bumps
> the version for you. Set the version in compose beforehand and answer
> **No**, so it uses the `.homeychangelog.json` entry you wrote.
