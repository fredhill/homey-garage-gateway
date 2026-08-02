"""
OpeningDeviceBase — shared behaviour for sensor-backed openings.

Both a garage door and a sensor-backed gate present the same model: a
position sensor reports open/closed, the user (or a flow) toggles
garagedoor_closed to actuate, and wireless tilt sensors add temperature
and battery. The only differences are the flow-card ids (door_* vs
gate_*) and cosmetics, so that logic lives here and each driver's
device.py subclasses with its own card-id maps.

This module sits at the app root. Driver device.py files live two levels
down (drivers/<id>/device.py); they prepend the app root to sys.path
before importing it, so resolution does not depend on how the on-device
Homey runtime configures sys.path.

State is driven by GatewayDevice: after every poll the hub writes a
snapshot into app.door_state[(gateway_id, door_id)] and calls
refresh_from_state() on each paired opening, which updates capabilities
and fires flow triggers.

Behaviour:
  - door_status / opening / closing: a single tilt sensor only reports
    terminal opened/closed and lags during travel, so the hub layer feeds
    us the ismartgate library's transitional status (opening/closing) —
    tracked from our open/close commands and resolved to opened/closed
    when the sensor reaches the target. We just render whatever status the
    snapshot carries.
  - garagedoor_closed + opened/closed triggers move ONLY on terminal
    states, keyed off the last terminal status (not off garagedoor_closed,
    which a command pre-sets), so the tile never blinks mid-travel and the
    triggers fire at the real open/close moment.
  - status_changed trigger: fires whenever door_status changes, carrying
    the new status as a token.
  - alarm_battery: set from the wireless sensor voltage so Homey's
    built-in low-battery flow card has something to read.
"""

import asyncio
import time
from datetime import datetime, timezone

from homey import device


# Minimum gap between consecutive commands to a single opening. Prevents an
# accidental open + close in quick succession from wearing out the motor
# or leaving the opening in a half-state. 1 second is below normal flow
# timing but above any plausible double-tap.
COMMAND_DEBOUNCE_SECONDS = 1.0

# Capabilities only meaningful when a wireless tilt sensor is present.
# Added dynamically when the API reports temperature/voltage data, removed
# when the sensor goes away (e.g. swapped for a wired one).
WIRELESS_SENSOR_CAPABILITIES = ("measure_temperature", "measure_battery", "alarm_battery")

# Estimated-percentage threshold below which alarm_battery is raised.
BATTERY_LOW_PCT = 15


class OpeningDeviceBase(device.Device):

    # Subclasses override these with their driver-specific flow-card ids.
    # Any entry left as None disables that card for the subclass.
    TRIGGER_IDS: dict = {
        "opened":         None,
        "closed":         None,
        "left_open":      None,
        "opening":        None,
        "closing":        None,
        "status_changed": None,
    }
    CONDITION_IDS: dict = {"is_open": None, "is_closed": None}
    ACTION_IDS: dict = {"toggle": None}

    async def on_init(self):
        await super().on_init()

        data = self.get_data()
        self._gateway_id: str = data["gateway_id"]
        self._door_id:    int = int(data["door_id"])

        # Migration for devices paired before door_status was introduced.
        # Idempotent — add_capability is a no-op if the capability is present.
        if not self.has_capability("door_status"):
            try:
                await self.add_capability("door_status")
                self.log("Migration: added door_status capability")
            except Exception as exc:
                self.log(f"Migration: could not add door_status: {exc!r}")

        self._last_command_at: float = 0.0
        self._left_open_task: asyncio.Task | None = None
        self._opened_at: datetime | None = None
        # Last UI status we reported; gates the status_changed trigger and
        # is seeded (not fired) on the first observation after pair/restart.
        self._last_ui_status: str | None = None
        # Last terminal status (opened/closed) we saw. Drives the
        # opened/closed triggers independently of garagedoor_closed, which a
        # command pre-sets and so can't be trusted as the 'before' value.
        self._last_terminal: str | None = None

        # Cache device trigger cards once (skip ids the subclass disabled).
        self._trig: dict = {}
        for key, card_id in self.TRIGGER_IDS.items():
            if card_id:
                self._trig[key] = self.homey.flow.get_device_trigger_card(card_id)

        # Capability listener. The Python Homey SDK has historically passed
        # a varying number of arguments to capability callbacks, so accept
        # any extras defensively — a strict signature would raise
        # "missing positional argument" on some firmware/SDK versions.
        #
        # NOTE: the toggle action and is_open/is_closed condition cards are
        # registered ONCE per driver (see the driver classes), not here.
        # A flow-card run listener may only be registered once — registering
        # per device crashes the second device's on_init with AlreadyExists.
        # The driver's listener resolves the target device from the card's
        # "device" argument, which the SDK hydrates into the device instance.
        self.register_capability_listener(
            "garagedoor_closed", self._on_capability_garagedoor_closed
        )

        self.log(
            f"{type(self).__name__} initialising — "
            f"gateway={self._gateway_id} door={self._door_id}"
        )

        # Render initial state from whatever the hub already polled, if anything.
        self._spawn(self.refresh_from_state())

    async def on_deleted(self):
        self._cancel_left_open_timer()
        self.log(f"{type(self).__name__} removed — door {self._door_id}")

    # ------------------------------------------------------------------
    # Background-task safety net
    # ------------------------------------------------------------------

    def _spawn(self, coro) -> asyncio.Task:
        """Fire-and-forget a coroutine with mandatory exception capture.

        Wraps asyncio.create_task with add_done_callback so that any
        exception raised by the coroutine is retrieved and logged, never
        escaping into the asyncio event loop where Python would surface it
        as 'Task exception was never retrieved' — which crashed the BenQ
        Homey app in v1.0.3 and required v1.0.4 to fix.
        """
        task = asyncio.create_task(coro)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.log(f"Background task error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # State refresh (called by GatewayDevice after each poll)
    # ------------------------------------------------------------------

    async def refresh_from_state(self):
        try:
            state_dict: dict = self.homey.app.door_state
        except AttributeError:
            return

        entry = state_dict.get((self._gateway_id, self._door_id))
        if entry is None:
            return

        status = entry.get("status", "undefined")

        # The iSmartGate library reports the open state as "opened" but the
        # door_status capability schema declares it as "open". Map at the
        # write boundary — otherwise Homey silently rejects the value and
        # the tile stays stuck on whatever it last accepted, making the
        # door look frozen even though polling is updating correctly.
        ui_status = "open" if status == "opened" else status
        try:
            await self.set_capability_value("door_status", ui_status)
        except Exception as exc:
            self.log(f"refresh: door_status error: {exc!r}")

        # status_changed (and any observed opening/closing) trigger. Seed
        # silently on the first observation so a pair/restart doesn't fire.
        self._note_status(ui_status)

        if status == "undefined":
            return

        # garagedoor_closed and the opened/closed triggers move ONLY on
        # terminal states. During opening/closing we hold them, so a
        # mid-travel sensor reading (which still shows the pre-travel state)
        # can't blink the tile or fire a flow at the wrong moment.
        if status in ("opened", "closed"):
            is_closed = (status == "closed")
            try:
                await self.set_capability_value("garagedoor_closed", is_closed)
                await self.set_available()
            except Exception as exc:
                self.log(f"refresh: garagedoor_closed error: {exc!r}")
                return

            # Fire opened/closed on the sensor transition, tracked via the
            # terminal status. Seed silently on the first terminal reading
            # after pair/restart so we don't fire on startup.
            prev_terminal = self._last_terminal
            self._last_terminal = status
            if prev_terminal is not None and status != prev_terminal:
                if is_closed:
                    self._spawn(self._fire_closed())
                    self._cancel_left_open_timer()
                    self._opened_at = None
                else:
                    self._spawn(self._fire_opened())
                    self._opened_at = datetime.now(timezone.utc)
                    self._schedule_left_open_warning()

        # Conditional sensor data — only present on wireless tilt sensors.
        await self._sync_wireless_capabilities(entry)

    def _note_status(self, ui_status: str) -> None:
        """Fire status_changed (and opening/closing if observed) on change.

        Seeds silently on the first call so pairing / app restart doesn't
        emit a spurious change.
        """
        if self._last_ui_status is None:
            self._last_ui_status = ui_status
            return
        if ui_status == self._last_ui_status:
            return
        self._last_ui_status = ui_status
        self._spawn(self._fire_status_changed(ui_status))
        if ui_status == "opening":
            self._spawn(self._fire_simple("opening"))
        elif ui_status == "closing":
            self._spawn(self._fire_simple("closing"))

    async def _sync_wireless_capabilities(self, entry: dict) -> None:
        """Add or remove temperature/battery capabilities to match the API."""
        has_temp    = entry.get("temperature") is not None
        has_voltage = entry.get("voltage") is not None
        has_wireless = has_temp or has_voltage

        # Add capabilities lazily on first sighting; remove if the sensor
        # later disappears (e.g. swapped for a wired one).
        if has_wireless:
            for cap in WIRELESS_SENSOR_CAPABILITIES:
                if not self.has_capability(cap):
                    try:
                        await self.add_capability(cap)
                        self.log(f"Added capability {cap} (wireless sensor detected)")
                    except Exception as exc:
                        self.log(f"Could not add {cap}: {exc!r}")
        else:
            for cap in WIRELESS_SENSOR_CAPABILITIES:
                if self.has_capability(cap):
                    try:
                        await self.remove_capability(cap)
                        self.log(f"Removed capability {cap} (wireless sensor gone)")
                    except Exception as exc:
                        self.log(f"Could not remove {cap}: {exc!r}")

        if has_temp and self.has_capability("measure_temperature"):
            try:
                await self.set_capability_value(
                    "measure_temperature", float(entry["temperature"])
                )
            except Exception as exc:
                self.log(f"refresh: temperature error: {exc!r}")

        battery_pct = _battery_from_voltage(entry.get("voltage"))
        if battery_pct is not None:
            if self.has_capability("measure_battery"):
                try:
                    await self.set_capability_value("measure_battery", battery_pct)
                except Exception as exc:
                    self.log(f"refresh: battery error: {exc!r}")
            if self.has_capability("alarm_battery"):
                try:
                    await self.set_capability_value(
                        "alarm_battery", battery_pct <= BATTERY_LOW_PCT
                    )
                except Exception as exc:
                    self.log(f"refresh: alarm_battery error: {exc!r}")

    # ------------------------------------------------------------------
    # Capability listener / commands
    # ------------------------------------------------------------------

    async def _on_capability_garagedoor_closed(self, value, *args, **kwargs):
        """User asked Homey to set garagedoor_closed -- send the command.

        Signature is intentionally loose to tolerate Python Homey SDK
        variations across firmware. A strict (value, opts) signature
        previously surfaced as a 'missing positional argument' error when
        the SDK invoked the callback with just (value).
        """
        if value:
            await self._cmd_close()
        else:
            await self._cmd_open()

    async def _cmd_open(self):
        self._check_debounce()
        hub = self._require_hub()
        await hub.open_door(self._door_id)
        self.log(f"Open command sent (door {self._door_id})")

    async def _cmd_close(self):
        self._check_debounce()
        hub = self._require_hub()
        await hub.close_door(self._door_id)
        self.log(f"Close command sent (door {self._door_id})")

    async def _cmd_toggle(self):
        if self._is_closed():
            await self._cmd_open()
        else:
            await self._cmd_close()

    def _check_debounce(self) -> None:
        """Raise if a command arrived too soon after the previous one.

        Surfacing the error (instead of silently dropping the command)
        gives flow logs and the device tile clear feedback that the action
        didn't run, rather than misleading the user.
        """
        now = time.monotonic()
        if now - self._last_command_at < COMMAND_DEBOUNCE_SECONDS:
            raise Exception(
                "Command ignored — another open/close was sent less than "
                f"{COMMAND_DEBOUNCE_SECONDS:g}s ago."
            )
        self._last_command_at = now

    def _is_closed(self) -> bool:
        """True iff the opening is fully closed. Treats None/unknown as not-closed."""
        return bool(self.get_capability_value("garagedoor_closed"))

    def _require_hub(self):
        gateway_driver = self.homey.drivers.get_driver("garage-gateway")
        for hub in gateway_driver.get_devices() if gateway_driver else []:
            if hub.get_data().get("id") == self._gateway_id:
                return hub
        raise Exception(f"Parent hub '{self._gateway_id}' not found for door {self._door_id}")

    # ------------------------------------------------------------------
    # Trigger firing
    # ------------------------------------------------------------------

    async def _fire_opened(self):
        await self._fire_simple("opened")

    async def _fire_closed(self):
        await self._fire_simple("closed")

    async def _fire_simple(self, key: str):
        """Fire a device trigger card that takes only the door_name token."""
        card = self._trig.get(key)
        if card is None:
            return
        try:
            await card.trigger(self, {"door_name": self.get_name()})
            self.log(f"Trigger fired: {self.TRIGGER_IDS.get(key)} ({self.get_name()})")
        except Exception as exc:
            self.log(f"Failed to fire {self.TRIGGER_IDS.get(key)}: {exc!r}")

    async def _fire_status_changed(self, status: str):
        card = self._trig.get("status_changed")
        if card is None:
            return
        try:
            await card.trigger(
                self, {"door_name": self.get_name(), "status": status}
            )
            self.log(f"Trigger fired: status_changed={status} ({self.get_name()})")
        except Exception as exc:
            self.log(f"Failed to fire status_changed: {exc!r}")

    async def _fire_left_open(self, minutes: int):
        card = self._trig.get("left_open")
        if card is None:
            return
        try:
            await card.trigger(
                self,
                {"door_name": self.get_name(), "minutes_open": minutes},
            )
            self.log(f"Trigger fired: left_open ({self.get_name()}, {minutes}m)")
        except Exception as exc:
            self.log(f"Failed to fire left_open: {exc!r}")

    # ------------------------------------------------------------------
    # Left-open timer
    # ------------------------------------------------------------------

    def _schedule_left_open_warning(self):
        self._cancel_left_open_timer()
        try:
            minutes = int(self.get_setting("left_open_warning") or 20)
        except (TypeError, ValueError):
            minutes = 20
        minutes = max(1, min(240, minutes))
        self._left_open_task = self._spawn(self._left_open_runner(minutes))

    async def _left_open_runner(self, minutes: int):
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            return
        # Confirm we're still open before firing.
        if not self._is_closed():
            await self._fire_left_open(minutes)

    def _cancel_left_open_timer(self):
        if self._left_open_task and not self._left_open_task.done():
            self._left_open_task.cancel()
        self._left_open_task = None


def _battery_from_voltage(voltage) -> int | None:
    """
    Map an iSmartGate wireless tilt-sensor voltage to a 0–100 % estimate.

    Why: the iSmartGate API reports voltage rather than %. The wireless
    tilt sensor uses a CR123A; ~3.0 V is fresh, ~2.4 V is the practical
    cutoff. Linear scaling inside that window matches how the iSmartGate
    web UI displays battery.
    """
    if voltage is None:
        return None
    try:
        v = float(voltage)
    except (TypeError, ValueError):
        return None
    # Reject non-finite values (NaN / inf) before they propagate into Homey.
    if v != v or v == float("inf") or v == float("-inf"):
        return None
    pct = (v - 2.4) / (3.0 - 2.4) * 100.0
    return max(0, min(100, int(round(pct))))
