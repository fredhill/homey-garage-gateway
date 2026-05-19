"""
GarageDoorDriver — list-based pairing for individual garage doors.

Pairing requires a Garage Gateway hub to already be paired. The driver
reads the hub's most recent poll snapshot and lists every configured
door that hasn't already been added.
"""

from homey import driver


class GarageDoorDriver(driver.Driver):

    async def on_init(self):
        await super().on_init()
        self.log("GarageDoorDriver ready")

    async def on_pair_list_devices(self, view_data: dict) -> list:
        gateway_driver = self.homey.drivers.get_driver("garage-gateway")
        hubs = gateway_driver.get_devices() if gateway_driver else []

        if not hubs:
            raise Exception(
                "Please add your iSmartGate hub first before adding doors."
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
                door_id = int(door.door_id)
                if (gw_id, door_id) in already_paired:
                    continue

                has_wireless = door.temperature is not None or door.voltage is not None
                capabilities = ["garagedoor_closed"]
                if has_wireless:
                    capabilities += ["measure_temperature", "measure_battery"]

                display_name = door.name or f"Garage Door {door_id}"

                result.append({
                    "name": display_name,
                    "data": {
                        "id":         f"{gw_id}-door-{door_id}",
                        "gateway_id": gw_id,
                        "door_id":    door_id,
                    },
                    "capabilities": capabilities,
                    "store": {
                        "has_wireless_sensor": has_wireless,
                        "sensor_id":           getattr(door, "sensorid", None),
                        "camera":              bool(getattr(door, "camera", False)),
                    },
                })

        if not result:
            raise Exception(
                "All configured doors on this hub are already added to Homey."
            )

        return result


homey_export = GarageDoorDriver
