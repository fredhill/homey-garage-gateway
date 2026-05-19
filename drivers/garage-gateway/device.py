"""
GatewayDevice — the iSmartGate / GogoGate2 hub.

Owns the single API instance and the polling loop. After every poll it
writes door snapshots into app.door_state (keyed by gateway_id + door_id)
and notifies each paired GarageDoorDevice to refresh.

Polling adapts to door state:
  - any door open  -> poll_interval_open    (default 15 s)
  - all closed     -> poll_interval_closed  (default 60 s)
  - >=2 errors     -> ERROR_BACKOFF_SECONDS (120 s)
"""

import asyncio

import httpx
from homey import device
from ismartgate import (
    OPEN_DOOR_STATUSES,
    CredentialsIncorrectException,
    GogoGate2Api,
    ISmartGateApi,
    get_configured_doors,
)


ERROR_BACKOFF_SECONDS = 120
DEFAULT_POLL_OPEN     = 15
DEFAULT_POLL_CLOSED   = 60
MIN_POLL_SECONDS      = 5
MAX_POLL_SECONDS      = 600


class GatewayDevice(device.Device):

    async def on_init(self):
        await super().on_init()

        store = self.get_store()
        self._host        = str(store.get("host", "")).strip()
        self._username    = str(store.get("username", "admin")).strip()
        self._password    = str(store.get("password", ""))
        self._device_type = str(store.get("device_type", "ismartgate")).strip()

        api_cls    = GogoGate2Api if self._device_type == "gogogate2" else ISmartGateApi
        self._api  = api_cls(self._host, self._username, self._password)

        self._consecutive_errors = 0
        self._latest_doors: list = []
        self._poll_task: asyncio.Task | None = None

        self.log(f"GatewayDevice initialising — {self._device_type} @ {self._host}")
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def on_deleted(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self.log("GatewayDevice removed — poll loop stopped")

    # ------------------------------------------------------------------
    # Public API used by garage-door devices and driver
    # ------------------------------------------------------------------

    def gateway_id(self) -> str:
        return self.get_data()["id"]

    def latest_doors(self) -> list:
        """Snapshot of configured doors from the most recent successful poll."""
        return list(self._latest_doors)

    async def open_door(self, door_id: int) -> None:
        await self._api.async_open_door(int(door_id))

    async def close_door(self, door_id: int) -> None:
        await self._api.async_close_door(int(door_id))

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        # Run one poll immediately so newly-paired hubs populate state fast.
        await self._poll_once()

        while True:
            try:
                await asyncio.sleep(self._next_interval_seconds())
                await self._poll_once()
            except asyncio.CancelledError:
                self.log("Poll loop cancelled")
                return
            except Exception as exc:
                self.log(f"Unhandled poll-loop error: {exc!r}")

    async def _poll_once(self) -> None:
        try:
            info = await self._api.async_info()
        except CredentialsIncorrectException as exc:
            self._consecutive_errors += 1
            self.log(f"Poll: credentials rejected: {exc!r}")
            await self.set_unavailable("Credentials rejected — re-pair the hub")
            return
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._consecutive_errors += 1
            self.log(f"Poll: network error ({self._consecutive_errors}x): {exc!r}")
            await self._on_unreachable("Could not reach the iSmartGate hub")
            return
        except Exception as exc:
            self._consecutive_errors += 1
            self.log(f"Poll: unexpected error ({self._consecutive_errors}x): {exc!r}")
            await self._on_unreachable(str(exc))
            return

        self._consecutive_errors = 0
        await self.set_available()
        await self.set_capability_value("alarm_connectivity", False)

        doors = list(get_configured_doors(info))
        self._latest_doors = doors

        # Write per-door snapshots into shared app state and notify each door device.
        gw_id = self.gateway_id()
        state = self.homey.app.door_state
        for door in doors:
            state[(gw_id, int(door.door_id))] = _door_snapshot(door)

        await self._notify_door_devices()

    async def _on_unreachable(self, message: str) -> None:
        try:
            await self.set_capability_value("alarm_connectivity", True)
        except Exception:
            pass
        await self.set_unavailable(message)

    def _next_interval_seconds(self) -> int:
        if self._consecutive_errors >= 2:
            return ERROR_BACKOFF_SECONDS

        settings = self.get_settings()
        any_open = any(d.status in OPEN_DOOR_STATUSES for d in self._latest_doors)

        try:
            if any_open:
                interval = int(settings.get("poll_interval_open", DEFAULT_POLL_OPEN))
            else:
                interval = int(settings.get("poll_interval_closed", DEFAULT_POLL_CLOSED))
        except (TypeError, ValueError):
            interval = DEFAULT_POLL_OPEN if any_open else DEFAULT_POLL_CLOSED

        return max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, interval))

    async def _notify_door_devices(self) -> None:
        """Ask every paired door device on this hub to refresh from app state."""
        try:
            door_driver = self.homey.drivers.get_driver("garage-door")
            for d in door_driver.get_devices():
                if d.get_data().get("gateway_id") == self.gateway_id():
                    asyncio.create_task(d.refresh_from_state())
        except Exception as exc:
            self.log(f"Error notifying door devices: {exc!r}")


def _door_snapshot(door) -> dict:
    """Plain-dict snapshot of an ISmartGateDoor for the shared app state."""
    return {
        "door_id":      int(door.door_id),
        "name":         door.name,
        "status":       door.status.value,  # 'opened' | 'closed' | 'undefined' | 'opening' | 'closing'
        "sensor":       bool(door.sensor),
        "sensor_id":    getattr(door, "sensorid", None),
        "temperature":  door.temperature,
        "voltage":      door.voltage,
        "camera":       bool(getattr(door, "camera", False)),
        "events":       int(getattr(door, "events", 0)),
    }


homey_export = GatewayDevice
