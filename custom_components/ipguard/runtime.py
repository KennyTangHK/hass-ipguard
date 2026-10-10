"""One push-driven cloud connection and cached relay inventory per entry."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ClientSnapshot, IpguardClient, Relay, Session
from .const import CONF_SESSION, DOMAIN

_LOGGER = logging.getLogger(__name__)
type RelayKey = tuple[str, int]


class IpguardCoordinator(DataUpdateCoordinator[tuple[Relay, ...]]):
    """Accept cloud pushes without polling the gateway or physical door."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.data = ()
        self.connected = False

    @callback
    def receive(self, snapshot: ClientSnapshot) -> None:
        """Publish connectivity as well as inventory changes to every entity."""
        self.connected = snapshot.connected and not snapshot.revoked
        self.async_set_updated_data(snapshot.relays)

    def relay(self, key: RelayKey) -> Relay | None:
        """Read current permission from the cache, never a stale entity copy."""
        return next(
            (item for item in self.data if (item.board.identity, item.number) == key),
            None,
        )


@dataclass(slots=True)
class IpguardRuntimeData:
    """Own cloud resources, subscriptions, and per-relay concurrency guards."""

    client: IpguardClient
    coordinator: IpguardCoordinator
    unsubscribe: Callable[[], None] | None = None
    locks: dict[RelayKey, asyncio.Lock] = field(default_factory=dict)
    reauth_started: bool = False
    closing: bool = False
    close_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def release_lock(self, key: RelayKey) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    def release_finished(self, key: RelayKey, lock: asyncio.Lock) -> None:
        """Keep only locks belonging to requests that are still in progress."""
        if self.locks.get(key) is lock and not lock.locked():
            self.locks.pop(key)

    async def async_close(self) -> None:
        """Remove our listener before closing sockets and reconnect tasks."""
        if self.close_task is None:
            self.closing = True
            self.coordinator.connected = False
            if self.unsubscribe is not None:
                self.unsubscribe()
                self.unsubscribe = None
            self.close_task = asyncio.create_task(self.client.async_close())
        await asyncio.shield(self.close_task)


type IpguardConfigEntry = ConfigEntry[IpguardRuntimeData]


def persist_session(hass: HomeAssistant, entry: ConfigEntry, session: Session) -> None:
    """Persist only explicit SDK authorization, without logging its contents."""
    serialized = session.to_dict()
    if entry.data.get(CONF_SESSION) != serialized:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SESSION: serialized}
        )
