"""One push-driven cloud connection and cached relay inventory per entry."""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ClientSnapshot, IncomingCall, IpguardClient, Relay, Session
from .const import CONF_SESSION, DOMAIN

_LOGGER = logging.getLogger(__name__)
type RelayKey = tuple[str, int]
_EARLY_CALL_LIMIT = 16
_EARLY_CALL_MAX_AGE = 30


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
    unsubscribe_calls: Callable[[], None] | None = None
    call_listener: Callable[[IncomingCall], None] | None = field(
        default=None, repr=False
    )
    calls_ready: bool = False
    pending_calls: deque[tuple[float, IncomingCall]] = field(
        default_factory=lambda: deque(maxlen=_EARLY_CALL_LIMIT), repr=False
    )
    locks: dict[RelayKey, asyncio.Lock] = field(default_factory=dict)
    reauth_started: bool = False
    closing: bool = False
    close_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @callback
    def receive_call(self, call: IncomingCall) -> None:
        """Deliver sanitized rings or buffer brief startup/readiness gaps."""
        if self.closing or self.reauth_started:
            return
        if self.call_listener is not None:
            if self.coordinator.connected:
                self.call_listener(call)
            else:
                self.pending_calls.append((monotonic(), call))
        elif not self.calls_ready:
            self.pending_calls.append((monotonic(), call))

    @callback
    def flush_calls(self) -> None:
        """Consume recent readiness-gap calls only after availability is updated."""
        if (
            self.closing
            or self.reauth_started
            or self.call_listener is None
            or not self.coordinator.connected
        ):
            return
        pending = tuple(self.pending_calls)
        self.pending_calls.clear()
        now = monotonic()
        for received, call in pending:
            if now - received <= _EARLY_CALL_MAX_AGE:
                self.receive_call(call)

    @callback
    def subscribe_calls(
        self, listener: Callable[[IncomingCall], None]
    ) -> Callable[[], None]:
        """Attach the account entity once and consume fresh readiness notifications."""
        self.calls_ready = True
        if self.closing or self.reauth_started:
            self.pending_calls.clear()
            return lambda: None
        self.call_listener = listener
        self.flush_calls()

        @callback
        def unsubscribe() -> None:
            if self.call_listener is listener:
                self.call_listener = None
                self.pending_calls.clear()

        return unsubscribe

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
            self.coordinator.async_set_updated_data(self.coordinator.data)
            self.pending_calls.clear()
            self.call_listener = None
            if self.unsubscribe is not None:
                self.unsubscribe()
                self.unsubscribe = None
            if self.unsubscribe_calls is not None:
                self.unsubscribe_calls()
                self.unsubscribe_calls = None
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
