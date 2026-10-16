"""Account controls and doorbell share a device across inventory changes and reload."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from ipguard_api import Board, ClientSnapshot, Relay, Session
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_runtime import add_entry

from custom_components.ipguard.const import CONF_SESSION, DOMAIN


async def test_one_account_device_includes_new_boards_and_doorbell(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    entry = add_entry(hass, session)
    second = replace(relay, board=Board("second-board", name="Garage"))
    client.async_get_relays.return_value = (relay, second)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        devices = dr.async_get(hass)
        (device,) = dr.async_entries_for_config_entry(devices, entry.entry_id)
        assert device.identifiers == {(DOMAIN, entry.entry_id)}
        assert device.name == entry.title
        assert device.manufacturer == "Noralsy"
        assert device.entry_type is dr.DeviceEntryType.SERVICE
        rows = er.async_entries_for_config_entry(registry, entry.entry_id)
        assert len(rows) == 3
        assert {row.device_id for row in rows} == {device.id}
        buttons = hass.states.async_all("button")
        assert {state.attributes["friendly_name"] for state in buttons} == {
            "Kenny Release Front door (Entrance)",
            "Kenny Release Front door (Garage)",
        }
        assert hass.states.async_all("event")[0].attributes["friendly_name"] == (
            "Kenny Incoming call"
        )
        existing_ids = {row.entity_id for row in rows}
        third = replace(relay, board=Board("third-board", name="Back gate"))
        client.subscribe.call_args.args[0](
            ClientSnapshot((relay, second, third), True, session=session)
        )
        await hass.async_block_till_done()
        rows = er.async_entries_for_config_entry(registry, entry.entry_id)
        assert len(rows) == 4
        assert existing_ids <= {row.entity_id for row in rows}
        assert {row.device_id for row in rows} == {device.id}
        assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 1
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert dr.async_entries_for_config_entry(devices, entry.entry_id)[0].id == (
            device.id
        )
        assert {row.entity_id for row in rows} == {
            row.entity_id
            for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        client.async_release.assert_not_awaited()
        client.async_request_verification.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_account_without_relays_still_has_a_doorbell_device(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = add_entry(hass, session)
    client.async_get_relays.return_value = ()
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        (device,) = dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        (event,) = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert event.domain == "event"
        assert event.device_id == device.id
        assert not hass.states.async_all("button")
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_accounts_sharing_a_board_keep_separate_devices(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    first = add_entry(hass, session)
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Other account",
        unique_id="email:other@example.invalid",
        data={CONF_SESSION: session.to_dict()},
    )
    other_client = deepcopy(client)
    with patch(
        "custom_components.ipguard.IpguardClient", side_effect=[client, other_client]
    ):
        assert await hass.config_entries.async_setup(first.entry_id)
        second.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()
        registry, devices = er.async_get(hass), dr.async_get(hass)
        account_ids = set()
        for entry in (first, second):
            (device,) = dr.async_entries_for_config_entry(devices, entry.entry_id)
            account_ids.add(device.id)
            assert device.identifiers == {(DOMAIN, entry.entry_id)}
            assert {
                entity.device_id
                for entity in er.async_entries_for_config_entry(
                    registry, entry.entry_id
                )
            } == {device.id}
        assert len(account_ids) == 2
        assert await hass.config_entries.async_unload(first.entry_id)
        assert other_client.async_close.await_count == 0
        assert await hass.config_entries.async_unload(second.entry_id)
