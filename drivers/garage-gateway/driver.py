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

import re

from ismartgate import (
    CredentialsIncorrectException,
    GogoGate2Api,
    ISmartGateApi,
)
from homey import driver

_BARE_UDI_RE = re.compile(r"^[0-9a-fA-F]{8,10}$")


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
            raise Exception(_pairing_error_message(host, exc))

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
            await self.homey.settings.set("ismartgate_password", "")
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


def _classify_pairing_error(exc: Exception) -> str:
    # Walk the __cause__/__context__ chain so we can classify the underlying
    # socket/OSError even when httpx has wrapped it in its own ConnectError.
    chain = []
    step = exc
    while step is not None and step not in chain:
        chain.append(step)
        step = step.__cause__ or step.__context__

    for step in chain:
        if isinstance(step, OSError) and step.errno == -2:  # EAI_NONAME
            return "dns"

    text = " ".join(str(step) for step in chain)
    if "Name or service not known" in text or "nodename nor servname" in text:
        return "dns"
    if "All connection attempts failed" in text or "Connection refused" in text:
        return "connection"
    return "other"


def _pairing_error_message(host: str, exc: Exception) -> str:
    kind = _classify_pairing_error(exc)

    if kind == "dns":
        if _BARE_UDI_RE.match(host):
            return (
                f"Could not resolve '{host}' on the network. This looks like "
                f"just the device ID — try the full remote-access address "
                f"(e.g. '{host}.isgaccess.com') or the device's LAN IP address "
                f"instead."
            )
        return (
            f"Could not resolve '{host}' on the network. Double-check it's "
            f"spelled exactly right (e.g. '.isgaccess.com', not "
            f"'.isgacces.com'), or try the device's LAN IP address instead."
        )

    if kind == "connection":
        return (
            f"Could not connect to '{host}'. The address resolved, but the "
            f"device didn't answer — check that you have the right LAN IP, "
            f"that the device is powered on, and that it's on the same "
            f"network as Homey (not blocked by a firewall or VLAN)."
        )

    return (
        f"Could not reach the device at '{host}'. Check the host address "
        f"and that the device is on the same network."
    )


def _udi_from_remote(remoteaccess) -> str | None:
    if not remoteaccess:
        return None
    return str(remoteaccess).split(".", 1)[0]


homey_export = GarageGatewayDriver
