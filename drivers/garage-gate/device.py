"""
GarageGateDevice — one configured gate on an iSmartGate hub.

Two runtime shapes, chosen from the 'is_button' store flag set at pair
time:

  - sensor-backed gate: behaves exactly like a garage door, so it reuses
    OpeningDeviceBase with gate_* flow-card ids.
  - sensorless / pulse gate: a momentary 'button' device. Each press fires
    a single activate() pulse via the hub; the hardware toggles on each
    activation and reports no open/closed position, so no door_status or
    garagedoor_closed is presented.

Both shapes also expose the custom 'Pulse the gate' action card.
"""

import os
import sys
import time

# device.py lives at drivers/garage-gate/; the shared module sits at the
# app root two levels up. Prepend it so the import resolves regardless of
# how the on-device Homey runtime configures sys.path.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from homey import device
from opening_device import OpeningDeviceBase

# Minimum gap between consecutive pulses to one gate (see the door debounce).
PULSE_DEBOUNCE_SECONDS = 1.0


class GarageGateDevice(OpeningDeviceBase):

    TRIGGER_IDS = {
        "opened":         "gate_opened",
        "closed":         "gate_closed",
        "left_open":      "gate_left_open",
        "opening":        "gate_opening",
        "closing":        "gate_closing",
        "status_changed": "gate_status_changed",
    }
    CONDITION_IDS = {"is_open": "gate_is_open", "is_closed": "gate_is_closed"}
    ACTION_IDS = {"toggle": "toggle_gate"}

    async def on_init(self):
        self._is_button = bool(self.get_store().get("is_button"))
        if self._is_button:
            await self._init_button()
        else:
            await super().on_init()

    # ------------------------------------------------------------------
    # Sensorless / pulse gate
    # ------------------------------------------------------------------

    async def _init_button(self):
        # Skip the OpeningDeviceBase body (sensor/open-close model) and run
        # only the framework's device init.
        await device.Device.on_init(self)

        data = self.get_data()
        self._gateway_id: str = data["gateway_id"]
        self._door_id:    int = int(data["door_id"])
        self._last_command_at: float = 0.0
        # Set so the inherited on_deleted (which cancels a left-open timer)
        # is safe even though a button gate never schedules one.
        self._left_open_task = None

        try:
            await self.set_class("button")
        except Exception as exc:
            self.log(f"Could not set device class to button: {exc!r}")

        self.register_capability_listener("button", self._on_button)

        self.log(
            f"GarageGateDevice (pulse) initialising — "
            f"gateway={self._gateway_id} door={self._door_id}"
        )

    async def _on_button(self, value=True, *args, **kwargs):
        await self._cmd_pulse()

    async def refresh_from_state(self):
        # A button gate has no door_status / garagedoor_closed capabilities;
        # the base refresh would log capability errors on every poll.
        if self._is_button:
            return
        await super().refresh_from_state()

    # ------------------------------------------------------------------
    # Pulse command (invoked by the button capability and the driver's
    # pulse_gate action listener).
    # ------------------------------------------------------------------

    async def _cmd_pulse(self):
        now = time.monotonic()
        if now - getattr(self, "_last_command_at", 0.0) < PULSE_DEBOUNCE_SECONDS:
            raise Exception(
                "Command ignored — another pulse was sent less than "
                f"{PULSE_DEBOUNCE_SECONDS:g}s ago."
            )
        self._last_command_at = now
        hub = self._require_hub()
        await hub.activate_door(self._door_id)
        self.log(f"Pulse command sent (gate {self._door_id})")


homey_export = GarageGateDevice
