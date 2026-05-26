"""
GatewayDevice — the iSmartGate / GogoGate2 hub.

Owns the single API instance and the polling loop. After every poll it
writes door snapshots into app.door_state (keyed by gateway_id + door_id)
and notifies each paired GarageDoorDevice to refresh.

Polling adapts to door state and error counts:
  - any door open                 -> poll_interval_open    (default 15 s)
  - all doors closed              -> poll_interval_closed  (default 60 s)
  - ≥2 consecutive network errors -> NETWORK_BACKOFF       (120 s)
  - credentials rejected          -> CREDS_BACKOFF         (10 min)
                                     and the device is marked unavailable
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


NETWORK_BACKOFF_SECONDS = 120
# Why a long backoff on credential errors: iSmartGate firmware can lock
# the admin account after repeated failed logins. Polling every 60 s with
# stale credentials (e.g. user changed the password from the web UI)
# could trigger that lockout. 10 min lets a human intervene before any
# lockout policy kicks in.
CREDENTIALS_BACKOFF_SECONDS = 600

DEFAULT_POLL_OPEN   = 15
DEFAULT_POLL_CLOSED = 60
MIN_POLL_SECONDS    = 5
MAX_POLL_SECONDS    = 600


class GatewayDevice(device.Device):

    async def on_init(self):
        await super().on_init()

        store = self.get_store()
        self._host        = str(store.get("host", "")).strip()
        self._username    = str(store.get("username", "admin")).strip()
        self._password    = str(store.get("password", ""))
        self._device_type = str(store.get("device_type", "ismartgate")).strip()

        api_cls   = GogoGate2Api if self._device_type == "gogogate2" else ISmartGateApi
        self._api = api_cls(self._host, self._username, self._password)

        self._consecutive_errors = 0
        self._credentials_rejected = False
        self._latest_doors: list = []
        self._poll_task: asyncio.Task | None = None
        self._gateway_id_cache: str = self.get_data()["id"]

        # Logged once so the iSmartGate host isn't repeated on every poll.
        self.log(
            f"GatewayDevice initialising — type={self._device_type} host={self._host}"
        )
        self._poll_task = self._spawn(self._poll_loop())

    async def on_deleted(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        # Drop our door-state entries so a stale snapshot can't leak into
        # any garage-door device that outlives the hub during teardown.
        if hasattr(self.homey, "app") and hasattr(self.homey.app, "door_state"):
            state = self.homey.app.door_state
            for key in [k for k in state.keys() if k[0] == self._gateway_id_cache]:
                state.pop(key, None)
        self.log("GatewayDevice removed — poll loop stopped")

    # ------------------------------------------------------------------
    # Public API used by garage-door devices and driver
    # ------------------------------------------------------------------

    def gateway_id(self) -> str:
        return self._gateway_id_cache

    def latest_doors(self) -> list:
        """Snapshot of configured doors from the most recent successful poll."""
        return list(self._latest_doors)

    async def open_door(self, door_id: int) -> None:
        if self._credentials_rejected:
            raise Exception(
                "Hub credentials are rejected — re-pair the hub before sending commands."
            )
        await self._api.async_open_door(int(door_id))

    async def close_door(self, door_id: int) -> None:
        if self._credentials_rejected:
            raise Exception(
                "Hub credentials are rejected — re-pair the hub before sending commands."
            )
        await self._api.async_close_door(int(door_id))

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        # Run one poll immediately so newly-paired hubs populate state fast.
        # Wrapped in the same safety net as the loop body — without this an
        # exception on the first poll (e.g. SDK quirk inside set_unavailable)
        # would kill the task before the loop's except handler ever ran.
        # Lesson learned from com.fredhill.benq-projector v1.0.5.
        try:
            await self._poll_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.log(f"Initial poll failed: {type(exc).__name__}: {exc}")

        while True:
            try:
                await asyncio.sleep(self._next_interval_seconds())
                await self._poll_once()
            except asyncio.CancelledError:
                self.log("Poll loop cancelled")
                return
            except Exception as exc:
                self.log(f"Unhandled poll-loop error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Background-task safety net
    # ------------------------------------------------------------------

    def _spawn(self, coro) -> asyncio.Task:
        """Fire-and-forget a coroutine with mandatory exception capture.

        Wraps asyncio.create_task with add_done_callback so that any
        exception raised by the coroutine is retrieved and logged,
        never escaping into the asyncio event loop where Python would
        surface it as 'Task exception was never retrieved' — which
        crashed the BenQ Homey app in v1.0.3 and required v1.0.4 to fix.
        """
        task = asyncio.create_task(coro)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.log(f"Background task error: {type(exc).__name__}: {exc}")

    async def _poll_once(self) -> None:
        try:
            info = await self._api.async_info()
        except CredentialsIncorrectException:
            # Don't log {exc!r} — the library's repr can include the request
            # URL or other diagnostic detail we don't want to repeat in logs
            # on every failed attempt.
            self._consecutive_errors += 1
            if not self._credentials_rejected:
                self._credentials_rejected = True
                self.log("Poll: credentials rejected — backing off polling")
            await self.set_unavailable(
                "Hub credentials rejected. Open the app settings, update the "
                "password, then re-pair the hub from Add a Device."
            )
            return
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._consecutive_errors += 1
            self.log(
                f"Poll: network error ({self._consecutive_errors}x): {type(exc).__name__}"
            )
            await self._on_unreachable("Could not reach the iSmartGate hub")
            return
        except Exception as exc:
            self._consecutive_errors += 1
            self.log(
                f"Poll: unexpected error ({self._consecutive_errors}x): {type(exc).__name__}: {exc}"
            )
            await self._on_unreachable(
                f"Polling failed ({type(exc).__name__})"
            )
            return

        self._consecutive_errors = 0
        # If we'd previously locked out on bad creds, a successful poll
        # clears that — the user has fixed the password or the hub is
        # accepting it again.
        if self._credentials_rejected:
            self._credentials_rejected = False
            self.log("Poll: credentials accepted — resuming normal polling")

        await self.set_available()
        await self.set_capability_value("alarm_connectivity", False)

        doors = list(get_configured_doors(info))
        self._latest_doors = doors

        # Write per-door snapshots into shared app state and notify each door device.
        gw_id = self._gateway_id_cache
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
        # Hard backoff while credentials are rejected — no point polling
        # every minute with a known-bad password.
        if self._credentials_rejected:
            return CREDENTIALS_BACKOFF_SECONDS

        if self._consecutive_errors >= 2:
            return NETWORK_BACKOFF_SECONDS

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
                if d.get_data().get("gateway_id") == self._gateway_id_cache:
                    self._spawn(d.refresh_from_state())
        except Exception as exc:
            self.log(f"Error notifying door devices: {type(exc).__name__}: {exc}")


def _door_snapshot(door) -> dict:
    """Plain-dict snapshot of an ISmartGateDoor for the shared app state."""
    status_val = getattr(door.status, "value", str(door.status))
    return {
        "door_id":     int(door.door_id),
        "name":        door.name,
        "status":      status_val,  # 'opened' | 'closed' | 'undefined' | 'opening' | 'closing'
        "sensor":      bool(door.sensor),
        "sensor_id":   getattr(door, "sensorid", None),
        "temperature": door.temperature,
        "voltage":     door.voltage,
        "camera":      bool(getattr(door, "camera", False)),
        "events":      int(getattr(door, "events", 0)),
    }


homey_export = GatewayDevice
