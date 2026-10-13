"""Resolve optional local video endpoints from supported platform metadata."""

import asyncio
import re
from dataclasses import dataclass, field
from ipaddress import ip_address

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.network import (
    NoURLAvailableError,
    get_supervisor_network_url,
    get_url,
)
from yarl import URL

from .const import SCRYPTED_PLUGIN
from .media import CompanionError, server_url

_DISCOVERY_SECONDS = 10
_SCRYPTED_SECURE_PORT = 10443
_SCRYPTED_INGRESS_PORT = 11080
_INGRESS_ENTRY = re.compile(r"/api/hassio_ingress/([A-Za-z0-9_-]{1,512})\Z")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")


@dataclass(frozen=True, slots=True)
class ScryptedApp:
    """A verified running platform app; its ingress capability stays private."""

    url: URL
    ingress_entry: str = field(repr=False)


def _official_repository(value: str) -> bool:
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        return False
    try:
        source = URL(value)
        return (
            source.scheme == "https"
            and source.host == "github.com"
            and source.user is None
            and source.password is None
            and source.port == 443
            and not source.query_string
            and not source.fragment
            and source.path.rstrip("/").casefold()
            in {"/koush/scrypted", "/koush/scrypted.git"}
        )
    except ValueError, TypeError:
        return False


async def async_find_scrypted_app(
    hass: HomeAssistant, selected_url: URL | None = None
) -> ScryptedApp | None:
    """Find one running official host-network app, without probing services."""
    if not is_hassio(hass):
        return None
    # Supervisor libraries are optional on ordinary Core/Container systems.
    from homeassistant.components.hassio import (  # noqa: PLC0415
        get_supervisor_client,
    )

    try:
        async with asyncio.timeout(_DISCOVERY_SECONDS):
            client = get_supervisor_client(hass)
            addons, store = await asyncio.gather(
                client.addons.list(), client.store.info()
            )
            repositories = {
                repository.slug
                for repository in store.repositories
                if _official_repository(repository.source)
            }
            slugs = {
                addon.slug
                for addon in addons
                if addon.repository in repositories
                and addon.slug == f"{addon.repository}_scrypted"
                and addon.state == "started"
            }
            found: list[ScryptedApp] = []
            for slug in sorted(slugs):
                info = await client.addons.addon_info(slug)
                if (
                    info.state != "started"
                    or info.slug != slug
                    or info.repository not in repositories
                    or info.host_network is not True
                    or info.ingress is not True
                    or info.ingress_port != _SCRYPTED_INGRESS_PORT
                    or not isinstance(info.ingress_entry, str)
                    or _INGRESS_ENTRY.fullmatch(info.ingress_entry) is None
                ):
                    continue
                try:
                    address = ip_address(str(info.ip_address))
                    if address.is_unspecified or address.is_multicast:
                        continue
                    url = URL.build(
                        scheme="https",
                        host=str(address),
                        port=_SCRYPTED_SECURE_PORT,
                    )
                except ValueError, TypeError:
                    continue
                app = ScryptedApp(url, info.ingress_entry)
                if selected_url is None or selected_url.origin() == url.origin():
                    found.append(app)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise CompanionError("cannot_discover") from None
    if len(found) > 1:
        raise CompanionError("multiple_scrypted")
    return found[0] if found else None


def default_ha_url(hass: HomeAssistant, *, app: bool) -> URL:
    """Select a callback URL using HA's configured network and TLS settings."""
    try:
        url = get_supervisor_network_url(hass) if app else None
        if url is None:
            url = get_url(hass, prefer_external=False, allow_cloud=False)
        return server_url(url, https=False)
    except NoURLAvailableError, CompanionError, ValueError:
        raise CompanionError("ha_url_unavailable") from None


async def async_ingress_pairing(
    hass: HomeAssistant, app: ScryptedApp
) -> tuple[URL, dict[str, str]]:
    """Create a fresh temporary ingress session; never forward Supervisor auth."""
    from homeassistant.components.hassio import (  # noqa: PLC0415
        get_supervisor_client,
    )
    from homeassistant.components.hassio.const import DATA_COMPONENT  # noqa: PLC0415

    try:
        match = _INGRESS_ENTRY.fullmatch(app.ingress_entry)
        if match is None or not is_hassio(hass):
            raise CompanionError("cannot_discover")
        supervisor = hass.data[DATA_COMPONENT]
        base = server_url(str(supervisor.base_url), https=False)
        async with asyncio.timeout(_DISCOVERY_SECONDS):
            session = await get_supervisor_client(hass).ingress.create_session()
        if not isinstance(session, str) or _SESSION.fullmatch(session) is None:
            raise CompanionError("cannot_discover")
        target = base.with_path(f"/ingress/{match[1]}/endpoint/{SCRYPTED_PLUGIN}/pair")
        return target, {"Cookie": f"ingress_session={session}"}
    except asyncio.CancelledError:
        raise
    except Exception:
        raise CompanionError("cannot_discover") from None
