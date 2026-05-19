"""
GarageDoorDevice — represents one configured door on an iSmartGate hub.

State is driven by GatewayDevice: after every poll the hub writes a
snapshot into app.door_state[(gateway_id, door_id)] and calls
refresh_from_state() on each paired door. This device reads its entry
and updates capabilities.

Capability mapping:
  garagedoor_closed = True   ⇔  door reports status == 'closed'
  garagedoor_closed = False  ⇔  any other state (open/opening/closing)
  status == 'undefined'       ⇒  leave capability unchanged

Action paths:
  - User toggles the tile in Homey, or a flow runs the auto-generated
    "Set garage door to open/closed" action card → capability listener
    sends the command via the parent hub.
  - The custom "Toggle garage door" action card flips the current state.
"""

import asyncio
from datetime import datetime, timezone

from homey import device


# Minimum gap between consecutive commands to a door — prevents thrash if a
# flow accidentally fires open + close in quick succession.
COMMAND_DEBOUNCE_SECONDS = 1.0


class GarageDoorDevice(device.Device):

    async def on_init(self):
        await super().on_init()

        data = self.get_data()
        self._gateway_id: str = data["gateway_id"]
        self._door_id:    int = int(data["door_id"])

        self._has_wireless: bool = bool(self.get_store().get("has_wireless_sensor", False))

        self._last_command_at: float = 0.0
        self._left_open_task: asyncio.Task | None = None
        self._opened_at: datetime | None = None

        # Cache flow trigger cards once.
        self._trig_opened    = self.homey.flow.get_device_trigger_card("door_opened")
        self._trig_closed    = self.homey.flow.get_device_trigger_card("door_closed")
        self._trig_left_open = self.homey.flow.get_device_trigger_card("door_left_open")

        # Capability listener — fires when Homey sets garagedoor_closed
        # (from the device tile, an auto-generated flow card, or otherwise).
        self.register_capability_listener("garagedoor_closed", self._on_capability_garagedoor_closed)

        # Custom action card (open/close are covered by the capability listener).
        self.homey.flow.get_action_card("toggle_door").register_run_listener(
            lambda args, state: self._cmd_toggle()
        )

        # Custom condition cards.
        self.homey.flow.get_condition_card("is_open").register_run_listener(
            lambda args, state: not bool(self.get_capability_value("garagedoor_closed"))
        )
        self.homey.flow.get_condition_card("is_closed").register_run_listener(
            lambda args, state: bool(self.get_capability_value("garagedoor_closed"))
        )

        self.log(f"GarageDoorDevice initialising — gateway={self._gateway_id} door={self._door_id}")

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
        if status == "undefined":
            return

        is_closed = (status == "closed")
        was_closed = self.get_capability_value("garagedoor_closed")

        try:
            await self.set_capability_value("garagedoor_closed", is_closed)
            await self.set_available()
        except Exception as exc:
            self.log(f"refresh: set_capability error: {exc!r}")
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
        if entry.get("temperature") is not None and self.has_capability("measure_temperature"):
            try:
                await self.set_capability_value("measure_temperature", float(entry["temperature"]))
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

    async def _on_capability_garagedoor_closed(self, value, opts):
        """User asked Homey to set garagedoor_closed -- send the matching command."""
        if value:
            await self._cmd_close()
        else:
            await self._cmd_open()

    async def _cmd_open(self):
        if not self._debounce_ok():
            return
        hub = self._require_hub()
        await hub.open_door(self._door_id)
        self.log(f"Open command sent (door {self._door_id})")

    async def _cmd_close(self):
        if not self._debounce_ok():
            return
        hub = self._require_hub()
        await hub.close_door(self._door_id)
        self.log(f"Close command sent (door {self._door_id})")

    async def _cmd_toggle(self):
        if bool(self.get_capability_value("garagedoor_closed")):
            await self._cmd_open()
        else:
            await self._cmd_close()

    def _debounce_ok(self) -> bool:
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - self._last_command_at < COMMAND_DEBOUNCE_SECONDS:
            self.log("Debounced: command suppressed (too soon after previous)")
            return False
        self._last_command_at = now
        return True

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
        if not bool(self.get_capability_value("garagedoor_closed")):
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
    pct = (v - 2.4) / (3.0 - 2.4) * 100.0
    return max(0, min(100, int(round(pct))))


homey_export = GarageDoorDevice
