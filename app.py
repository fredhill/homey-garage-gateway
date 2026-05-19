"""
Garage Gateway — iSmartGate / GogoGate2 controller app for Homey.

The App class is a lightweight shared-state container. Real work is in
the driver and device classes:

  - GarageGatewayDriver / GatewayDevice — owns the iSmartGate API instance,
    runs the polling loop, and writes door state into app.door_state.
  - GarageDoorDriver / GarageDoorDevice — one per configured door,
    reads from app.door_state and presents the garagedoor_closed
    capability, fires trigger cards on state changes.
"""

from homey import app


class GarageGatewayApp(app.App):

    async def on_init(self):
        await super().on_init()

        # Shared state dict keyed by (gateway_id, door_id) -> door snapshot.
        # GatewayDevice writes after every successful poll; door devices read
        # to render their capabilities and decide when to fire trigger cards.
        # Initialised empty so door devices that start before the first poll
        # can safely read without checking attribute existence.
        self.door_state: dict[tuple[str, int], dict] = {}

        self.log("Garage Gateway started")

    async def on_settings_set(self, *args, **kwargs):
        """Stub: avoid base-class kwarg mismatch when settings are saved."""
        pass


homey_export = GarageGatewayApp
