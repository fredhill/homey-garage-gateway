# Garage Gateway

A native [Homey](https://homey.app) **Python app** for **iSmartGate** and
**GogoGate2** garage door controllers. Local-network control, real-time
state, flow triggers — no cloud dependency.

> Replaces the abandoned `com.gogogate.ismartgate` community app, which
> has no device card, no trigger cards, and requires cloud connectivity.

## Supported devices

| Device           | Doors    | Status          |
| ---------------- | -------- | --------------- |
| iSmartGate PRO   | up to 3  | ✅ Tested       |
| iSmartGate LITE  | 1        | ✅ Same API     |
| iSmartGate MINI  | 1        | ✅ Same API     |
| GogoGate2        | up to 3  | ✅ Same library |

## Features

- 🔌 **Local-only** — works without internet
- 🚪 **Open / close / toggle** from any Homey flow
- 📡 **Real-time open / closed status** for each configured door
- ⏱️ **Door-left-open trigger** with a configurable timeout
- 🌡️ **Temperature monitoring** *(wireless tilt sensors only)*
- 🔋 **Battery monitoring** *(wireless tilt sensors only)*
- 🔗 **UDI-friendly pairing** — works on-network or via remote address

## Flow cards

**Triggers**
- The garage door opened
- The garage door closed
- The garage door was left open

**Conditions**
- The garage door is / is not open
- The garage door is / is not closed

**Actions**
- Toggle the garage door *(open + close are auto-generated from the `garagedoor_closed` capability)*

## Setup

1. **Install the app** on your Homey
2. Open the app's **settings page** and enter:
   - Device type (iSmartGate or GogoGate2)
   - Host — local IP, `ismartgate.local`, or your UDI (`<udi>.isgaccess.com`)
   - Username (default `admin`)
   - Password
3. From **Devices → Add a Device**, pick **Garage Gateway → iSmartGate Hub**
4. Once the hub appears, add each door via **Garage Gateway → Garage Door**

## Architecture

```
┌─────────────────────────────┐
│   iSmartGate Hub device     │  Owns the API + the polling loop
│   (one per controller)      │
└──────────────┬──────────────┘
               │ writes door snapshots to
               │ app.door_state, then notifies
               ▼
┌─────────────────────────────┐
│   Garage Door device        │  One per configured door (up to 3)
│   garagedoor_closed         │  Conditional: temperature, battery
└─────────────────────────────┘
```

Polling adapts to state — **15 s** while any door is open, **60 s**
when all are closed, **120 s** after two consecutive errors.

## Local development

```bash
# Connectivity smoke test — verifies the ismartgate library reaches the hub
python3 -m venv .venv
.venv/bin/pip install ismartgate
ISMARTGATE_HOST=10.50.0.36 \
ISMARTGATE_USERNAME=admin \
ISMARTGATE_PASSWORD='your-password' \
  .venv/bin/python scripts/probe.py
```

Build / install via the Homey CLI:

```bash
# First time on a new machine: fetches and caches the Homey CLI
npx homey app validate            # static checks
npx homey app dependencies install  # builds pre-compiled Python deps
npx homey app install             # installs onto your Homey
npx homey app run                 # runs with hot reload (dev mode)
```

## Assets to add before publishing

PNGs referenced from `.homeycompose/` still need to be supplied:

- `assets/images/{small,large,xlarge}.png` — app icons
- `drivers/garage-gateway/assets/images/{small,large}.png`
- `drivers/garage-door/assets/images/{small,large}.png`

## License

MIT — see [LICENSE](LICENSE).
