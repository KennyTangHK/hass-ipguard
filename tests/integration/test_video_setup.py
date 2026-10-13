"""Synthetic Supervisor metadata resolves endpoints without service discovery."""

import asyncio
from collections.abc import Generator
from dataclasses import FrozenInstanceError
from traceback import format_exception
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohasupervisor import SupervisorClient, SupervisorError
from aiohasupervisor.addons import AddonsClient
from aiohasupervisor.ingress import IngressClient
from aiohasupervisor.models import (
    AddonStage,
    AddonState,
    InstalledAddon,
    InstalledAddonComplete,
    Repository,
    StoreInfo,
)
from aiohasupervisor.store import StoreClient
from homeassistant.components.hassio.const import DATA_COMPONENT
from homeassistant.components.hassio.handler import HassIO
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError
from yarl import URL

from custom_components.ipguard.media import CompanionError
from custom_components.ipguard.video_setup import (
    ScryptedApp,
    async_find_scrypted_app,
    async_ingress_pairing,
    default_ha_url,
)

INGRESS = "/api/hassio_ingress/SyntheticPrivateIngressCapability"
SESSION = "SyntheticPrivateIngressSession"
BASE = "custom_components.ipguard.video_setup"


def installed(
    repository: str = "official", *, state: AddonState = AddonState.STARTED
) -> InstalledAddon:
    return InstalledAddon(
        advanced=False,
        available=True,
        build=False,
        description="Synthetic fixture",
        homeassistant=None,
        icon=False,
        logo=False,
        name="Scrypted",
        repository=repository,
        slug=f"{repository}_scrypted",
        stage=AddonStage.STABLE,
        update_available=False,
        url="https://github.com/koush/scrypted",
        version_latest="0",
        version="0",
        detached=False,
        state=state,
    )


def repository(
    slug: str = "official", source: str = "https://github.com/koush/scrypted"
) -> Repository:
    return Repository(
        slug=slug,
        name="Synthetic fixture",
        source=source,
        url=source,
        maintainer="Synthetic resident",
    )


def app_info(repository: str = "official", **changes) -> Mock:
    return Mock(
        spec=InstalledAddonComplete,
        **{
            "state": AddonState.STARTED,
            "slug": f"{repository}_scrypted",
            "repository": repository,
            "host_network": True,
            "ingress": True,
            "ingress_port": 11080,
            "ingress_entry": INGRESS,
            "ip_address": "172.30.32.1",
            **changes,
        },
    )


@pytest.fixture
def supervisor(hass: HomeAssistant) -> Generator[Mock]:
    hass.config.components.add("hassio")
    client = Mock(spec=SupervisorClient)
    client.addons = Mock(spec=AddonsClient)
    client.addons.list = AsyncMock(return_value=[installed()])
    client.addons.addon_info = AsyncMock(return_value=app_info())
    client.store = Mock(spec=StoreClient)
    client.store.info = AsyncMock(
        return_value=StoreInfo(addons=[], repositories=[repository()])
    )
    client.ingress = Mock(spec=IngressClient)
    client.ingress.create_session = AsyncMock(return_value=SESSION)
    hass.data[DATA_COMPONENT] = Mock(spec=HassIO, base_url=URL("http://supervisor:80"))
    with patch(
        "homeassistant.components.hassio.get_supervisor_client", return_value=client
    ):
        yield client


async def test_no_supervisor_does_not_fetch_platform_metadata(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    hass.config.components.remove("hassio")
    assert await async_find_scrypted_app(hass) is None
    supervisor.addons.list.assert_not_awaited()
    supervisor.store.info.assert_not_awaited()


async def test_no_installed_app_returns_none(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    supervisor.addons.list.return_value = []
    assert await async_find_scrypted_app(hass) is None
    supervisor.addons.addon_info.assert_not_awaited()
    supervisor.ingress.create_session.assert_not_awaited()


async def test_official_running_app_uses_supervisor_address_and_keeps_ingress_private(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    app = await async_find_scrypted_app(hass)
    assert app == ScryptedApp(URL("https://172.30.32.1:10443"), INGRESS)
    assert "SyntheticPrivate" not in repr(app)
    with pytest.raises(FrozenInstanceError):
        app.ingress_entry = "replacement"
    supervisor.addons.addon_info.assert_awaited_once_with("official_scrypted")
    supervisor.ingress.create_session.assert_not_awaited()


@pytest.mark.parametrize(
    "state",
    [AddonState.STOPPED, AddonState.UNKNOWN, AddonState.ERROR, AddonState.STARTUP],
)
async def test_app_must_be_started_without_starting_it(
    hass: HomeAssistant, supervisor: Mock, state: AddonState
) -> None:
    supervisor.addons.list.return_value = [installed(state=state)]
    assert await async_find_scrypted_app(hass) is None
    supervisor.addons.addon_info.assert_not_awaited()
    supervisor.addons.start_addon.assert_not_called()


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/other/scrypted",
        "https://github.com/koush/scrypted-fork",
        "https://github.com/koush/scrypted?source=other",
        "https://credential@github.com/koush/scrypted",
        "https://github.com/koush/scrypted#other",
        "https://github.com/koush/\nscrypted",
    ],
)
async def test_app_name_or_claimed_url_does_not_authorize_a_fork(
    hass: HomeAssistant, supervisor: Mock, source: str
) -> None:
    supervisor.store.info.return_value = StoreInfo(
        addons=[], repositories=[repository(source=source)]
    )
    assert await async_find_scrypted_app(hass) is None
    supervisor.addons.addon_info.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"state": AddonState.STOPPED},
        {"slug": "different_scrypted"},
        {"repository": "other"},
        {"host_network": False, "network": {"10443/tcp": 30443}},
        {"ingress": False},
        {"ingress_port": 12345},
        {"ingress_entry": "/api/hassio_ingress/private/other"},
        {"ingress_entry": "/api/hassio_ingress/private?token=other"},
        {"ingress_entry": None},
        {"ip_address": "0.0.0.0"},
        {"ip_address": "::"},
        {"ip_address": "224.0.0.1"},
        {"ip_address": "not-an-address"},
    ],
)
async def test_unsupported_or_malformed_app_metadata_is_not_guessed(
    hass: HomeAssistant, supervisor: Mock, changes: dict
) -> None:
    supervisor.addons.addon_info.return_value = app_info(**changes)
    assert await async_find_scrypted_app(hass) is None


async def test_ipv6_origin_is_built_without_hostname_guessing(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    supervisor.addons.addon_info.return_value = app_info(ip_address="2001:db8::5")
    app = await async_find_scrypted_app(hass)
    assert app is not None and app.url == URL("https://[2001:db8::5]:10443")


async def test_multiple_apps_require_a_unique_exact_selected_origin(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    supervisor.addons.list.return_value = [installed(), installed("second")]
    supervisor.store.info.return_value = StoreInfo(
        addons=[], repositories=[repository(), repository("second")]
    )
    info = {
        "official_scrypted": app_info(),
        "second_scrypted": app_info("second", ip_address="172.30.32.9"),
    }
    supervisor.addons.addon_info.side_effect = info.__getitem__
    with pytest.raises(CompanionError, match="multiple_scrypted"):
        await async_find_scrypted_app(hass)
    app = await async_find_scrypted_app(hass, URL("https://172.30.32.9:10443"))
    assert app is not None and app.url == URL("https://172.30.32.9:10443")
    assert await async_find_scrypted_app(hass, URL("http://172.30.32.9:10443")) is None
    assert await async_find_scrypted_app(hass, URL("https://172.30.32.9:443")) is None
    assert (
        await async_find_scrypted_app(
            hass, URL("https://scrypted.example.invalid:10443")
        )
        is None
    )


@pytest.mark.parametrize("stage", ["list", "store", "info"])
async def test_supervisor_failures_are_sanitized(
    hass: HomeAssistant, supervisor: Mock, stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    method = {
        "list": supervisor.addons.list,
        "store": supervisor.store.info,
        "info": supervisor.addons.addon_info,
    }[stage]
    method.side_effect = SupervisorError("SyntheticPrivateSupervisorError")
    with pytest.raises(CompanionError, match="cannot_discover") as raised:
        await async_find_scrypted_app(hass)
    assert (
        "SyntheticPrivate" not in "".join(format_exception(raised.value)) + caplog.text
    )


async def test_discovery_is_bounded_and_cancellation_propagates(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    async def blocked():
        await asyncio.Event().wait()

    supervisor.addons.list.side_effect = blocked
    with patch(f"{BASE}._DISCOVERY_SECONDS", 0.01):
        with pytest.raises(CompanionError, match="cannot_discover"):
            await async_find_scrypted_app(hass)
    supervisor.addons.list.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await async_find_scrypted_app(hass)


def test_callback_prefers_supervisor_network_and_uses_configured_url_fallback(
    hass: HomeAssistant,
) -> None:
    with (
        patch(
            f"{BASE}.get_supervisor_network_url", return_value="http://homeassistant:80"
        ) as internal,
        patch(
            f"{BASE}.get_url", return_value="https://ha.example.invalid"
        ) as configured,
    ):
        assert default_ha_url(hass, app=True) == URL("http://homeassistant:80")
        configured.assert_not_called()
        assert default_ha_url(hass, app=False) == URL("https://ha.example.invalid")
        configured.assert_called_once_with(
            hass, prefer_external=False, allow_cloud=False
        )
        internal.assert_called_once_with(hass)
    with (
        patch(f"{BASE}.get_supervisor_network_url", return_value=None),
        patch(f"{BASE}.get_url", return_value="https://ha.example.invalid"),
    ):
        assert default_ha_url(hass, app=True) == URL("https://ha.example.invalid")


def test_missing_callback_url_is_sanitized(hass: HomeAssistant) -> None:
    with patch(f"{BASE}.get_url", side_effect=NoURLAvailableError):
        with pytest.raises(CompanionError, match="ha_url_unavailable"):
            default_ha_url(hass, app=False)


async def test_ingress_pairing_uses_fresh_cookie_and_exact_supervisor_origin(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    app = ScryptedApp(URL("https://172.30.32.1:10443"), INGRESS)
    first, headers = await async_ingress_pairing(hass, app)
    assert first == URL(
        "http://supervisor/ingress/SyntheticPrivateIngressCapability/endpoint/scrypted-ipguard/pair"
    )
    assert headers == {"Cookie": f"ingress_session={SESSION}"}
    assert "Authorization" not in headers
    supervisor.ingress.create_session.assert_awaited_once_with()
    supervisor.ingress.create_session.return_value = "FreshSyntheticIngressSession"
    _, second = await async_ingress_pairing(hass, app)
    assert second != headers


@pytest.mark.parametrize(
    "session", ["", "SyntheticPrivate;other=cookie", "SyntheticPrivate\nheader", None]
)
async def test_invalid_ingress_session_is_rejected_privately(
    hass: HomeAssistant, supervisor: Mock, session: object
) -> None:
    supervisor.ingress.create_session.return_value = session
    with pytest.raises(CompanionError, match="cannot_discover"):
        await async_ingress_pairing(
            hass, ScryptedApp(URL("https://172.30.32.1:10443"), INGRESS)
        )


async def test_ingress_cancellation_propagates(
    hass: HomeAssistant, supervisor: Mock
) -> None:
    supervisor.ingress.create_session.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await async_ingress_pairing(
            hass, ScryptedApp(URL("https://172.30.32.1:10443"), INGRESS)
        )


@pytest.mark.parametrize(
    "base",
    [
        "http://credential@supervisor",
        "http://supervisor/private/path",
        "http://supervisor?credential=private",
        "http://supervisor#private",
    ],
)
async def test_ingress_never_uses_a_modified_supervisor_origin(
    hass: HomeAssistant, supervisor: Mock, base: str
) -> None:
    hass.data[DATA_COMPONENT] = Mock(spec=HassIO, base_url=URL(base))
    with pytest.raises(CompanionError, match="cannot_discover"):
        await async_ingress_pairing(
            hass, ScryptedApp(URL("https://172.30.32.1:10443"), INGRESS)
        )
    supervisor.ingress.create_session.assert_not_awaited()


async def test_ingress_supervisor_error_is_sanitized(
    hass: HomeAssistant, supervisor: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor.ingress.create_session.side_effect = SupervisorError(
        "SyntheticPrivateIngressError"
    )
    with pytest.raises(CompanionError, match="cannot_discover") as raised:
        await async_ingress_pairing(
            hass, ScryptedApp(URL("https://172.30.32.1:10443"), INGRESS)
        )
    assert (
        "SyntheticPrivate" not in "".join(format_exception(raised.value)) + caplog.text
    )
