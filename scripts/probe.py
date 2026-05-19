"""Local connectivity probe for the iSmartGate / GogoGate2 API.

Usage:
    export ISMARTGATE_HOST=10.50.0.36
    export ISMARTGATE_USERNAME=admin
    export ISMARTGATE_PASSWORD='your-password'
    python3 scripts/probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from ismartgate import ISmartGateApi, get_configured_doors


async def main() -> int:
    host = os.environ.get("ISMARTGATE_HOST")
    username = os.environ.get("ISMARTGATE_USERNAME", "admin")
    password = os.environ.get("ISMARTGATE_PASSWORD")

    if not host or not password:
        print(
            "ERROR: set ISMARTGATE_HOST and ISMARTGATE_PASSWORD env vars",
            file=sys.stderr,
        )
        return 2

    api = ISmartGateApi(host, username, password)
    info = await api.async_info()

    print(f"Hub name:    {info.ismartgatename}")
    print(f"Model:       {info.model}")
    print(f"Firmware:    {info.firmwareversion}")
    print(f"API version: {info.apiversion}")
    print(f"Remote:      {info.remoteaccess} (enabled={info.remoteaccessenabled})")

    doors = list(get_configured_doors(info))
    print(f"\nConfigured doors: {len(doors)}")
    for door in doors:
        wireless = door.temperature is not None or door.voltage is not None
        print(
            f"  door {door.door_id}: {door.name!r:20s} "
            f"status={door.status.value} sensor={door.sensorid} "
            f"wireless={wireless} camera={door.camera}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
