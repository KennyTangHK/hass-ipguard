"""Release actions use fresh permissions and never queue/replay a command."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from ipguard_api import ClientSnapshot, OutcomeUnknownError, Relay, Session
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ipguard.button import IpguardReleaseButton
from custom_components.ipguard.const import DOMAIN
from custom_components.ipguard.runtime import IpguardCoordinator, IpguardRuntimeData


def make_button(
    hass: HomeAssistant, client: Mock, relay: Relay, session: Session
) -> IpguardReleaseButton:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = IpguardCoordinator(hass, entry)
    coordinator.receive(ClientSnapshot((relay,), True, session=session))
    runtime = IpguardRuntimeData(client, coordinator)
    button = IpguardReleaseButton(entry.entry_id, runtime, relay)
    button.hass = hass
    return button


async def test_revoked_permission_prevents_send(
    hass: HomeAssistant, client: Mock, relay: Relay, session: Session
) -> None:
    button = make_button(hass, client, relay, session)
    button.coordinator.receive(
        ClientSnapshot((replace(relay, pending_confirmation=True),), True)
    )
    assert not button.available
    with pytest.raises(HomeAssistantError) as raised:
        await button.async_press()
    assert raised.value.translation_key == "unavailable"
    client.async_release.assert_not_awaited()
    assert button.should_poll is False
    assert not hasattr(button, "is_locked")


async def test_uncertain_release_is_never_retried(
    hass: HomeAssistant, client: Mock, relay: Relay, session: Session
) -> None:
    button = make_button(hass, client, relay, session)
    client.async_release.side_effect = OutcomeUnknownError("sensitive-key")
    with pytest.raises(HomeAssistantError) as raised:
        await button.async_press()
    assert raised.value.translation_key == "outcome_unknown"
    client.async_release.assert_awaited_once_with(relay.board.identity, 1)
    assert "sensitive-key" not in str(raised.value)


async def test_successful_press_sends_selected_relay_once(
    hass: HomeAssistant, client: Mock, relay: Relay, session: Session
) -> None:
    button = make_button(hass, client, relay, session)
    await button.async_press()
    client.async_release.assert_awaited_once_with(relay.board.identity, relay.number)


async def test_overlapping_press_is_rejected_instead_of_queued(
    hass: HomeAssistant, client: Mock, relay: Relay, session: Session
) -> None:
    button = make_button(hass, client, relay, session)
    started, finish = asyncio.Event(), asyncio.Event()

    async def release(*args: object) -> None:
        started.set()
        await finish.wait()

    client.async_release.side_effect = release
    first = hass.async_create_task(button.async_press())
    await started.wait()
    with pytest.raises(HomeAssistantError) as raised:
        await button.async_press()
    assert raised.value.translation_key == "busy"
    finish.set()
    await first
    client.async_release.assert_awaited_once()
