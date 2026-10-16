"""Explicit enrollment, replacement consent, and restoration flow behavior."""

from collections.abc import Generator
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from ipguard_api import (
    ConnectionFailureError,
    ExistingInstallationError,
    InvalidCodeError,
    RateLimitError,
    Session,
    SessionStatus,
    VerificationExpiredError,
    validate_identifier,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ipguard.const import (
    CONF_IDENTIFIER,
    CONF_INSTALLATION_NAME,
    CONF_REPLACE_EXISTING,
    CONF_REQUEST_NEW_CODE,
    CONF_SESSION,
    CONF_VERIFICATION_CODE,
    DOMAIN,
)

CLIENT_PATH = "custom_components.ipguard.config_flow.IpguardClient"
ACCOUNT = {CONF_IDENTIFIER: "person@example.invalid", CONF_INSTALLATION_NAME: "Kenny"}


@pytest.fixture(autouse=True)
def setup_mock() -> Generator[AsyncMock]:
    with patch(
        "custom_components.ipguard.async_setup_entry", return_value=True
    ) as setup:
        yield setup


async def challenge(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], ACCOUNT)


async def test_initial_form_has_no_side_effects(hass: HomeAssistant) -> None:
    with patch(CLIENT_PATH) as factory:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    factory.assert_not_called()


async def test_phone_setup_uses_normalized_unique_identity(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    client.async_complete_registration.return_value = replace(
        session, identifier=validate_identifier("+447700900123")
    )
    with patch(CLIENT_PATH, return_value=client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ACCOUNT, CONF_IDENTIFIER: "+44 7700 900123"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "phone:00447700900123"
    client.async_request_verification.assert_awaited_once_with(
        "+44 7700 900123", "Kenny"
    )


async def test_local_bad_code_does_not_verify_or_resend(
    hass: HomeAssistant, client: Mock
) -> None:
    with patch(CLIENT_PATH, return_value=client):
        result = await challenge(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "12345"}
        )
    assert result["errors"] == {CONF_VERIFICATION_CODE: "invalid_code"}
    client.async_verify_code.assert_not_awaited()
    client.async_request_verification.assert_awaited_once()


async def test_complete_flow_masks_code_and_persists_verified_session(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    with patch(CLIENT_PATH, return_value=client) as factory:
        result = await challenge(hass)
        assert result["step_id"] == "verification"
        selectors = list(result["data_schema"].schema.values())
        assert selectors[0].config["type"] == "password"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SESSION: session.to_dict()}
    factory.assert_called_once_with(session=None)
    client.async_request_verification.assert_awaited_once_with(
        "person@example.invalid", "Kenny"
    )
    client.async_verify_code.assert_awaited_once_with("012345")
    client.async_complete_registration.assert_awaited_once_with(replace_existing=False)
    client.async_get_relays.assert_awaited_once()
    client.async_release.assert_not_awaited()
    client.async_close.assert_awaited_once()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (CONF_IDENTIFIER, "not-an-account", "invalid_identifier"),
        (CONF_INSTALLATION_NAME, "IPGUARD CLI", "invalid_name"),
    ],
)
async def test_invalid_local_fields_prevent_requests(
    hass: HomeAssistant, field: str, value: str, error: str
) -> None:
    with patch(CLIENT_PATH) as factory:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ACCOUNT, field: value}
        )
    assert result["errors"] == {field: error}
    factory.assert_not_called()


async def test_duplicate_email_aborts_before_sending_code(
    hass: HomeAssistant, session: Session
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="email:person@example.invalid",
        data={CONF_SESSION: session.to_dict()},
    )
    entry.add_to_hass(hass)
    with patch(CLIENT_PATH) as factory:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ACCOUNT, CONF_IDENTIFIER: "Person@Example.Invalid"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    factory.assert_not_called()


async def test_bad_code_and_redraw_do_not_resend(
    hass: HomeAssistant, client: Mock
) -> None:
    client.async_verify_code.side_effect = InvalidCodeError()
    with patch(CLIENT_PATH, return_value=client):
        result = await challenge(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "111111"}
        )
        assert result["errors"] == {"base": "invalid_code"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], None)
        assert result["step_id"] == "verification"
        client.async_request_verification.assert_awaited_once()
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REQUEST_NEW_CODE: True}
        )
    assert client.async_request_verification.await_count == 2
    client.async_verify_code.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (VerificationExpiredError(), "code_expired"),
        (RateLimitError(), "rate_limited"),
        (ConnectionFailureError("sensitive-token"), "cannot_connect"),
    ],
)
async def test_classified_errors_are_safe(
    hass: HomeAssistant,
    client: Mock,
    error: Exception,
    key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client.async_verify_code.side_effect = error
    with patch(CLIENT_PATH, return_value=client):
        result = await challenge(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
    assert result["errors"] == {"base": key}
    assert "sensitive-token" not in caplog.text
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("consent", [False, True])
async def test_replacement_is_only_explicit(
    hass: HomeAssistant, client: Mock, session: Session, consent: bool
) -> None:
    client.async_complete_registration.side_effect = [
        ExistingInstallationError(),
        session,
    ]
    with patch(CLIENT_PATH, return_value=client):
        result = await challenge(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
        assert result["step_id"] == "replace"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REPLACE_EXISTING: consent}
        )
    if consent:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert client.async_complete_registration.await_args_list == [
            call(replace_existing=False),
            call(replace_existing=True),
        ]
    else:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "replacement_declined"
        client.async_complete_registration.assert_awaited_once_with(
            replace_existing=False
        )
    client.async_close.assert_awaited_once()


async def test_empty_inventory_is_valid_and_pending_authorization_is_not(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    client.async_get_relays.return_value = ()
    client.async_complete_registration.return_value = replace(
        session, status=SessionStatus.PENDING
    )
    with patch(CLIENT_PATH, return_value=client):
        result = await challenge(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        client.async_complete_registration.return_value = session
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cancel_closes_challenge(hass: HomeAssistant, client: Mock) -> None:
    with patch(CLIENT_PATH, return_value=client):
        result = await challenge(hass)
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()
    client.async_close.assert_awaited_once()


async def test_reauth_requires_submit_and_updates_same_entry(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kenny",
        unique_id="email:person@example.invalid",
        data={CONF_SESSION: session.to_dict()},
    )
    entry.add_to_hass(hass)
    with (
        patch(CLIENT_PATH, return_value=client) as factory,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["step_id"] == "reauth_confirm"
        factory.assert_not_called()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "user"
        factory.assert_not_called()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT
        )
        factory.assert_called_once_with(session=session)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VERIFICATION_CODE: "012345"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_SESSION]["identity"] == session.identity


async def test_reauth_rejects_other_account_before_code_request(
    hass: HomeAssistant, session: Session
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="email:person@example.invalid",
        data={CONF_SESSION: session.to_dict()},
    )
    entry.add_to_hass(hass)
    with patch(CLIENT_PATH) as factory:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ACCOUNT, CONF_IDENTIFIER: "other@example.invalid"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    factory.assert_not_called()
    assert entry.data[CONF_SESSION] == session.to_dict()
