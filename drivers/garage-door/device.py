"""
GarageDoorDevice — represents one configured door on an iSmartGate hub.

State is driven by GatewayDevice: after every poll the hub writes a
snapshot into app.door_state[(gateway_id, door_id)] and calls
refresh_from_state() on each paired door. This device reads its entry
and updates capabilities.

Capability mapping:
  door_status         = "open" | "closed" | "opening" | "closing" | "undefined"
                        (read-only enum; what the user sees on the tile)
  garagedoor_closed   = True iff status == "closed"
                        (boolean; HomeKit / Google Assistant compatible,
                        and the setter the user/flows toggle to actuate)

Status == "undefined" leaves both capabilities unchanged.

Action paths:
  - User toggles the tile in Homey, or a flow runs the auto-generated
    open/close action card → capability listener sends the matching
    command via the parent hub.
  - The custom "Toggle garage door" action card flips the current state.
"""

import asyncio
import time
from datetime import datetime, timezone

from homey import device


# Minimum gap between consecutive commands to a single door. Prevents an
# accidental open + close in quick succession from wearing out the motor
# or leaving the door in a half-state. 1 second is below normal flow
# timing but above any plausible double-tap.
COMMAND_DEBOUNCE_SECONDS = 1.0

# Capabilities only meaningful when a wireless tilt sensor is present.
# Added dynamically in on_init if the API reports temperature/voltage data,
# removed when the sensor goes away (e.g. swapped for a wired one).
WIRELESS_SENSOR_CAPABILITIES = ("measure_temperature", "measure_battery")


class GarageDoorDevice(device.Device):

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

        # Cache flow trigger cards once.
        self._trig_opened    = self.homey.flow.get_device_trigger_card("door_opened")
        self._trig_closed    = self.homey.flow.get_device_trigger_card("door_closed")
        self._trig_left_open = self.homey.flow.get_device_trigger_card("door_left_open")

        # Capability listener. The Python Homey SDK has historically passed
        # a varying number of arguments to capability callbacks, so accept
        # any extras defensively — a strict signature would raise
        # "missing positional argument" on some firmware/SDK versions.
        self.register_capability_listener(
            "garagedoor_closed", self._on_capability_garagedoor_closed
        )

        # Custom action card (open/close are auto-generated from the
        # garagedoor_closed capability — no need to register them here).
        self.homey.flow.get_action_card("toggle_door").register_run_listener(
            lambda *args, **kwargs: self._cmd_toggle()
        )

        # Custom condition cards.
        self.homey.flow.get_condition_card("is_open").register_run_listener(
            lambda *args, **kwargs: not self._is_closed()
        )
        self.homey.flow.get_condition_card("is_closed").register_run_listener(
            lambda *args, **kwargs: self._is_closed()
        )

        self.log(
            f"GarageDoorDevice initialising — gateway={self._gateway_id} door={self._door_id}"
        )

        # Render initial state from whatever the hub already polled, if anything.
        asyncio.create_task(self.refresh_from_state())

    async def on_deleted(self):
        self._cancel_left_open_timer()
        self.log(f"GarageDoorDevice removed — door {self._door_id}")

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
        # the tile stays stuck on whatever it last accepted (typically
        # "closed" from the initial post-pair poll), making the door look
        # frozen even though the polling loop is updating correctly.
        ui_status = "open" if status == "opened" else status
        try:
            await self.set_capability_value("door_status", ui_status)
        except Exception as exc:
            self.log(f"refresh: door_status error: {exc!r}")

        if status == "undefined":
            return

        is_closed = (status == "closed")
        was_closed = self.get_capability_value("garagedoor_closed")

        # For transitional states (opening / closing), don't yet change
        # garagedoor_closed — we only update on terminal open/closed.
        if status in ("opened", "closed"):
            try:
                await self.set_capability_value("garagedoor_closed", is_closed)
                await self.set_available()
            except Exception as exc:
                self.log(f"refresh: garagedoor_closed error: {exc!r}")
                return

            # Trigger on transitions only — skip the very first update where
            # was_closed is None.
            if was_closed is not None and bool(was_closed) != bool(is_closed):
                if is_closed:
                    asyncio.create_task(self._fire_closed())
                    self._cancel_left_open_timer()
                    self._opened_at = None
                else:
                    asyncio.create_task(self._fire_opened())
                    self._opened_at = datetime.now(timezone.utc)
                    self._schedule_left_open_warning()

        # Conditional sensor data — only present on wireless tilt sensors.
        await self._sync_wireless_capabilities(entry)

    async def _sync_wireless_capabilities(self, entry: dict) -> None:
        """Add or remove temperature/battery capabilities to match what the API reports."""
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
        if battery_pct is not None and self.has_capability("measure_battery"):
            try:
                await self.set_capability_value("measure_battery", battery_pct)
            except Exception as exc:
                self.log(f"refresh: battery error: {exc!r}")

    # ------------------------------------------------------------------
    # Capability listener / commands
    # ------------------------------------------------------------------

    async def _on_capability_garagedoor_closed(self, value, *args, **kwargs):
        """User asked Homey to set garagedoor_closed -- send the matching command.

        Signature is intentionally loose to tolerate Python Homey SDK
        variations across firmware. A strict (value, opts) signature
        previously surfaced as a 'missing positional argument' error
        when the SDK invoked the callback with just (value).
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
        gives flow logs and the device tile clear feedback that the
        action didn't run, rather than misleading the user.
        """
        now = time.monotonic()
        if now - self._last_command_at < COMMAND_DEBOUNCE_SECONDS:
            raise Exception(
                "Command ignored — another open/close was sent less than "
                f"{COMMAND_DEBOUNCE_SECONDS:g}s ago."
            )
        self._last_command_at = now

    def _is_closed(self) -> bool:
        """True iff the door is fully closed. Treats None / unknown as not-closed."""
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
        try:
            await self._trig_opened.trigger(self, {"door_name": self.get_name()}, {})
            self.log(f"Trigger fired: door_opened ({self.get_name()})")
        except Exception as exc:
            self.log(f"Failed to fire door_opened: {exc!r}")

    async def _fire_closed(self):
        try:
            await self._trig_closed.trigger(self, {"door_name": self.get_name()}, {})
            self.log(f"Trigger fired: door_closed ({self.get_name()})")
        except Exception as exc:
            self.log(f"Failed to fire door_closed: {exc!r}")

    async def _fire_left_open(self, minutes: int):
        try:
            await self._trig_left_open.trigger(
                self,
                {"door_name": self.get_name(), "minutes_open": minutes},
                {},
            )
            self.log(f"Trigger fired: door_left_open ({self.get_name()}, {minutes}m)")
        except Exception as exc:
            self.log(f"Failed to fire door_left_open: {exc!r}")

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
        self._left_open_task = asyncio.create_task(self._left_open_runner(minutes))

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


homey_export = GarageDoorDevice
