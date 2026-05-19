"""
GarageGatewayDriver — pairing driver for the iSmartGate / GogoGate2 hub.

Credentials are collected on the app settings page before pairing. The
pair flow then:

  1. Reads ismartgate_host, ismartgate_username, ismartgate_password from
     app settings (host may be an IP, a hostname like 'ismartgate.local',
     or a UDI such as 'fe56b595f1.isgaccess.com').
  2. Validates the connection by calling async_info() on the chosen API.
  3. Returns a single hub device entry; credentials are stashed in the
     encrypted device store and cleared from plain-text settings.

Most users only have one hub, so the pair flow is just list_devices +
add_devices — matching the Fing app's pattern.
"""

from ismartgate import (
    CredentialsIncorrectException,
    GogoGate2Api,
    ISmartGateApi,
)
from homey import driver


class GarageGatewayDriver(driver.Driver):

    async def on_init(self):
        await super().on_init()
        self.log("GarageGatewayDriver ready")

    async def on_pair_list_devices(self, view_data: dict) -> list:
        host        = str(self.homey.settings.get("ismartgate_host")     or "").strip()
        username    = str(self.homey.settings.get("ismartgate_username") or "admin").strip()
        password    = str(self.homey.settings.get("ismartgate_password") or "")
        device_type = str(self.homey.settings.get("device_type")         or "ismartgate").strip()

        if not host or not password:
            raise Exception(
                "Please open the Garage Gateway app settings and enter your "
                "iSmartGate host (IP, ismartgate.local, or UDI address) and "
                "password before adding the hub."
            )

        api_cls = GogoGate2Api if device_type == "gogogate2" else ISmartGateApi
        self.log(f"Pairing: connecting to {device_type} at {host} as {username}")

        try:
            api  = api_cls(host, username, password)
            info = await api.async_info()
        except CredentialsIncorrectException:
            raise Exception(
                "Username or password rejected by the device. Please correct "
                "them in the Garage Gateway app settings and try again."
            )
        except Exception as exc:
            # Log exception class + message rather than repr — some httpx
            # repr() output includes the full request URL with query string,
            # which we never want repeated in app logs.
            self.log(
                f"Pairing: connection error: {type(exc).__name__}: {exc}"
            )
            raise Exception(
                f"Could not reach the device at '{host}'. Check the host "
                f"address and that the device is on the same network."
            )

        hub_name = getattr(info, "ismartgatename", None) or "iSmartGate Hub"
        model    = getattr(info, "model", "ismartgate")
        firmware = getattr(info, "firmwareversion", "")
        udi      = _udi_from_remote(getattr(info, "remoteaccess", None))

        # Use UDI for a stable hub identifier when remote access is set up;
        # fall back to host otherwise. Survives IP changes on the LAN.
        device_id = f"gateway-{udi or host.replace('.', '-').replace(':', '-')}"

        self.log(f"Pairing: verified {model} '{hub_name}' (firmware {firmware})")

        # Once pairing confirms the credentials, clear the plain-text copy
        # from app settings — they live in the encrypted device store from now on.
        try:
            self.homey.settings.set("ismartgate_password", "")
            self.log("Pairing: cleared password from plain-text app settings")
        except Exception as exc:
            self.log(
                f"Pairing: could not clear password from settings: "
                f"{type(exc).__name__}: {exc}"
            )

        return [
            {
                "name": hub_name,
                "data": {"id": device_id},
                "store": {
                    "host":        host,
                    "username":    username,
                    "password":    password,
                    "device_type": device_type,
                    "udi":         udi,
                    "model":       model,
                },
                "capabilities": ["alarm_connectivity"],
                "settings": {},
            }
        ]


def _udi_from_remote(remoteaccess) -> str | None:
    if not remoteaccess:
        return None
    return str(remoteaccess).split(".", 1)[0]


homey_export = GarageGatewayDriver
