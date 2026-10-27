"""Stateless cloud release buttons; no lock state or delayed release requests."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    AuthenticationError,
    IpguardError,
    OutcomeUnknownError,
    Relay,
    RelayUnavailableError,
)
from .const import DOMAIN
from .runtime import (
    IpguardConfigEntry,
    IpguardCoordinator,
    IpguardRuntimeData,
    RelayKey,
    account_device_info,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IpguardConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover newly authorized relays while retaining unavailable old controls."""
    runtime = entry.runtime_data
    known: set[RelayKey] = set()

    @callback
    def discover() -> None:
        new_entities = []
        for relay in runtime.coordinator.data:
            key = (relay.board.identity, relay.number)
            if key in known or not relay.remote_allowed or relay.pending_confirmation:
                continue
            known.add(key)
            new_entities.append(IpguardReleaseButton(entry, runtime, relay))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(runtime.coordinator.async_add_listener(discover))
    discover()


class IpguardReleaseButton(CoordinatorEntity[IpguardCoordinator], ButtonEntity):
    """Make an explicit momentary release, with current cached authorization."""

    _attr_has_entity_name = True
    _attr_translation_key = "release"

    def __init__(
        self, entry: IpguardConfigEntry, runtime: IpguardRuntimeData, relay: Relay
    ) -> None:
        key = (relay.board.identity, relay.number)
        super().__init__(runtime.coordinator, context=key)
        self._runtime = runtime
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key[0]}_{key[1]}_release"
        self._attr_translation_placeholders = {
            "relay": relay.name or f"Relay {relay.number}",
            "board": relay.board.name or relay.board.identity,
        }
        self._attr_device_info = account_device_info(entry)

    @property
    def available(self) -> bool:
        relay = self.coordinator.relay(self._key)
        return (
            self.coordinator.connected
            and relay is not None
            and relay.remote_allowed
            and not relay.pending_confirmation
        )

    async def async_update(self) -> None:
        """Keep cached push data when Home Assistant requests a manual update."""

    async def async_press(self) -> None:
        """Reject parallel/offline presses; send once and never replay uncertainty."""
        if not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="unavailable"
            )
        lock = self._runtime.release_lock(self._key)
        if lock.locked():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="busy")
        try:
            async with lock:
                if not self.available:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="unavailable"
                    )
                try:
                    await self._runtime.client.async_release(*self._key)
                except OutcomeUnknownError:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="outcome_unknown"
                    ) from None
                except AuthenticationError:
                    if (
                        self.coordinator.config_entry is not None
                        and not self._runtime.reauth_started
                    ):
                        self._runtime.reauth_started = True
                        await self._runtime.async_close()
                        self.coordinator.config_entry.async_start_reauth(self.hass)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="invalid_auth"
                    ) from None
                except RelayUnavailableError:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="unavailable"
                    ) from None
                except IpguardError:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="release_failed"
                    ) from None
        finally:
            self._runtime.release_finished(self._key, lock)
