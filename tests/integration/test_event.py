"""Incoming calls become passive, private Home Assistant doorbell events."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest
from homeassistant.components.event import DoorbellEventType, EventDeviceClass
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from ipguard_api import (
    AuthRevokedError,
    Board,
    ClientSnapshot,
    ConnectionFailureError,
    IncomingCall,
    Relay,
    Session,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from test_runtime import add_entry

from custom_components.ipguard import async_setup_entry
from custom_components.ipguard.const import DOMAIN
from custom_components.ipguard.runtime import IpguardCoordinator, IpguardRuntimeData


async def setup_event(
    hass: HomeAssistant, client: Mock, session: Session
) -> tuple[MockConfigEntry, str]:
    entry = add_entry(hass, session)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = hass.states.async_all("event")
    assert len(states) == 1
    return entry, states[0].entity_id


async def setup_ring_automation(
    hass: HomeAssistant, entity_id: str, action: Mock
) -> None:
    @callback
    def ring_service(call: ServiceCall) -> None:
        action(call.data)

    hass.services.async_register("test", "ring", ring_service)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "Incoming call",
                "triggers": [
                    {
                        "trigger": "event.received",
                        "target": {"entity_id": entity_id},
                        "options": {"event_type": ["ring"]},
                    }
                ],
                "actions": [{"action": "test.ring"}],
            }
        },
    )
    await hass.async_block_till_done()


async def test_doorbell_ring_runs_standard_automation_without_call_control(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        state = hass.states.get(entity_id)
        assert state.state == STATE_UNKNOWN
        assert state.attributes["device_class"] == EventDeviceClass.DOORBELL
        assert state.attributes["event_types"] == [DoorbellEventType.RING]
        registry_entry = er.async_get(hass).async_get(entity_id)
        assert registry_entry.unique_id == f"{entry.entry_id}_incoming_call"
        assert registry_entry.device_id is None
        action = Mock()
        await setup_ring_automation(hass, entity_id, action)
        board = replace(relay.board, site_id="known-site", site_name="Known site")
        client.subscribe_calls.call_args.args[0](IncomingCall(board))
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        assert state.attributes["event_type"] == DoorbellEventType.RING
        assert state.attributes["board_id"] == board.identity
        assert state.attributes["board_name"] == board.name
        assert state.attributes["site_id"] == "known-site"
        assert state.attributes["site_name"] == "Known site"
        action.assert_called_once()
        client.async_release.assert_not_awaited()
        client.async_request_verification.assert_not_awaited()
        assert {call[0] for call in client.method_calls} == {
            "subscribe",
            "subscribe_calls",
            "async_connect",
            "async_get_relays",
        }
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("inventory", ["empty", "not_remote"])
async def test_account_event_exists_without_release_buttons(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    relay: Relay,
    inventory: str,
) -> None:
    client.async_get_relays.return_value = (
        () if inventory == "empty" else (replace(relay, remote_allowed=False),)
    )
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        assert not hass.states.async_all("button")
        client.subscribe_calls.call_args.args[0](IncomingCall())
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.attributes["event_type"] == DoorbellEventType.RING
        assert "board_id" not in state.attributes
        client.async_release.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_caller_clears_known_metadata_and_keeps_secrets_private(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    relay: Relay,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        receive = client.subscribe_calls.call_args.args[0]
        receive(IncomingCall(relay.board))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["board_id"] == relay.board.identity
        receive(IncomingCall())
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert set(state.attributes) == {
            "event_type",
            "event_types",
            "device_class",
            "friendly_name",
        }
        exposed = json.dumps(state.as_dict()) + caplog.text
        for value in (
            session.identity,
            session.identifier.value,
            session.verification_code,
            session.sip_password,
            session.server_public_key,
        ):
            assert value not in exposed
        client.async_release.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_disconnect_changes_availability_without_replaying_ring(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        action = Mock()
        await setup_ring_automation(hass, entity_id, action)
        receive = client.subscribe_calls.call_args.args[0]
        receive(IncomingCall(relay.board))
        await hass.async_block_till_done()
        timestamp = hass.states.get(entity_id).state
        action.assert_called_once()
        push = client.subscribe.call_args.args[0]
        push(ClientSnapshot((relay,), False, session=session))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        push(ClientSnapshot((relay,), True, session=session))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == timestamp
        action.assert_called_once()
        receive(IncomingCall())
        await hass.async_block_till_done()
        assert action.call_count == 2
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconnect_offer_waits_for_readiness_and_failed_attempt_is_discarded(
    hass: HomeAssistant, client: Mock, session: Session, relay: Relay
) -> None:
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        action = Mock()
        await setup_ring_automation(hass, entity_id, action)
        receive = client.subscribe_calls.call_args.args[0]
        push = client.subscribe.call_args.args[0]
        push(ClientSnapshot((relay,), False, error=ConnectionFailureError()))
        receive(IncomingCall(relay.board))
        push(ClientSnapshot((relay,), False))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        action.assert_not_called()
        push(ClientSnapshot((relay,), True))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["event_type"] == "ring"
        assert hass.states.get(entity_id).attributes["board_id"] == relay.board.identity
        assert not entry.runtime_data.pending_calls
        action.assert_called_once()
        timestamp = hass.states.get(entity_id).state
        push(ClientSnapshot((relay,), False, error=ConnectionFailureError()))
        receive(IncomingCall())
        push(ClientSnapshot((relay,), False, error=ConnectionFailureError()))
        assert not entry.runtime_data.pending_calls
        push(ClientSnapshot((relay,), True))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == timestamp
        action.assert_called_once()
        client.async_release.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_early_calls_survive_initial_snapshot_and_queue_is_bounded(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    rings: list[State] = []

    @callback
    def record(event: Event) -> None:
        if (
            event.data["entity_id"].startswith("event.")
            and (state := event.data["new_state"]) is not None
            and state.attributes.get("event_type") == "ring"
        ):
            rings.append(state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, record)

    async def connect(connected_session: Session) -> None:
        receive = client.subscribe_calls.call_args.args[0]
        for index in range(20):
            receive(IncomingCall(Board(f"known-{index}")))
        client.subscribe.call_args.args[0](ClientSnapshot((), False, session=session))

    client.async_connect.side_effect = connect
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        assert len(rings) == 16
        assert rings[0].attributes["board_id"] == "known-4"
        assert hass.states.get(entity_id).attributes["board_id"] == "known-19"
        assert not entry.runtime_data.pending_calls
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_startup_events_are_discarded(
    hass: HomeAssistant, client: Mock
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = IpguardRuntimeData(client, IpguardCoordinator(hass, entry))
    runtime.coordinator.connected = True
    with patch("custom_components.ipguard.runtime.monotonic", return_value=1):
        runtime.receive_call(IncomingCall())
    listener = Mock()
    with patch("custom_components.ipguard.runtime.monotonic", return_value=32):
        unsubscribe = runtime.subscribe_calls(listener)
    listener.assert_not_called()
    assert not runtime.pending_calls
    unsubscribe()
    runtime.receive_call(IncomingCall())
    assert not runtime.pending_calls
    await runtime.async_close()


async def test_disconnect_before_entity_setup_discards_queued_rings(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    client.connected = False

    async def connect(connected_session: Session) -> None:
        push = client.subscribe.call_args.args[0]
        push(ClientSnapshot((), True, session=session))
        client.subscribe_calls.call_args.args[0](IncomingCall())
        push(ClientSnapshot((), False, session=session))

    client.async_connect.side_effect = connect
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        assert not entry.runtime_data.pending_calls
        client.subscribe.call_args.args[0](ClientSnapshot((), True, session=session))
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.state == STATE_UNKNOWN
        assert state.attributes["event_type"] is None
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("early_call", [False, True])
async def test_restore_and_reload_keep_identity_without_replaying_call(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    relay: Relay,
    early_call: bool,
) -> None:
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        receive = client.subscribe_calls.call_args.args[0]
        receive(IncomingCall(relay.board))
        await hass.async_block_till_done()
        previous = hass.states.get(entity_id)
        action = Mock()
        await setup_ring_automation(hass, entity_id, action)
        runtime = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        receive(IncomingCall())
        assert not runtime.pending_calls
        assert runtime.call_listener is None
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    previous,
                    {
                        "last_event_type": "ring",
                        "last_event_attributes": {"board_id": relay.board.identity},
                    },
                )
            ],
        )
        if early_call:

            async def connect(connected_session: Session) -> None:
                client.subscribe_calls.call_args.args[0](IncomingCall())

            client.async_connect.side_effect = connect
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.attributes["event_type"] == "ring"
        if early_call:
            assert state.state != previous.state
            assert "board_id" not in state.attributes
            action.assert_called_once()
        else:
            assert state.state == previous.state
            action.assert_not_called()
        assert len(hass.states.async_all("event")) == 1
        assert entry.runtime_data is not runtime
        assert not entry.runtime_data.pending_calls
        client.subscribe_calls.call_args.args[0](IncomingCall())
        await hass.async_block_till_done()
        assert action.call_count == (2 if early_call else 1)
        assert hass.states.get(entity_id).state != previous.state
        client.async_request_verification.assert_not_awaited()
        client.async_release.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("reason", ["shutdown", "revocation"])
async def test_cleanup_removes_call_subscription_and_ignores_late_calls(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    relay: Relay,
    reason: str,
) -> None:
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        entry, entity_id = await setup_event(hass, client, session)
        receive = client.subscribe_calls.call_args.args[0]
        action = Mock()
        await setup_ring_automation(hass, entity_id, action)
        if reason == "shutdown":
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        else:
            client.subscribe.call_args.args[0](
                ClientSnapshot((relay,), False, revoked=True, error=AuthRevokedError())
            )
        receive(IncomingCall())
        await hass.async_block_till_done()
        receive(IncomingCall())
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        action.assert_not_called()
        runtime = entry.runtime_data
        assert runtime.closing
        assert runtime.call_listener is None
        assert not runtime.pending_calls
        client.unsubscribe.assert_called_once()
        client.unsubscribe_calls.assert_called_once()
        client.async_close.assert_awaited_once()
        client.async_release.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("phase", ["connect", "platform"])
async def test_cancelled_setup_clears_early_calls_and_both_subscriptions(
    hass: HomeAssistant, client: Mock, session: Session, phase: str
) -> None:
    entry = add_entry(hass, session)
    started = asyncio.Event()

    async def wait(*args: object) -> None:
        client.subscribe_calls.call_args.args[0](IncomingCall())
        started.set()
        await asyncio.Event().wait()

    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        if phase == "connect":
            client.async_connect.side_effect = wait
            task = asyncio.create_task(async_setup_entry(hass, entry))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with patch(
                "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
                side_effect=wait,
            ):
                task = asyncio.create_task(async_setup_entry(hass, entry))
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    runtime = client.subscribe_calls.call_args.args[0].__self__
    assert runtime.closing
    assert not runtime.pending_calls
    client.unsubscribe.assert_called_once()
    client.unsubscribe_calls.assert_called_once()
    client.async_close.assert_awaited_once()
    client.async_release.assert_not_awaited()
