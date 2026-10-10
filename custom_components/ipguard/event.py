"""Passive account-level doorbell events from the existing cloud connection."""

from homeassistant.components.event import (
    DoorbellEventType,
    EventDeviceClass,
    EventEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IncomingCall
from .runtime import IpguardConfigEntry, IpguardCoordinator, IpguardRuntimeData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IpguardConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose calls even when no caller or remotely releasable relay is known."""
    async_add_entities([IpguardIncomingCallEvent(entry, entry.runtime_data)])


class IpguardIncomingCallEvent(CoordinatorEntity[IpguardCoordinator], EventEntity):
    """Record an incoming call without acknowledging or controlling the call."""

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_call"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types: list[str] = [DoorbellEventType.RING]

    def __init__(self, entry: IpguardConfigEntry, runtime: IpguardRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_incoming_call"
        self._attr_translation_placeholders = {"account": entry.title}

    @property
    def available(self) -> bool:
        return self.coordinator.connected and not self._runtime.closing

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def subscribe() -> None:
            self.async_on_remove(self._runtime.subscribe_calls(self._receive_call))

        # HA enables state writes after async_added_to_hass returns. Subscribe
        # on the next loop iteration so buffered rings each produce an event.
        handle = self.hass.loop.call_soon(subscribe)
        self.async_on_remove(handle.cancel)

    @callback
    def _receive_call(self, call: IncomingCall) -> None:
        """Publish only metadata from the SDK's validated call projection."""
        attributes: dict[str, str] = {}
        if (board := call.board) is not None:
            for key, value in (
                ("board_id", board.identity),
                ("board_name", board.name),
                ("site_id", board.site_id),
                ("site_name", board.site_name),
            ):
                if value:
                    attributes[key] = value
        self._trigger_event(DoorbellEventType.RING, attributes or None)
        self.async_write_ha_state()
