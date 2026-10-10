"""Saved startup, live permissions, revocation, and deterministic cleanup."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from ipguard_api import (
    AuthRevokedError,
    ClientSnapshot,
    ConnectionFailureError,
    Relay,
    Session,
    SessionStatus,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ipguard import async_setup_entry, async_unload_entry
from custom_components.ipguard.const import CONF_SESSION, DOMAIN


def add_entry(hass: HomeAssistant, session: Session) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kenny",
        unique_id="email:person@example.invalid",
        data={CONF_SESSION: session.to_dict()},
    )
    entry.add_to_hass(hass)
    return entry


async def test_startup_push_and_unload_without_sms(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    entry = add_entry(hass, session)
    with patch(
        "custom_components.ipguard.IpguardClient", return_value=client
    ) as factory:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        factory.assert_called_once_with(session=session)
        client.async_connect.assert_awaited_once_with(session)
        client.async_request_verification.assert_not_awaited()
        client.async_release.assert_not_awaited()
        states = hass.states.async_all("button")
        assert len(states) == 1
        assert states[0].state == STATE_UNKNOWN
        listener = client.subscribe.call_args.args[0]
        listener(
            ClientSnapshot(
                (replace(relay, remote_allowed=False),), True, session=session
            )
        )
        await hass.async_block_till_done()
        assert hass.states.get(states[0].entity_id).state == STATE_UNAVAILABLE
        listener(ClientSnapshot((relay,), False, session=session))
        await hass.async_block_till_done()
        assert hass.states.get(states[0].entity_id).state == STATE_UNAVAILABLE
        new_relay = replace(relay, number=2, name="Side door")
        listener(ClientSnapshot((relay, new_relay), True, session=session))
        await hass.async_block_till_done()
        assert len(hass.states.async_all("button")) == 2
        runtime = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        client.unsubscribe.assert_called_once()
        client.async_close.assert_awaited_once()
        assert all(
            state.state == STATE_UNAVAILABLE
            for state in hass.states.async_all("button")
        )
        listener(ClientSnapshot((relay,), True, session=session))
        assert runtime.coordinator.connected is False


async def test_revocation_starts_one_reauth_flow_without_sending_code(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    entry = add_entry(hass, session)
    with (
        patch("custom_components.ipguard.IpguardClient", return_value=client),
        patch("custom_components.ipguard.config_flow.IpguardClient") as flow_factory,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        listener = client.subscribe.call_args.args[0]
        snapshot = ClientSnapshot(
            (relay,),
            False,
            revoked=True,
            error=AuthRevokedError(),
            session=replace(session, status=SessionStatus.DISABLED),
        )
        listener(snapshot)
        listener(snapshot)
        await hass.async_block_till_done()
        progress = hass.config_entries.flow.async_progress()
        assert len(progress) == 1
        assert progress[0]["step_id"] == "reauth_confirm"
        flow_factory.assert_not_called()
        client.async_close.assert_awaited_once()
        client.async_request_verification.assert_not_awaited()
        assert entry.data[CONF_SESSION]["status"] == "disabled"
        assert all(
            state.state == STATE_UNAVAILABLE
            for state in hass.states.async_all("button")
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("error", "mapped"),
    [
        (AuthRevokedError(), ConfigEntryAuthFailed),
        (ConnectionFailureError(), ConfigEntryNotReady),
    ],
)
async def test_setup_failures_close_resources(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    error: Exception,
    mapped: type[Exception],
) -> None:
    entry = add_entry(hass, session)
    client.async_connect.side_effect = error
    with (
        patch("custom_components.ipguard.IpguardClient", return_value=client),
        pytest.raises(mapped),
    ):
        await async_setup_entry(hass, entry)
    client.unsubscribe.assert_called_once()
    client.async_close.assert_awaited_once()
    client.async_release.assert_not_awaited()


@pytest.mark.parametrize(
    ("payload", "mapped"),
    [(None, ConfigEntryError), ({"identifier": {}}, ConfigEntryError)],
)
async def test_invalid_saved_data_never_constructs_cloud_client(
    hass: HomeAssistant, payload: object, mapped: type[Exception]
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SESSION: payload})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ipguard.IpguardClient") as factory,
        pytest.raises(mapped),
    ):
        await async_setup_entry(hass, entry)
    factory.assert_not_called()


async def test_pending_saved_authorization_requires_reauth_without_network(
    hass: HomeAssistant, session: Session
) -> None:
    entry = add_entry(hass, replace(session, status=SessionStatus.PENDING))
    with (
        patch("custom_components.ipguard.IpguardClient") as factory,
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)
    factory.assert_not_called()


async def test_failed_platform_unload_keeps_active_client(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = add_entry(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with patch(
            "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
            return_value=False,
        ):
            assert not await async_unload_entry(hass, entry)
        client.async_close.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_new_session_credentials_are_saved_from_push(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    entry = add_entry(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        updated = replace(session, sip_password="updated-synthetic-secret")
        client.subscribe.call_args.args[0](
            ClientSnapshot((relay,), True, session=updated)
        )
        assert entry.data[CONF_SESSION] == updated.to_dict()
        client.async_request_verification.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


def test_metadata_and_translations_are_consistent() -> None:
    folder = Path(__file__).parents[2] / "custom_components" / DOMAIN
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_push"
    assert manifest["integration_type"] == "hub"
    assert "quality_scale" not in manifest
    assert json.loads((folder / "strings.json").read_text()) == json.loads(
        (folder / "translations" / "en.json").read_text()
    )
