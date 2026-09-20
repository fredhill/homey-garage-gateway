# Garage Gateway — Project History & Developer Handoff

A single, self-contained document for anyone picking this project up cold:
what it is, why it exists, how it is built, everything that has shipped,
and — most valuably — the things that were learned the hard way.

| | |
|---|---|
| **App ID** | `com.fredhill.garage-gateway` |
| **Store page** | https://homey.app/en-us/app/com.fredhill.garage-gateway/Garage-Gateway/ |
| **Repo** | https://github.com/fredhill/homey-garage-gateway |
| **Dev dashboard** | https://tools.developer.homey.app/apps/app/com.fredhill.garage-gateway |
| **Author** | Fred Hill |
| **Live version** | **1.0.7** (Build 8, live 2026-09-14) |
| **Platform** | Homey SDK 3, **Python** runtime (3.13) — *not* the Node.js SDK |
| **Dependency** | `ismartgate >= 5.0.0` |
| **License** | See `LICENSE` |

> This replaces the earlier `DEVELOPMENT_LOG.md`, which stopped at v1.0.3
> and was removed when this file was added. It remains in git history at
> commit `7d795c4` if you ever want it.

---

## 1. The original idea

Homey users with an **iSmartGate** or **GogoGate2** garage controller had
two unappealing options: the official vendor app, or nothing. The goal here
was an app that is:

1. **Local-only.** It talks directly to the hub over the LAN. No cloud
   account, no vendor relay, no internet dependency for day-to-day control.
2. **Automation-first.** The differentiator is Flow coverage. A community
   user who migrated to this app put it plainly: the official app was *"quite
   limited in terms of the available flow cards"*, making it hard to build
   things like "warn me if the door has been open 20 minutes" or "react when
   the door *starts* moving." Rich, correctly-timed Flow cards are the
   reason this app exists.
3. **Honest about hardware limits.** A garage tilt sensor knows two things:
   fully open, or fully closed. Much of this project's engineering is about
   presenting that honestly while still giving users useful transitional
   state.

## 2. Scope

**In scope**
- iSmartGate (PRO / LITE / MINI) and GogoGate2 hubs, over the local network.
- Garage doors *and* gates, including momentary "pulse" gates with no
  position sensor.
- Multiple hubs on one Homey.
- Wireless tilt-sensor extras: temperature, battery %, low-battery alarm.
- A comprehensive, correctly-timed Flow card set.

**Explicitly out of scope (so far)**
- Cloud / remote access as a *transport*. (A hub's UDI remote address can be
  used as the host, but there is no cloud-account integration.)
- Camera snapshots. Doors can report an attached camera; nothing consumes it.
- Anything requiring Homey permissions — `permissions` is deliberately `[]`.

---

## 3. Quick start for a new developer

### Prerequisites
- **Docker Desktop must be running.** Homey *Python* apps build and run in a
  container. `app run`, `app install`, and `app publish` all fail without it.
- The Homey CLI is used via `npx` — there is intentionally no global install.
- An Athom login (`npx homey login`) and a Homey on the network.

### Everyday commands
```bash
cd ~/Developer/homey-garage-gateway

npx homey app validate --level publish   # structural check at the store bar
npx homey app run                        # temporary session + live log stream
npx homey app install                    # permanent local install
npx homey app publish                    # upload a build to the App Store
npx homey whoami && npx homey list       # check login / visible Homeys
```

### Release process
1. Bump `version` in **`.homeycompose/app.json`** (never edit `app.json` —
   it is generated).
2. Add an entry to **`.homeychangelog.json`**.
3. `npx homey app validate --level publish`.
4. Commit and push.
5. `npx homey app publish` → answer **No** to "update your version number?"
   (the version is already set; answering No makes it reuse your changelog
   entry instead of prompting).
6. Open the build page, optionally share the **Test** link, then **Submit for
   certification** to go live.

> A published build is installable via its **test link immediately**, before
> certification. That is how risky changes get validated by real users
> without touching everyone. It has been used repeatedly here and is the
> single most useful habit in this project.

---

## 4. Architecture

### 4.1 Component map
```
app.py                  GarageGatewayApp — shared state only
  └─ app.door_state[(gateway_id, door_id)] -> snapshot dict

opening_device.py       OpeningDeviceBase — shared door/gate behaviour
                        (lives at the APP ROOT, see 4.3)

drivers/
  garage-gateway/       the hub: owns the API client + polling loop
  garage-door/          one device per configured garage door
  garage-gate/          one device per configured gate  (added in 1.0.3)

.homeycompose/          source of truth for app.json + custom capabilities
settings/index.html     credential entry page
```

### 4.2 The hub and the polling loop — `drivers/garage-gateway/device.py`
Each paired hub is an independent `GatewayDevice` with **its own API client
and its own poll loop**. After every successful poll it writes a per-door
snapshot into `app.door_state` and notifies every paired door *and* gate
device to refresh.

Polling cadence:

| Condition | Interval |
|---|---|
| A door is mid-travel (opening/closing) | `TRANSITION_POLL_SECONDS` = 3 s |
| Any door open | `poll_interval_open` (default 15 s) |
| All closed | `poll_interval_closed` (default 60 s) |
| ≥2 consecutive network errors | 120 s |
| Credentials rejected | 600 s + device marked unavailable |

Two details that matter:
- **`_poll_wake` (`asyncio.Event`)** — every command sets it, so the loop
  wakes immediately instead of waiting out its interval. The tile shows
  opening/closing within ~1 s of a tap.
- **The long credential backoff is deliberate.** iSmartGate firmware can
  lock the admin account after repeated failed logins; backing off hard lets
  a human fix the password before a lockout.

### 4.3 Shared behaviour — `opening_device.py`
A garage door and a *sensor-backed* gate are the same model, so that logic
lives once in `OpeningDeviceBase`. Each driver's `device.py` subclasses it
and supplies its own Flow-card id map (`door_*` vs `gate_*`).

The module sits at the **app root**. Driver files are two levels down, so
they prepend the app root to `sys.path` before importing it:

```python
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
from opening_device import OpeningDeviceBase
```

This is deliberate: it does not depend on how the on-device runtime
configures `sys.path`, which is not documented and cannot be tested locally.

### 4.4 Devices and capabilities

| Capability | Where | Notes |
|---|---|---|
| `door_status` (custom enum) | door, sensor-gate | open / closed / opening / closing / undefined — read-only |
| `garagedoor_closed` | door, sensor-gate | HomeKit/Google compatible; **also the setter used to actuate** |
| `measure_temperature` | wireless sensor | added dynamically |
| `measure_battery` | wireless sensor | derived from voltage (CR123A curve) |
| `alarm_battery` | wireless sensor | low-battery alarm |
| `button` | pulse gate | momentary single `activate()` |
| `alarm_connectivity` | hub | hub unreachable |

Wireless capabilities are added/removed **lazily** as the API reports (or
stops reporting) a sensor, so a wired door shows no empty sensor tiles.

### 4.5 Flow cards
- **Door:** triggers `opened`, `closed`, `left_open`, `opening`, `closing`,
  `status_changed`; conditions `is_open`, `is_closed`; action `toggle`.
- **Gate:** the same six triggers and two conditions with `gate_*` ids, plus
  actions `toggle_gate` and **`pulse_gate`** (for momentary gates).

### 4.6 Pairing
The hub pairs first. Credentials are typed into the **app settings page**,
validated during pairing, copied into that hub's **encrypted device store**,
and then the plaintext password is **cleared from settings**.

Door and gate drivers then list the hub's configured openings, **split by the
hub's `gate` flag**, so each opening appears under exactly one driver.
Sensorless gates pair as a `button` device.

**Multiple hubs** work by repeating that: put hub 2's host/password in the
settings form, pair again. Each hub gets a unique device id
(`gateway-{udi or host}`) and its own stored credentials. When more than one
hub is paired, the pair list prefixes each opening with its hub name
(`Home - Garage Door`) so identically-named doors stay distinguishable.

### 4.7 Security model
- Credentials live in the encrypted device store; the plaintext copy is
  cleared after pairing.
- The settings page treats the password as **write-only** — it is never read
  back into the DOM — and validates the host against a pattern that rejects
  URL schemes and paths.
- `permissions: []`. No web API endpoints exist.
- Logs deliberately avoid credentials and full request URLs (an httpx `repr`
  can contain the query string).

---

## 5. Version history

| Ver | Build | Date | Outcome | Summary |
|---|---|---|---|---|
| 1.0.0 | — | 2026-05-25 | submitted | Initial release: local control, status, temp/battery, core Flows, BenQ-derived hardening |
| 1.0.1 | — | 2026-06-07 | **Rejected** | Icon redesign + product photos — rejected on guideline 1.5 |
| 1.0.2 | 3 | 2026-06-10 | Approved | Removed icon background fills; reviewer left a non-blocking 1.4 note |
| 1.0.3 | 4 | 2026-06-19 | Approved / live | Gate support, full Flow set, four audit fixes |
| 1.0.4 | 5 | 2026-07-30 | Test only | Accurate during-travel status; GogoGate2 as a first-class device |
| 1.0.5 | — | 2026-08-01 | Test only | Trigger fix, part 1 (call signature) — **did not fully work** |
| 1.0.6 | 7 | 2026-08-03 | Test, hw-confirmed | Trigger fix, part 2 (run listeners) — confirmed on real hardware |
| 1.0.7 | **8** | published 2026-09-11 · **live 2026-09-14** | **LIVE** | Multi-hub pair-list labelling; store text now mentions gates + multi-hub |

Live users went **1.0.3 → 1.0.7 in a single certification**, because 1.0.4–1.0.6
were deliberately kept on the test channel while the trigger bug was chased.

### Commit landmarks
```
2026-05-18  4554595  Initial commit
2026-05-18  c7dce63  Initial Garage Gateway Homey app scaffold
2026-05-19  1284b5b  Fix open/close, add door_status capability, ship security audit
2026-05-25  b78ec66  Fix door_status not updating after open/close
2026-05-25  e211847  Prep v1.0.0 for Homey App Store submission
2026-05-25  5cafc72  Apply pre-submission hardening from BenQ app lessons
2026-06-07  d9b2145  v1.0.1: redesign icons + add product-photo driver images
2026-06-10  9ee9784  v1.0.2: remove icon backgrounds for App Store guideline 1.5
2026-06-10  aa7bbe4  v1.0.3: gate support, comprehensive flows, and audit fixes
2026-06-22  7d795c4  Add development log
2026-07-30  6000bc4  v1.0.4: accurate travel status + GogoGate2 as a first-class device
2026-08-01  3ba1774  v1.0.5: fix custom flow triggers that never fired
2026-08-03  94987c1  v1.0.6: register run listeners so device triggers actually fire
2026-09-11  5d535df  v1.0.7: multi-hub pair-list labelling + store text
```

> Note: the v1.0.2 and v1.0.3 commits share a date because the author
> identity on the then-unpushed commits was rewritten with a rebase, which
> reset their dates. Trust the release table above for *shipping* dates and
> this list for *commit* order.

---

## 6. App Store certification history

| Build / ver | Feedback | Resolution |
|---|---|---|
| 1.0.1 | **Guideline 1.5 (icons):** solid coloured background renders as a solid black shape, hiding the illustration | Removed the background `<rect>`; redrew as single-colour silhouettes on transparent, using `fill-rule="evenodd"` for cut-outs |
| 1.0.2 | **Guideline 1.4 (images), non-blocking:** the garage-door *driver image* had a real-world background (driveway, greenery) | Replaced with the device on a **pure white** background; hub image flattened to opaque white |
| 1.0.3–1.0.7 | *(none)* | — |

**Pattern:** the reviewer is thorough about icons (1.5) and driver images
(1.4). The rules that keep this clean:
- **Icons:** transparent background, single colour, no background fill.
- **Driver images:** the device on a **white** background, 500×500 + 75×75.
- **App promo images** (`assets/images/`) may be styled/branded — different
  rule, and they have never been flagged.
- Declare `energy.batteries` on any driver that can expose `measure_battery`,
  even when the capability is added dynamically (validation will not catch it).

---

## 7. Hard-won lessons

**These are the highest-value part of this document.** Most cost real
debugging time, and several were invisible to `validate`.

### 7.1 Flow cards (the Python runtime's sharpest edges)
1. **A device trigger card needs a registered run listener — or it silently
   does nothing.** `FlowCard._run()` raises `NotFound` when
   `_run_listener is None`, and `register_run_listener` does *not* tell Homey
   core anything. So Homey calls back to evaluate the flow, hits `NotFound`,
   and **blocks the flow with no visible error**. Register a listener
   returning `True` for every device trigger card.
2. **`card.trigger(device, tokens)` — there is no third positional argument.**
   The signature is `trigger(device, tokens={}, **trigger_kwargs)`. Passing a
   third positional (an old-style `state` dict) raises `TypeError`.
3. **Register run listeners ONCE per driver, never per device.** A card
   allows exactly one listener; per-device registration raises
   `AlreadyExists` and **crashes the second device's `on_init`**. Resolve the
   target from `args["device"]` — the SDK hydrates it into the real device
   instance (`client/sdk.py` → `driver.get_device_by_id`).
4. Consequence of 1–3: **every custom trigger in this app was silently dead
   from 1.0.0 until 1.0.6.** Nobody noticed because nobody had built a Flow
   on them yet.

### 7.2 Debugging technique that cracked it
You can **read the runtime SDK offline, straight out of the Docker image** —
no Homey required. This is how 7.1.1 and 7.1.2 were found:

```bash
IMG=ghcr.io/athombv/python-homey-app-runner:latest
docker run --rm --entrypoint /bin/sh "$IMG" -c \
  'ls /python-venvs/3.13/lib/python3.13/site-packages/homey/'
# python lives at /python-venvs/3.13/bin/python
```

The Python Homey framework is **runtime-provided and not vendored**, so it
cannot be inspected from the repo. When behaviour is unexplained, read the
source in the image before guessing.

### 7.3 Swallowed exceptions hide bugs for months
The trigger-fire helpers wrapped `card.trigger()` in `try/except` that only
logged. A hard `TypeError` therefore looked exactly like "nothing happened."
Defensive logging is good; **silent** defensive logging is a trap. If a
failure means a feature is entirely dead, make it loud.

### 7.4 Hardware truth: a tilt sensor only knows terminal states
It reports the **pre-travel** state during motion — still "closed" while
opening, still "open" while closing. Naively reporting the raw sensor
produced: a status stuck on "opening", a "closing → opened" blink, and a
10+ second lag. The fix is to use the **`ismartgate` library's transitional
tracking** (`_get_door_statuses`, 55 s timeout), which is populated by *our
own* open/close commands and resolves when the sensor reaches the target.

Corollary: **transitions are only observable for app-initiated commands.** If
someone opens the door with a physical remote, no command passes through the
library, so only terminal open/closed can be reported. That is a hardware
limitation, not a bug, and it is disclosed in the Flow card hints.

### 7.5 `garagedoor_closed` is both the command and the state
Setting it *is* how Homey actuates the door. So it is pre-set the instant a
Flow commands the door, which makes it **useless as a "before" value** for
detecting a real transition. Track the last **terminal status** separately
(`_last_terminal`) and fire opened/closed from that.

### 7.6 Library and data gotchas
- **`events` is `int | None` and GogoGate2 always returns `None`.**
  `int(None)` raised `TypeError` on **every poll**, leaving every GogoGate2
  hub permanently unavailable. Shipped in 1.0.2, fixed in 1.0.3.
- **The library says `opened`; the capability enum says `open`.** Map at the
  write boundary or Homey silently rejects the value and the tile freezes.
- Guard `int()` / `float()` on anything from the API, and reject non-finite
  floats before they reach Homey.

### 7.7 Async hygiene (inherited from the BenQ app)
Every fire-and-forget coroutine goes through `_spawn()`, which attaches a
done-callback that retrieves and logs exceptions. Without it, a stray error
surfaces as "Task exception was never retrieved" and can take the app down —
this crashed `com.fredhill.benq-projector` v1.0.3 and needed v1.0.4 to fix.
The first poll is wrapped too, so an early failure cannot kill the loop
before the handler exists.

### 7.8 Process lessons
- **Use the test channel.** Publish, share the test link with the reporter,
  confirm, *then* promote. Two of the trigger fixes were wrong or incomplete;
  no live user ever saw them.
- **Confirm on real hardware before declaring victory.** `validate` checks
  structure, not behaviour. The trigger fix was only truly proven by running
  `npx homey app run` and physically cycling the door.
- **Bundle fixes.** Certification is a round trip; 1.0.4–1.0.7 shipped as one.

---

## 8. Community feedback that shaped the app

| Who | What | Result |
|---|---|---|
| GogoGate2 user | Confirmed a **2-door GogoGate2** setup working on 1.0.3 | Independently validated the `events=None` crash fix *and* the multi-device fix on hardware the author does not own |
| **Alex** | Three precise, capability-level reports of wrong door status and dead Flow triggers | Drove 1.0.4, 1.0.5 and 1.0.6 — the single most valuable bug report this project has had |
| **Tomm Borge** | Asked whether more than one hub was supported | Revealed multi-hub was supported but **undiscoverable** → 1.0.7 pair-list labelling + store text. He had not bought hardware yet |

Traction data point: **31 installs, 0 crashes** (2026-06), growing steadily.

---

## 9. Known limitations and open items

- **Multi-hub is untested on 2+ physical hubs.** It is correct by
  construction (per-hub API client, poll loop, credentials; state keyed by
  `(gateway_id, door_id)`; `_require_hub()` resolves by the device's
  `gateway_id`) but nobody has run it for real. **Verify that each door
  commands its own hub** when someone does.
- **Transitions need app-initiated commands** (see 7.4).
- **Door ↔ gate reconfiguration**: changing an opening's type in the
  iSmartGate web UI after pairing leaves a stale device, and the same opening
  could be paired under both drivers. Low impact; a future build could mark
  the stale device unavailable when the `gate` flag flips.
- **Sensorless gates** deliberately raise on `toggle`/`is_open`/`is_closed`
  ("use the Pulse action") rather than reporting meaningless state.
- **Hub id falls back to host** when no UDI is present, so a DHCP change
  makes the hub look like a new device. Prefer UDI / static IPs.
- Generated flow cards contain a **duplicated `device` arg** (an explicit one
  plus Homey's auto-injected one). Pre-existing, harmless, present since the
  first approved build.
- **Camera snapshots** are unimplemented.
- Battery scaling assumes a **CR123A** (~3.0 V fresh, ~2.4 V cutoff).

## 10. Ideas worth considering next

- Camera snapshot capability / Flow token.
- Mark stale devices unavailable when an opening changes type.
- Per-hub credential entry in the pair wizard, instead of reusing one global
  settings form for each hub in turn.
- Localisation beyond `en`.

---

## 11. Repo layout

```
app.py                       App class; owns app.door_state
opening_device.py            OpeningDeviceBase (shared door/gate logic)
.homeycompose/app.json       SOURCE OF TRUTH for the manifest
.homeycompose/capabilities/  custom door_status capability
.homeychangelog.json         per-version changelog
app.json                     GENERATED — do not edit
drivers/garage-gateway/      hub driver + device (API, polling)
drivers/garage-door/         door driver + thin device subclass
drivers/garage-gate/         gate driver + device (sensor or pulse/button)
settings/index.html          credential entry page
assets/                      app icon + promo images
scripts/                     probe.py; source SVGs for driver images
locales/en.json              app name/description strings
GARAGE-GATEWAY-PROJECT-HISTORY.md   this document
```

**Never edit `app.json` directly** — it is regenerated from
`.homeycompose/` every time the CLI pre-processes the app.
