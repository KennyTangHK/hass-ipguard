"""Saved call preferences reload safely without changing account entities."""

from copy import deepcopy
from unittest.mock import Mock, call, patch

import pytest
from homeassistant.components.event import DoorbellEventType
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from ipguard_api import IncomingCall, Relay, Session
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_event import setup_ring_automation
from test_runtime import add_entry

from custom_components.ipguard.const import CONF_AUTO_HANGUP, CONF_SESSION, DOMAIN


@pytest.mark.parametrize("saved", [None, False, True])
async def test_options_form_uses_saved_preference_without_cloud_actions(
    hass: HomeAssistant, session: Session, saved: bool | None
) -> None:
    options = {} if saved is None else {CONF_AUTO_HANGUP: saved}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SESSION: session.to_dict()},
        options=options,
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ipguard.IpguardClient") as runtime_factory,
        patch("custom_components.ipguard.config_flow.IpguardClient") as flow_factory,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result["data_schema"]({}) == {
            CONF_AUTO_HANGUP: True if saved is None else saved
        }
        hass.config_entries.options.async_abort(result["flow_id"])
        await hass.async_block_till_done()
    assert entry.options == options
    assert entry.data == {CONF_SESSION: session.to_dict()}
    runtime_factory.assert_not_called()
    flow_factory.assert_not_called()


async def test_auto_hangup_toggle_reloads_saved_session_and_keeps_ring_events(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    entry = add_entry(hass, session)
    clients = [client, deepcopy(client), deepcopy(client)]
    with patch(
        "custom_components.ipguard.IpguardClient", side_effect=clients
    ) as factory:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        initial_runtime = entry.runtime_data
        registry = er.async_get(hass)
        devices = dr.async_get(hass)
        before_rows = {
            row.entity_id: (row.unique_id, row.device_id)
            for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert len(before_rows) == 2
        (device,) = dr.async_entries_for_config_entry(devices, entry.entry_id)
        (event_state,) = hass.states.async_all("event")
        entity_id = event_state.entity_id
        action = Mock()
        await setup_ring_automation(hass, entity_id, action)

        for index, enabled in enumerate((False, True), start=1):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            assert result["data_schema"]({}) == {CONF_AUTO_HANGUP: not enabled}
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_AUTO_HANGUP: enabled}
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            await hass.async_block_till_done()

            assert factory.call_count == index + 1
            assert entry.options == {CONF_AUTO_HANGUP: enabled}
            assert entry.data == {CONF_SESSION: session.to_dict()}
            assert entry.runtime_data is not initial_runtime
            assert initial_runtime.closing
            clients[index - 1].async_close.assert_awaited_once()
            assert {
                row.entity_id: (row.unique_id, row.device_id)
                for row in er.async_entries_for_config_entry(registry, entry.entry_id)
            } == before_rows
            assert {
                account.id
                for account in dr.async_entries_for_config_entry(
                    devices, entry.entry_id
                )
            } == {device.id}
            assert {state.entity_id for state in hass.states.async_all("button")} | {
                state.entity_id for state in hass.states.async_all("event")
            } == set(before_rows)
            action.assert_has_calls([call({})] * (index - 1))
            assert action.call_count == index - 1

            clients[index].subscribe_calls.call_args.args[0](IncomingCall(relay.board))
            await hass.async_block_till_done()
            state = hass.states.get(entity_id)
            assert state.attributes["event_type"] == DoorbellEventType.RING
            assert state.attributes["board_id"] == relay.board.identity
            assert action.call_count == index
            initial_runtime = entry.runtime_data

        assert factory.call_args_list == [
            call(session=session, auto_hangup=True),
            call(session=session, auto_hangup=False),
            call(session=session, auto_hangup=True),
        ]
        for active_client in clients:
            active_client.async_connect.assert_awaited_once_with(session)
            active_client.async_request_verification.assert_not_awaited()
            active_client.async_verify_code.assert_not_awaited()
            active_client.async_complete_registration.assert_not_awaited()
            active_client.async_release.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        clients[-1].async_close.assert_awaited_once()
