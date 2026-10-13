"""Set up a saved IPGUARD installation without requesting another code."""

from collections.abc import Mapping

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import async_setup_component

from .api import (
    AuthenticationError,
    ClientSnapshot,
    ConnectionFailureError,
    InvalidInputError,
    IpguardClient,
    IpguardError,
    Session,
    SessionStatus,
)
from .const import CONF_SESSION, DOMAIN, PLATFORMS
from .media import manager, register_views
from .runtime import (
    IpguardConfigEntry,
    IpguardCoordinator,
    IpguardRuntimeData,
    persist_session,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register local bridge routes once; no provider action is performed."""
    register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IpguardConfigEntry) -> bool:
    """Reconnect with saved authorization and subscribe before fetching inventory."""
    payload = entry.data.get(CONF_SESSION)
    if not isinstance(payload, Mapping):
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="invalid_session"
        )
    try:
        session = Session.from_dict(payload)
    except InvalidInputError, ValueError, TypeError:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="invalid_session"
        ) from None
    if session.status is not SessionStatus.ACTIVATED:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        )

    client = IpguardClient(session=session)
    runtime = IpguardRuntimeData(client, IpguardCoordinator(hass, entry))
    owner = manager(hass)
    runtime.media = owner.create_bridge(entry, client)

    @callback
    def unregister_media() -> None:
        if owner.bridges.get(entry.entry_id) is runtime.media:
            owner.bridges.pop(entry.entry_id, None)

    runtime.unregister_media = unregister_media

    async def reauthenticate() -> None:
        await runtime.async_close()
        entry.async_start_reauth(hass)

    @callback
    def receive(snapshot: ClientSnapshot) -> None:
        if runtime.closing:
            return
        if snapshot.revoked or (
            not snapshot.connected
            and (runtime.coordinator.connected or snapshot.error is not None)
        ):
            runtime.pending_calls.clear()
        runtime.coordinator.receive(snapshot)
        runtime.flush_calls()
        if snapshot.session is not None:
            persist_session(hass, entry, snapshot.session)
        if snapshot.revoked and not runtime.reauth_started:
            runtime.reauth_started = True
            hass.async_create_task(reauthenticate(), "IPGUARD authorization cleanup")

    runtime.unsubscribe = client.subscribe(receive)
    runtime.unsubscribe_calls = client.subscribe_calls(runtime.receive_call)
    try:
        await client.async_connect(session)
        relays = await client.async_get_relays()
    except AuthenticationError:
        await runtime.async_close()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from None
    except ConnectionFailureError, IpguardError:
        await runtime.async_close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from None
    except BaseException:
        await runtime.async_close()
        raise

    runtime.coordinator.connected = client.connected
    runtime.coordinator.async_set_updated_data(relays)
    if client.session is not None:
        persist_session(hass, entry, client.session)
    entry.runtime_data = runtime

    async def shutdown(event: Event) -> None:
        await runtime.async_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    )
    try:
        if runtime.media is not None and not await async_setup_component(
            hass, "stream", {}
        ):
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="media_unavailable"
            )
        await hass.config_entries.async_forward_entry_setups(
            entry,
            [
                platform
                for platform in PLATFORMS
                if platform != Platform.CAMERA or runtime.media is not None
            ],
        )
    except BaseException:
        await runtime.async_close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IpguardConfigEntry) -> bool:
    """Unload entities and stop every cloud resource owned by this entry."""
    if not await hass.config_entries.async_unload_platforms(
        entry,
        [
            platform
            for platform in PLATFORMS
            if platform != Platform.CAMERA or entry.runtime_data.media is not None
        ],
    ):
        return False
    await entry.runtime_data.async_close()
    return True
