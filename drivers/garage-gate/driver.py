"""
GarageGateDriver — list-based pairing for gates.

Mirrors GarageDoorDriver but lists only the openings the hub flags as
gates (door.gate is True). Each gate is paired in one of two shapes:

  - sensor-backed gate  -> door_status + garagedoor_closed (full open/close
                           model, same as a garage door)
  - sensorless / pulse  -> a single 'button' capability that fires one
                           activate() pulse; the hardware toggles on each
                           press and reports no position.

The device's class and runtime behaviour are chosen from the
'is_button' store flag in device.py.
"""

from homey import driver


class GarageGateDriver(driver.Driver):

    async def on_init(self):
        await super().on_init()

        # Registered once per driver (see GarageDoorDriver for why). The
        # "device" argument is hydrated into the gate instance by the SDK.
        self.homey.flow.get_action_card("toggle_gate").register_run_listener(
            self._on_toggle
        )
        self.homey.flow.get_action_card("pulse_gate").register_run_listener(
            self._on_pulse
        )
        self.homey.flow.get_condition_card("gate_is_open").register_run_listener(
            self._on_is_open
        )
        self.homey.flow.get_condition_card("gate_is_closed").register_run_listener(
            self._on_is_closed
        )

        self.log("GarageGateDriver ready")

    async def _on_toggle(self, args, *extra, **kwargs):
        device = _device_from_args(args)
        if device is not None:
            _require_sensor(device)
            await device._cmd_toggle()

    async def _on_pulse(self, args, *extra, **kwargs):
        device = _device_from_args(args)
        if device is not None:
            await device._cmd_pulse()

    async def _on_is_open(self, args, *extra, **kwargs) -> bool:
        device = _device_from_args(args)
        if device is None:
            return False
        _require_sensor(device)
        return not device._is_closed()

    async def _on_is_closed(self, args, *extra, **kwargs) -> bool:
        device = _device_from_args(args)
        if device is None:
            return False
        _require_sensor(device)
        return device._is_closed()

    async def on_pair_list_devices(self, view_data: dict) -> list:
        gateway_driver = self.homey.drivers.get_driver("garage-gateway")
        hubs = gateway_driver.get_devices() if gateway_driver else []

        if not hubs:
            raise Exception(
                "Please add your iSmartGate hub first before adding gates."
            )

        already_paired: set[tuple[str, int]] = set()
        for d in self.get_devices():
            data = d.get_data()
            gid = data.get("gateway_id")
            did = data.get("door_id")
            if gid and did is not None:
                already_paired.add((gid, int(did)))

        result = []
        for hub in hubs:
            gw_id = hub.get_data()["id"]
            doors = hub.latest_doors()
            if not doors:
                self.log(f"Hub {gw_id} has no recent poll snapshot — pair the hub first")
                continue

            for door in doors:
                # Only openings the hub flags as gates belong here.
                if not getattr(door, "gate", False):
                    continue

                door_id = int(door.door_id)
                if (gw_id, door_id) in already_paired:
                    continue

                has_sensor   = bool(getattr(door, "sensor", False))
                has_wireless = door.temperature is not None or door.voltage is not None
                is_button    = not has_sensor

                if is_button:
                    capabilities = ["button"]
                else:
                    capabilities = ["door_status", "garagedoor_closed"]
                    if has_wireless:
                        capabilities += ["measure_temperature", "measure_battery", "alarm_battery"]

                display_name = door.name or f"Gate {door_id}"

                result.append({
                    "name": display_name,
                    "data": {
                        "id":         f"{gw_id}-gate-{door_id}",
                        "gateway_id": gw_id,
                        "door_id":    door_id,
                    },
                    "capabilities": capabilities,
                    "store": {
                        "is_button":           is_button,
                        "has_wireless_sensor": has_wireless,
                        "sensor_id":           getattr(door, "sensorid", None),
                        "mode":                getattr(getattr(door, "mode", None), "value", None),
                        "camera":              bool(getattr(door, "camera", False)),
                    },
                })

        if not result:
            raise Exception(
                "No gates found on this hub, or they are already added. In the "
                "iSmartGate web interface a door can be set to behave as a gate."
            )

        return result


def _device_from_args(args):
    """Pull the hydrated device instance from a flow card's arguments."""
    try:
        return args["device"]
    except (TypeError, KeyError):
        return getattr(args, "device", None)


def _require_sensor(device) -> None:
    """Open/close state is meaningless on a sensorless (pulse) gate.

    Without this, _is_closed() would always report not-closed and a toggle
    would always send a close — flows would silently misbehave instead of
    telling the user to use the Pulse action.
    """
    if device.get_store().get("is_button"):
        raise Exception(
            "This gate has no position sensor — use the 'Pulse' action instead."
        )


homey_export = GarageGateDriver
