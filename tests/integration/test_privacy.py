"""Secrets remain in private config storage, not exposed responses or errors."""

import asyncio
import json
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from homeassistant.components.config.config_entries import (
    ConfigManagerFlowResourceView,
    _prepare_config_flow_result_json,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import json_bytes
from ipguard_api import (
    AuthRevokedError,
    ConnectionFailureError,
    IpguardError,
    OutcomeUnknownError,
    Relay,
    RelayUnavailableError,
    Session,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_button import make_button

from custom_components.ipguard import async_setup_entry
from custom_components.ipguard.const import (
    CONF_IDENTIFIER,
    CONF_INSTALLATION_NAME,
    CONF_SESSION,
    CONF_VERIFICATION_CODE,
    DOMAIN,
)

SECRETS = (
    "secret-resident@example.invalid",
    "00447700900456",
    "private-device-identity",
    "006543",
    "private-sip-password",
    "opaque_CLI_Node.js_token",
)


@pytest.mark.parametrize(
    ("failure", "translation"),
    [
        (OutcomeUnknownError, "outcome_unknown"),
        (RelayUnavailableError, "unavailable"),
        (AuthRevokedError, "invalid_auth"),
        (ConnectionFailureError, "release_failed"),
    ],
)
async def test_full_button_traceback_and_logs_hide_sdk_error_contents(
    hass: HomeAssistant,
    client: Mock,
    relay: Relay,
    session: Session,
    caplog: pytest.LogCaptureFixture,
    failure: type[IpguardError],
    translation: str,
) -> None:
    button = make_button(hass, client, relay, session)
    client.async_release.side_effect = failure(" ".join(SECRETS))
    with pytest.raises(HomeAssistantError) as raised:
        await button.async_press()
    assert raised.value.translation_key == translation
    exposed = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert not any(secret in exposed for secret in SECRETS)
    assert not button._runtime.locks
    assert button.extra_state_attributes is None
    client.async_release.assert_awaited_once()
    await hass.async_block_till_done()


@pytest.mark.parametrize("failure", [AuthRevokedError, ConnectionFailureError])
async def test_full_setup_traceback_hides_sdk_error_contents(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
    failure: type[IpguardError],
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SESSION: session.to_dict()})
    entry.add_to_hass(hass)
    client.async_connect.side_effect = failure(" ".join(SECRETS))
    with (
        patch("custom_components.ipguard.IpguardClient", return_value=client),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await async_setup_entry(hass, entry)
    exposed = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert not any(secret in exposed for secret in SECRETS)
    client.async_close.assert_awaited_once()


async def test_flow_errors_do_not_export_sdk_payloads(
    hass: HomeAssistant, client: Mock
) -> None:
    client.async_verify_code.side_effect = ConnectionFailureError(" ".join(SECRETS))
    with patch(
        "custom_components.ipguard.config_flow.IpguardClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_IDENTIFIER: "resident@example.invalid",
                CONF_INSTALLATION_NAME: "Pixel 7",
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_VERIFICATION_CODE: "123456",
            },
        )
    assert result["errors"] == {"base": "cannot_connect"}
    response = ConfigManagerFlowResourceView(
        hass.config_entries.flow
    )._prepare_result_json(result)
    exposed = json_bytes(response).decode()
    assert not any(secret in exposed for secret in SECRETS)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    client.async_close.assert_awaited_once()


async def test_successful_flow_response_omits_saved_credentials(
    hass: HomeAssistant, session: Session
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SESSION: session.to_dict()})
    entry.add_to_hass(hass)
    result = _prepare_config_flow_result_json(
        {
            "type": FlowResultType.CREATE_ENTRY,
            "data": dict(entry.data),
            "context": {},
            "result": entry,
        },
        lambda value: dict(value),
    )
    assert "data" not in result and "context" not in result
    exposed = json_bytes(result).decode()
    assert session.verification_code not in exposed
    assert session.sip_password not in exposed


async def test_cancelled_press_releases_operation_lock(
    hass: HomeAssistant, client: Mock, relay: Relay, session: Session
) -> None:
    button = make_button(hass, client, relay, session)
    started = asyncio.Event()

    async def release(*args: object) -> None:
        started.set()
        await asyncio.Event().wait()

    client.async_release.side_effect = release
    task = asyncio.create_task(button.async_press())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not button._runtime.locks
    client.async_close.assert_not_awaited()
    client.async_release.return_value = None
    client.async_release.side_effect = None
    await button.async_press()
    assert not button._runtime.locks


def test_setup_text_contains_international_phone_examples() -> None:
    folder = Path(__file__).parents[2] / "custom_components/ipguard"
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((folder / name).read_text())
        description = text["config"]["step"]["user"]["data_description"]["identifier"]
        error = text["config"]["error"]["invalid_identifier"]
        for value in ("+447700900123", "+33612345678"):
            assert value in description and value in error
        assert "leading 0" in description
        assert "Spaces" in description and "00 prefixes" in description
