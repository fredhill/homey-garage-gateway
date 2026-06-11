"""
GarageDoorDevice — one configured garage door on an iSmartGate hub.

All behaviour lives in OpeningDeviceBase (see opening_device.py at the app
root); this subclass only supplies the door-specific flow-card ids. A
sensor-backed gate is the same model with gate_* card ids — see
drivers/garage-gate/device.py.

Capability mapping (from the base):
  door_status        = "open" | "closed" | "opening" | "closing" | "undefined"
  garagedoor_closed  = True iff closed (HomeKit/Google compatible; the
                       setter the user/flows toggle to actuate)
  measure_temperature / measure_battery / alarm_battery
                     = added only when a wireless tilt sensor is present
"""

import os
import sys

# device.py lives at drivers/garage-door/; the shared module sits at the
# app root two levels up. Prepend it so the import resolves regardless of
# how the on-device Homey runtime configures sys.path.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from opening_device import OpeningDeviceBase


class GarageDoorDevice(OpeningDeviceBase):

    TRIGGER_IDS = {
        "opened":         "door_opened",
        "closed":         "door_closed",
        "left_open":      "door_left_open",
        "opening":        "door_opening",
        "closing":        "door_closing",
        "status_changed": "door_status_changed",
    }
    CONDITION_IDS = {"is_open": "is_open", "is_closed": "is_closed"}
    ACTION_IDS = {"toggle": "toggle_door"}


homey_export = GarageDoorDevice
